"""Error trigger do turno (30/out/2026): quando o turno quebra, o fio continua.

Como o "error trigger" de cada no no n8n: o turno que levanta excecao (LLM sem cota,
timeout, bug de logica) nao deixa mais o paciente sem nada. Com o tenant em
``TURN_FALLBACK_TENANTS``, o ``except`` final de ``conversation_turn_workflow`` chama
:func:`acionar_fallback_do_turno`, que, sem LLM, manda ao paciente UMA mensagem curta
de espera, se a conversa estiver com o bot e o turno ainda nao tiver respondido.

A equipe da clinica NAO e avisada: falha tecnica nunca aciona a equipe (docs/genius-heal).
Quem recebe o aviso e o operador, no celular dele (``OPS_ALERT_TO``), pelo alerta
"turno nao concluido" que o proprio ``except`` do turno ja manda; o desfecho daqui
entra nesse alerta.

A espera NAO quita a pendencia: sai com proposito ``handoff`` e o vigia de orfaos
(app/state/orfaos.py) continua cobrando a resposta de verdade.

O texto nao e um aviso generico: o assunto da fala (valor, agenda, arquivo ou geral),
o primeiro nome e uma variacao estavel por turno escolhem a frase. Sem prazo, sem
citar equipe ou sistema, sem promessa de resultado, neutra quanto ao genero do paciente.

Nunca levanta: o fallback nao pode trocar a excecao original do turno por outra.
Idempotente: ordinal fixo (95) na chave do outbox; replay do workflow nao duplica.
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any, Literal

from sqlalchemy import text

from app.contracts import OutboxCommand
from app.db import sql_session, transaction
from app.durable.handoff_notice import _normalize, contact_name
from app.durable.persistence import ClaimedTurn
from app.identity_primitives import stable_id

logger = logging.getLogger(__name__)

ORDINAL_PACIENTE = 95

_VALOR = re.compile(
    r"\b(quanto|valor|valores|preco|precos|custa|custo|cartao|parcela|parcelas|pix|desconto|orcamento)\b"
)
_AGENDA = re.compile(
    r"\b(horario|horarios|agenda|agendar|agendamento|marcar|marcacao|consulta|exame|exames|vaga|vagas|"
    r"segunda|terca|quarta|quinta|sexta|sabado|manha|tarde|amanha|hoje)\b"
)

# Duas frases por assunto, para a mesma pessoa nao ler sempre a mesma. {oi} = "Oi" ou "Oi, Nome".
_FRASES: dict[str, tuple[str, ...]] = {
    "valor": (
        "{oi}! Vou conferir os valores certinhos aqui pra te passar sem erro.",
        "{oi}! Deixa eu confirmar essa informação de valor direitinho antes de te responder.",
    ),
    "agenda": (
        "{oi}! Vou olhar a agenda aqui pra te passar o horário certo.",
        "{oi}! Deixa eu conferir a disponibilidade aqui pra te responder direitinho.",
    ),
    "arquivo": (
        "{oi}! Recebi o que você enviou e vou dar uma olhada com atenção.",
        "{oi}! Chegou aqui o que você mandou, vou ver com calma pra te responder.",
    ),
    "geral": (
        "{oi}! Vi sua mensagem e vou olhar isso com atenção pra te responder direitinho.",
        "{oi}! Deixa eu conferir isso aqui pra te passar a informação certa.",
    ),
}

_ESTADO_SQL = text(
    """
    /* turn_fallback.estado */
    SELECT
      EXISTS (SELECT 1 FROM genius_next.outbox ob
               WHERE ob.tenant_id = :tenant_id AND ob.turn_id = :turn_id
                 AND ob.kind = 'customer'
                 AND ob.delivery_status IN ('sent', 'recorded')) AS ja_respondeu,
      (SELECT c.automation_state FROM genius_next.clients c
        WHERE c.tenant_id = :tenant_id AND c.phone = :phone) AS automation_state
    """
)


@transaction()
def estado_para_fallback_txn(tenant_id: str, turn_id: str, phone: str) -> tuple[bool, str | None]:
    row = sql_session().execute(
        _ESTADO_SQL, {"tenant_id": tenant_id, "turn_id": turn_id, "phone": phone}
    ).one()
    return bool(row.ja_respondeu), row.automation_state


def _primeiro_nome(turn: Any) -> str | None:
    """Primeiro nome do contato, so se parecer nome (letras, 2 a 20 caracteres)."""
    bruto = contact_name(getattr(turn, "events", None) or [])
    if not bruto:
        return None
    primeiro = bruto.split()[0].strip(".,!?:;")
    if not (2 <= len(primeiro) <= 20) or not primeiro.isalpha():
        return None
    return primeiro[:1].upper() + primeiro[1:].lower()


def _assunto(turn: Any) -> str:
    falas = [
        (getattr(e, "text_content", None) or "")
        for e in (getattr(turn, "events", None) or [])
        if getattr(e, "direction", None) == "inbound"
    ]
    if not any(f.strip() for f in falas):
        return "arquivo"
    texto = _normalize(" ".join(falas))
    if _VALOR.search(texto):
        return "valor"
    if _AGENDA.search(texto):
        return "agenda"
    return "geral"


def texto_de_espera(turn: Any, override: str | None = None) -> str:
    """Frase de espera deste turno. Pura e estavel: o mesmo turno sempre gera a mesma."""
    if override and override.strip():
        return override.strip()
    variante = int(hashlib.sha256(turn.turn_id.encode()).hexdigest()[:8], 16) % 2
    nome = _primeiro_nome(turn)
    return _FRASES[_assunto(turn)][variante].format(oi=f"Oi, {nome}" if nome else "Oi")


def comando_de_espera(
    turn: ClaimedTurn,
    *,
    delivery_mode: Literal["record_only", "live"],
    texto: str,
) -> OutboxCommand:
    """Comando de espera ao paciente. Puro: sem banco, sem rede."""
    return OutboxCommand(
        tenant_id=turn.tenant_id,
        turn_id=turn.turn_id,
        ordinal=ORDINAL_PACIENTE,
        instance=turn.instance,
        destination_jid=turn.remote_jid,
        action="send_text",
        kind="customer",
        purpose="handoff",
        text=texto,
        idempotency_key=stable_id(
            "outbox", turn.tenant_id, turn.turn_id, ORDINAL_PACIENTE, "send_text"
        ),
        delivery_mode=delivery_mode,
    )


def acionar_fallback_do_turno(
    turn: ClaimedTurn,
    phone: str,
    delivery_mode: Literal["record_only", "live"],
) -> str:
    """Ramo de erro do turno. Devolve o desfecho para o alerta; nunca levanta."""
    from app.durable.outbox import dispatch_outbox_batch_workflow
    from app.durable.persistence import insert_outbox_command_txn
    from app.settings import get_settings

    try:
        settings = get_settings()
        if not settings.turn_fallback_enabled(turn.tenant_id):
            return "desligado"
        ja_respondeu, automation_state = estado_para_fallback_txn(
            turn.tenant_id, turn.turn_id, phone
        )
        if ja_respondeu:
            # O turno quebrou depois de responder (status, follow-up): nada a fazer.
            return "ja_respondido"
        if automation_state == "crew":
            return "conversa_com_a_equipe"
        comando = comando_de_espera(
            turn,
            delivery_mode=delivery_mode,
            texto=texto_de_espera(turn, settings.TURN_FALLBACK_TEXT),
        )
        insert_outbox_command_txn(comando)
        dispatch_outbox_batch_workflow([comando])
        return "espera_enviada"
    except Exception:
        logger.exception("turn %s: fallback do turno falhou", turn.turn_id)
        return "falhou"
