"""Fire-and-forget WhatsApp typing indicator (Evolution ``sendPresence``).

This module turns on the ``digitando…`` (composing) or ``gravando…``
(recording) indicator on the customer's WhatsApp while the turn's real reply
is being generated.  It is a **cosmetic side effect, not business state**:

  * It is intentionally OUTSIDE the turn job's checkpoint/retry machinery.  If the
    call fails, is slow, or the process dies mid-request, nothing about the
    conversation, turn, or outbox changes — the customer just does not see the
    indicator, and WhatsApp clears the previous state on its own.
  * For the same reason it never blocks the reply path: the HTTP call runs in
    its own daemon thread with a short timeout, and every exception is caught
    and logged silently so a provider hiccup can never delay or kill the real
    reply generation.

The HTTP pattern (URL, ``apikey`` header, digit-only ``number``) mirrors
``app.durable.outbox.send_text`` so there is no new provider or config shape.

Zernio (WhatsApp Cloud API) has its own, narrower indicator: ``POST
/v1/inbox/conversations/{id}/typing`` shows "digitando..." for up to 25s and
marks the customer's last message as read.  See ``fire_zernio_typing_indicator``.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Literal
from urllib.parse import quote

import httpx
from sqlalchemy.orm import Session

from app.durable.tenant_registry import get_registry_engine
from app.identity_primitives import normalize_phone
from app.settings import Settings, get_settings

logger = logging.getLogger("genius-next.presence")

PresenceKind = Literal["composing", "recording"]


def _secret_value(secret) -> str:
    if secret is None:
        raise ValueError("Evolution API key is not configured")
    return secret.get_secret_value()


def send_presence(
    *,
    instance: str,
    remote_jid: str,
    presence: PresenceKind,
    delay_ms: int,
    settings: Settings,
    client: httpx.Client | None = None,
) -> httpx.Response:
    """One ``POST /chat/sendPresence/{instance}`` call (blocking, caller's risk).

    Kept a plain, testable function that mirrors ``outbox.send_text``'s request
    shape.  Callers that want fire-and-forget behaviour use
    ``fire_presence_indicator`` instead; this function exists for direct
    invocation (live smoke test) and so the HTTP contract is independently
    exercisable with ``httpx.MockTransport``.
    """
    if settings.EVOLUTION_URL is None:
        raise ValueError("Evolution API base URL is not configured")
    url = f"{settings.EVOLUTION_URL}/chat/sendPresence/{instance}"
    payload = {
        "number": normalize_phone(remote_jid),
        "delay": delay_ms,
        "presence": presence,
    }
    owned_client = client is None
    http = client or httpx.Client(timeout=settings.PRESENCE_TIMEOUT_SECONDS)
    try:
        return http.post(
            url,
            headers={"apikey": _secret_value(settings.EVOLUTION_API_KEY)},
            json=payload,
        )
    finally:
        if owned_client:
            http.close()


def _fire(indicator: dict[str, object], *, settings: Settings, client: httpx.Client) -> None:
    """Real sendPresence body, run inside the daemon thread; never raises."""
    try:
        response = client.post(
            f"{settings.EVOLUTION_URL}/chat/sendPresence/{indicator['instance']}",
            headers={"apikey": _secret_value(settings.EVOLUTION_API_KEY)},
            json={
                "number": indicator["number"],
                "delay": indicator["delay"],
                "presence": indicator["presence"],
            },
        )
        if response.status_code >= 400:
            logger.warning(
                "presence indicator rejected by provider: status=%s instance=%s",
                response.status_code,
                indicator["instance"],
            )
    except httpx.TransportError:
        logger.info("presence indicator transport failure (ignored)")
    except Exception:
        logger.exception("presence indicator unexpected failure (ignored)")


def fire_presence_indicator(
    *,
    instance: str,
    remote_jid: str,
    presence: PresenceKind,
    delay_ms: int,
    fire_after_seconds: float = 0.0,
    settings: Settings | None = None,
) -> None:
    """Dispatch one presence indicator fire-and-forget on a daemon thread.

    Returns immediately; the HTTP call runs on a detached ``daemon=True``
    thread with a short timeout so it can never delay or crash the turn that
    spawned it.  Nothing here is part of the job's persisted state.

    ``fire_after_seconds``, when set, sleeps inside the daemon thread before
    the real HTTP call -- lets a caller fire this right at ingest (before
    the debounce window even starts) with a small delay instead of showing
    "digitando..." on the very first keystroke.

    The indicator is gated on the same delivery allowlist as the real message
    send (see ``outbox.dispatch_outbox_workflow``): we never show a live
    WhatsApp ``digitando…`` to a recipient the tenant has not explicitly
    allowlisted for delivery.  When the feature is disabled, the recipient is
    not allowlisted, or required settings are absent, the call is a silent
    no-op.
    """
    if settings is None:
        settings = get_settings()
    if not settings.PRESENCE_ENABLED or settings.EVOLUTION_URL is None:
        return
    try:
        number = normalize_phone(remote_jid)
    except ValueError:
        logger.warning("presence indicator skipped: unparseable jid %r", remote_jid)
        return
    canonical_jid = f"{number}@s.whatsapp.net"
    allowlist = settings.delivery_allowlist_jids
    if "*" not in allowlist and canonical_jid not in allowlist:
        logger.info(
            "presence indicator skipped: destination not allowlisted (%s)",
            canonical_jid,
        )
        return
    indicator = {
        "instance": instance,
        "number": number,
        "delay": delay_ms,
        "presence": presence,
    }
    owned_client = httpx.Client(timeout=settings.PRESENCE_TIMEOUT_SECONDS)

    def _task() -> None:
        try:
            if fire_after_seconds > 0:
                time.sleep(fire_after_seconds)
            _fire(indicator, settings=settings, client=owned_client)
        finally:
            owned_client.close()

    threading.Thread(target=_task, daemon=True, name="presence-indicator").start()


# --- Zernio (WhatsApp Cloud API) ------------------------------------------
#
# Diferencas para a Evolution, todas de proposito:
#   * uma chamada so: o "digitando..." da Cloud API dura ate 25s e some quando
#     a resposta chega, entao nao ha blocos nem duracao a pedir;
#   * a chamada marca a ultima mensagem do cliente como lida (tique azul), por
#     isso so dispara quando a automacao vai responder (automation_state open,
#     a mesma regra de ``automation_allowed_for_turn_txn``): conversa assumida
#     pela equipe (crew) ou perdida (loss) fica de fora e o tique continua
#     sendo de quem de fato leu;
#   * mensagem trivial ("ok", "obrigado", emoji) nao ganha indicador quando
#     SUPPRESS_TRIVIAL_TURNS_ENABLED esta ligado: o turno dela termina sem
#     resposta, e na Cloud API o "digitando..." ficaria ate 25s na tela sem
#     nada a caminho;
#   * chave propria (ZERNIO_TYPING_ENABLED, alem de PRESENCE_ENABLED): o
#     rollback do Zernio e so env, sem desligar o indicador da Evolution;
#   * o destino passa pela mesma regra de allowlist do envio do Zernio
#     (``outbox.destination_allowed``), que aceita o gemeo do nono digito.
# Nada aqui loga segredo, texto ou telefone.


def zernio_typing_url(settings: Settings, conversation_id: str) -> str:
    """Endpoint do indicador; mesmo prefixo e quoting de ``zernio_delivery``."""
    return (
        f"{settings.ZERNIO_API_URL}/v1/inbox/conversations/"
        f"{quote(conversation_id, safe='')}/typing"
    )


def send_zernio_typing(
    *,
    conversation_id: str,
    account_id: str,
    settings: Settings,
    client: httpx.Client | None = None,
) -> httpx.Response:
    """Uma chamada ``POST .../typing`` (bloqueante; o risco e de quem chama).

    Exposta para o teste do contrato HTTP com ``httpx.MockTransport`` e para
    smoke test manual; o caminho do webhook usa ``fire_zernio_typing_indicator``.
    """
    if settings.ZERNIO_API_KEY is None:
        raise ValueError("Zernio API key is not configured")
    owned_client = client is None
    http = client or httpx.Client(timeout=settings.PRESENCE_TIMEOUT_SECONDS)
    try:
        return http.post(
            zernio_typing_url(settings, conversation_id),
            headers={
                "Authorization": f"Bearer {settings.ZERNIO_API_KEY.get_secret_value()}"
            },
            json={"accountId": account_id},
        )
    finally:
        if owned_client:
            http.close()


def zernio_automation_state(*, tenant_id: str, phone: str, settings: Settings) -> str | None:
    """automation_state atual do contato (leitura simples, fora de transacao).

    Usa a MESMA SQL de ``persistence`` (``automation_allowed_for_turn_txn``):
    se aquela regra mudar, o indicador acompanha sem copia para esquecer.
    """
    from app.durable.persistence import _AUTOMATION_STATE_SQL

    with Session(get_registry_engine(settings.DATABASE_URL)) as session:
        return session.execute(
            _AUTOMATION_STATE_SQL, {"tenant_id": tenant_id, "phone": phone}
        ).scalar_one_or_none()


def _fire_zernio(
    *,
    tenant_id: str,
    phone: str,
    account_id: str,
    conversation_id: str,
    settings: Settings,
    state_lookup: Callable[..., str | None],
    client: httpx.Client | None = None,
) -> None:
    """Corpo da thread daemon do indicador do Zernio; nunca levanta excecao.

    Sem ``client``, o httpx.Client (e o contexto SSL dele) so nasce aqui
    dentro, depois da espera e do gate de estado, dentro de
    ``send_zernio_typing``: conversa fora de open nao cria cliente nenhum.
    """
    try:
        state = state_lookup(tenant_id=tenant_id, phone=phone, settings=settings)
    except Exception as exc:
        # Sem saber o estado nao marcamos a mensagem como lida: fica sem typing.
        logger.info("zernio typing skipped: automation state unavailable (%s)", type(exc).__name__)
        return
    if state != "open":
        logger.info("zernio typing skipped: conversation not automated (state=%s)", state)
        return
    try:
        response = send_zernio_typing(
            conversation_id=conversation_id,
            account_id=account_id,
            settings=settings,
            client=client,
        )
    except httpx.TransportError:
        logger.info("zernio typing transport failure (ignored)")
        return
    except Exception as exc:
        # Sem exc_info: o traceback do httpx pode carregar o header Authorization.
        logger.warning("zernio typing unexpected failure (ignored): %s", type(exc).__name__)
        return
    if response.status_code >= 400:
        logger.warning(
            "zernio typing rejected by provider: status=%s", response.status_code
        )
        return
    try:
        success = response.json().get("success")
    except Exception:
        success = None
    if success is not True:
        logger.info("zernio typing not shown by provider (success=%s)", success)


def fire_zernio_typing_indicator(
    *,
    tenant_id: str,
    phone: str,
    account_id: str,
    conversation_id: str | None,
    remote_jid: str,
    text_content: str | None = None,
    fire_after_seconds: float = 0.0,
    settings: Settings | None = None,
    state_lookup: Callable[..., str | None] | None = None,
) -> None:
    """Mostra "digitando..." no Zernio uma vez, fire-and-forget, numa thread daemon.

    Retorna na hora.  Gates sincronos e baratos (sem I/O): PRESENCE_ENABLED e
    ZERNIO_TYPING_ENABLED, ZERNIO_API_KEY, conversationId conhecido, mensagem
    que nao seja trivial sem resposta e destino na allowlist de entrega pela
    regra do Zernio.  A espera de ``fire_after_seconds``, a leitura do
    automation_state, a criacao do httpx.Client e a chamada HTTP acontecem
    dentro da thread, nunca no caminho do webhook; qualquer falha la dentro
    so vira log.
    """
    if settings is None:
        settings = get_settings()
    if not (settings.PRESENCE_ENABLED and settings.ZERNIO_TYPING_ENABLED):
        return
    if settings.ZERNIO_API_KEY is None or not conversation_id:
        return
    if settings.SUPPRESS_TRIVIAL_TURNS_ENABLED and text_content and text_content.strip():
        # Mesma regra de ``flow._turno_e_totalmente_trivial``: midia sem texto
        # (audio, foto) nunca conta como trivial e continua com indicador.
        from app.status.rules import is_trivial_no_reply

        if is_trivial_no_reply(text_content):
            logger.info("zernio typing skipped: trivial message, turn will not reply")
            return
    try:
        number = normalize_phone(remote_jid)
    except ValueError:
        logger.warning("zernio typing skipped: unparseable jid")
        return
    from app.durable.outbox import destination_allowed

    canonical_jid = f"{number}@s.whatsapp.net"
    if not destination_allowed(canonical_jid, settings.delivery_allowlist_jids, "zernio"):
        logger.info("zernio typing skipped: destination not allowlisted")
        return
    lookup = state_lookup or zernio_automation_state

    def _task() -> None:
        if fire_after_seconds > 0:
            time.sleep(fire_after_seconds)
        _fire_zernio(
            tenant_id=tenant_id,
            phone=phone,
            account_id=account_id,
            conversation_id=conversation_id,
            settings=settings,
            state_lookup=lookup,
        )

    threading.Thread(target=_task, daemon=True, name="zernio-typing-indicator").start()
