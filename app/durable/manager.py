"""Manager durável: promessa (A), assistência (C), indicação (D) e
triagem (E).

Quatro comportamentos atrás de ``Settings.MANAGER_AGENT_TENANTS`` (padrão
vazio = desligado); nenhum deles existe fora da flag. A storage nasce na
migration ``076_proactive_agent.sql`` (``genius_next.proactive_items``); a
triagem (E) estende o CHECK de ``kind`` na migration ``077_proactive_triagem``.

* **(A) Promessa** — o turno respondeu prometendo verificar algo e o draft
  continua ``send_text``. A pendência vira uma linha ``kind='promessa'`` e o
  workflow ``manager_assist_workflow`` tenta resolver sozinho pela rota
  densa (:mod:`app.reply.tools.reasoning`); se não resolver, avisa a equipe
  com contexto e devolve ao paciente uma assistência honesta (sem prazo).
* **(C) Assistência** — o turno foi para a equipe (``handoff``) e ninguém
  respondeu: após ``MANAGER_ASSIST_DUE_MINUTES`` o paciente recebe um
  acompanhamento real (o que já se sabe, o que falta, o próximo passo), uma
  única vez, sem cobrar e sem inventar prazo. Se a equipe já respondeu
  (``webapp_sends``), se o paciente falou de novo ou se a conversa voltou
  para a automação, o workflow encerra em silêncio.
* **(D) Indicação** — cartão de contato ou menção a indicado: linha
  ``kind='indicacao'`` para a equipe, sem workflow (a resposta do turno é a
  fixa de ``app.reply.manager`` ou a do próprio modelo).
* **(E) Triagem** (branch ``feat/triagem-proativa``, atrás de
  ``MANAGER_TRIAGE_TENANTS``) — um handoff do ramo normal (não
  segurança/mídia, sem pedido de pessoa nem cadastro) é interceptado ANTES de
  acionar a equipe: o paciente recebe um texto de espera, a conversa NÃO é
  marcada como da equipe, e o workflow (``kind='triagem'``) chama o agente
  proativo pedindo JSON. Se resolver e passar na guarda, devolve a resposta ao
  paciente; senão, fail-closed: paciente recebe o handoff original, o owner
  recebe o aviso humano com o resumo, e a conversa é marcada como da equipe
  pelo mesmo mecanismo do handoff normal. Nunca silencia.

Contrato com ``docs/core/05``: **este módulo não toca em
``app/durable/followup.py``**. O follow-up V2 continua exatamente como está
(lei da seção 3: jobs ``async`` com ``schedule_in``, transações sem
``asyncio.to_thread``) — aqui é um workflow novo, paralelo, de um único
disparo.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from app.jobs import AUX_QUEUE, RETRY_CADEIA, defer_once, jobs
from sqlalchemy import text as _sql_text

from app.contracts import OutboxCommand
from app.db import sql_session, transaction
from app.durable.handoff_notice import HandoffCause
from app.identity_primitives import stable_id
from app.ops_alert import ops_alert_step, ops_alert_text
from app.settings import get_settings

logger = logging.getLogger(__name__)

# Estados possiveis de genius_next.manager_items.state (CHECK na migration).
STATE_OPEN = "open"
STATE_RESOLVED = "resolved"
STATE_ESCALATED = "escalated"
STATE_STOPPED = "stopped"
STATE_DONE = "done"

# Triagem proativa antes da equipe (branch feat/triagem-proativa). Mesmo
# literal de app.reply.manager.TRIAGEM_KIND e do CHECK da migration 077.
TRIAGEM_KIND = "triagem"

# Texto honesto de assistencia (sem prazo, sem cobranca, sem valor inventado).
ASSISTENCIA_FALLBACK = (
    "Oi! Só para atualizar: seu caso continua com a nossa equipe e ainda não "
    "tenho um retorno novo para te passar. Assim que houver novidade eu te "
    "chamo por aqui. Qualquer dúvida, é só me mandar mensagem."
)
PROMESSA_NAO_RESOLVIDA_FALLBACK = (
    "Não consegui confirmar isso sozinha agora. Já deixei esse caso com a "
    "nossa equipe, com todo o contexto, e volto até você assim que tiver a "
    "resposta certa. Se precisar antes, é só me chamar."
)

_INSERT_ITEM_SQL = _sql_text(
    """
    /* manager.insert_item */
    INSERT INTO genius_next.manager_items
        (item_key, tenant_id, phone, remote_jid, instance, provider,
         origin_turn_id, kind, question, state, payload, due_at)
    VALUES
        (:item_key, :tenant_id, :phone, :remote_jid, :instance, :provider,
         :origin_turn_id, :kind, :question, 'open', CAST(:payload AS jsonb),
         now() + make_interval(mins => :due_minutes))
    ON CONFLICT (item_key) DO NOTHING
    RETURNING item_key
    """
)
_LOAD_ITEM_SQL = _sql_text(
    """
    /* manager.load_item */
    SELECT state, kind, question, payload::text AS payload
    FROM genius_next.manager_items
    WHERE item_key = :item_key
    """
)
_MARK_ITEM_SQL = _sql_text(
    """
    /* manager.mark_item */
    UPDATE genius_next.manager_items
    SET state = :state, updated_at = now(),
        payload = payload || CAST(:extra AS jsonb)
    WHERE item_key = :item_key AND state = 'open'
    """
)
_NEWER_INBOUND_SQL = _sql_text(
    """
    /* manager.newer_inbound */
    SELECT 1
    FROM genius_next.events e
    JOIN genius_next.turns t ON t.id = :origin_turn_id
    WHERE e.tenant_id = :tenant_id
      AND e.phone = :phone
      AND e.direction = 'inbound'
      AND e.conversation_sequence > t.last_sequence
    LIMIT 1
    """
)
_TEAM_ANSWERED_SQL = _sql_text(
    """
    /* manager.team_answered */
    SELECT 1
    FROM genius_next.webapp_sends w
    JOIN genius_next.turns t ON t.id = :origin_turn_id
    WHERE w.tenant_id = :tenant_id
      AND w.phone = :phone
      AND w.created_at > t.created_at
      AND w.status IN ('sent', 'uncertain', 'pending')
    LIMIT 1
    """
)
_CLIENT_STATE_SQL = _sql_text(
    """
    /* manager.client_state */
    SELECT status, automation_state
    FROM genius_next.clients
    WHERE tenant_id = :tenant_id AND phone = :phone
    """
)


def manager_item_key(
    tenant_id: str, origin_turn_id: str, kind: str, question: str
) -> str:
    """Chave idempotente da pendencia (replay nunca duplica linha)."""
    return stable_id("proactive", tenant_id, origin_turn_id, kind, question)


# --------------------------------------------------------------------------- #
# Transactions e steps
# --------------------------------------------------------------------------- #


@transaction(isolation_level="SERIALIZABLE")
def register_manager_item_txn(
    *,
    tenant_id: str,
    phone: str,
    remote_jid: str,
    instance: str,
    provider: str,
    origin_turn_id: str,
    kind: str,
    question: str,
    payload: dict[str, Any] | None = None,
    due_minutes: int = 5,
) -> str | None:
    """Cria a pendencia (idempotente). Devolve o item_key quando criou."""
    import json

    item_key = manager_item_key(tenant_id, origin_turn_id, kind, question)
    row = sql_session().execute(
        _INSERT_ITEM_SQL,
        {
            "item_key": item_key,
            "tenant_id": tenant_id,
            "phone": phone,
            "remote_jid": remote_jid,
            "instance": instance,
            "provider": provider,
            "origin_turn_id": origin_turn_id,
            "kind": kind,
            "question": question,
            "payload": json.dumps(payload or {}, ensure_ascii=False),
            "due_minutes": int(due_minutes),
        },
    ).scalar_one_or_none()
    return str(row) if row else None


@transaction(isolation_level="SERIALIZABLE")
def mark_manager_item_txn(item_key: str, state: str, extra: dict[str, Any] | None = None) -> None:
    """So 'open' vira outro estado: replay atrasado nunca reabre uma pendencia."""
    import json

    sql_session().execute(
        _MARK_ITEM_SQL,
        {
            "item_key": item_key,
            "state": state,
            "extra": json.dumps(extra or {}, ensure_ascii=False),
        },
    )


def _leitura_sql(statement: Any, params: dict[str, Any]) -> Any:
    """Leitura de uma consulta com Session simples do registry.

    ``sql_session()`` so existe dentro de ``@transaction`` ("AssertionError: db
    is only available within a transaction"), por isso a leitura abre Session
    propria, como ``draft_follow_up_step`` e ``promessa_contexto_step``.
    """
    from sqlalchemy.orm import Session

    from app.durable.tenant_registry import get_registry_engine

    with Session(get_registry_engine(get_settings().DATABASE_URL)) as session:
        return session.execute(statement, params)


def load_manager_item_step(item_key: str) -> dict[str, Any] | None:
    row = _leitura_sql(_LOAD_ITEM_SQL, {"item_key": item_key}).mappings().first()
    return dict(row) if row else None


def newer_inbound_step(tenant_id: str, phone: str, origin_turn_id: str) -> bool:
    """Paciente falou de novo depois do turno de origem: ele ja foi atendido."""
    row = _leitura_sql(
        _NEWER_INBOUND_SQL,
        {"tenant_id": tenant_id, "phone": phone, "origin_turn_id": origin_turn_id},
    ).scalar_one_or_none()
    return row is not None


def team_answered_step(tenant_id: str, phone: str, origin_turn_id: str) -> bool:
    """Alguem da equipe respondeu pelo App apos o turno de origem."""
    row = _leitura_sql(
        _TEAM_ANSWERED_SQL,
        {"tenant_id": tenant_id, "phone": phone, "origin_turn_id": origin_turn_id},
    ).scalar_one_or_none()
    return row is not None


def client_state_step(tenant_id: str, phone: str) -> tuple[str, str]:
    row = _leitura_sql(
        _CLIENT_STATE_SQL, {"tenant_id": tenant_id, "phone": phone}
    ).mappings().first()
    if row is None:
        return ("", "")
    return (str(row["status"] or ""), str(row["automation_state"] or ""))


def promessa_contexto_step(
    tenant_id: str, phone: str
) -> tuple[str, tuple[tuple[str, str], ...]]:
    """Doutrina ativa + historico curto para a rota densa (uma leitura)."""
    from sqlalchemy.orm import Session

    from app.agent.memory import get_conversation_history
    from app.durable.tenant_registry import get_registry_engine
    from app.reply.prompt_profiles import active_prompt_for

    settings = get_settings()
    with Session(get_registry_engine(settings.DATABASE_URL)) as session:
        business = active_prompt_for(tenant_id, "reply_normal") or ""
        conversation = get_conversation_history(
            session,
            tenant_id=tenant_id,
            phone=phone,
            limit=settings.HISTORY_MAX_MESSAGES,
            max_chars=settings.HISTORY_TOTAL_MAX_CHARS,
        )
    historico: tuple[tuple[str, str], ...] = ()
    if conversation.available:
        historico = tuple(
            (str(msg.role), str(msg.text)) for msg in conversation.messages
        )
    return (business, historico)


def purpose_for_step(tenant_id: str, phone: str) -> Literal["reply", "handoff"]:
    """``reply`` so quando a conversa ainda e' da automacao; caso contrario
    ``handoff`` — o guarda de entrega negaria um ``reply`` em conversa de
    equipe (``automation_ineligible_at_dispatch``) e o paciente ficaria sem
    retorno, que e' exatamente o que este modulo existe para evitar."""
    _, automation_state = client_state_step(tenant_id, phone)
    return "reply" if automation_state in ("", "open") else "handoff"


# --------------------------------------------------------------------------- #
# Envio (mesmo cano do follow-up: outbox -> dispatch)
# --------------------------------------------------------------------------- #


def _patient_commands(
    *,
    tenant_id: str,
    item_key: str,
    instance: str,
    remote_jid: str,
    text: str,
    purpose: Literal["reply", "handoff"],
    delivery_mode: Literal["record_only", "live"],
) -> list[OutboxCommand]:
    from app.reply.split import split_human_reply

    turn_id = stable_id("proactive", tenant_id, item_key)
    parts = split_human_reply(
        text,
        max_blocks=get_settings().OUTPUT_SPLIT_MAX_BLOCKS,
        target_chars=get_settings().OUTPUT_SPLIT_TARGET_CHARS,
    ) or [text]
    return [
        OutboxCommand(
            tenant_id=tenant_id,
            turn_id=turn_id,
            ordinal=ordinal,
            instance=instance,
            destination_jid=remote_jid,
            action="send_text",
            kind="customer",
            purpose=purpose,
            text=message,
            idempotency_key=stable_id(
                "outbox", tenant_id, turn_id, ordinal, "send_text"
            ),
            delivery_mode=delivery_mode,
        )
        for ordinal, message in enumerate(parts)
    ]


def _team_notice_command(
    *,
    tenant_id: str,
    item_key: str,
    instance: str,
    remote_jid: str,
    question: str,
    delivery_mode: Literal["record_only", "live"],
    texto: str | None = None,
) -> OutboxCommand | None:
    from app.durable.tenant_registry import manager_jid_txn

    manager_jid = manager_jid_txn(tenant_id)
    if not manager_jid:
        return None
    turn_id = stable_id("proactive", tenant_id, item_key)
    if texto is None:
        # 16/out/2026 (chairman): recado na voz dele, sem "[AGENTE PROATIVO]" nem cara de sistema.
        from app.durable.handoff_notice import build_human_notice

        texto = build_human_notice(
            remote_jid=remote_jid,
            patient_texts=[],
            motivo=f"precisa de um retorno sobre {question}",
        )
    return OutboxCommand(
        tenant_id=tenant_id,
        turn_id=turn_id,
        ordinal=90,
        instance=instance,
        destination_jid=manager_jid,
        action="send_text",
        kind="team",
        text=texto,
        idempotency_key=stable_id("outbox", tenant_id, turn_id, 90, "send_text"),
        delivery_mode=delivery_mode,
    )


def _dispatch(commands: list[OutboxCommand]) -> bool:
    from app.durable.outbox import dispatch_outbox_batch_workflow
    from app.durable.persistence import insert_outbox_command_txn

    for command in commands:
        insert_outbox_command_txn(command)
    outcomes = dispatch_outbox_batch_workflow(commands)
    return bool(outcomes) and all(
        outcome is not None and outcome.status in {"recorded", "sent"}
        for outcome in outcomes
    )


def _saida_passa_na_guarda(texto: str) -> bool:
    """Mesma guarda de saida do turno (sidecar), nunca um caminho paralelo."""
    from app.durable.flow import guard_assess_step
    from app.security.guard import GuardDisposition

    assessment = guard_assess_step("output", [texto], ())
    if assessment.disposition in (
        GuardDisposition.BLOCK.value,
        GuardDisposition.UNAVAILABLE.value,
    ):
        logger.warning("manager: guarda de saida reteve o texto (%s)", assessment.disposition)
        return False
    return True


# --------------------------------------------------------------------------- #
# Resolucao da promessa (A) e texto de assistencia (C)
# --------------------------------------------------------------------------- #


def _resolve_promessa(
    tenant_id: str, phone: str, question: str, web_search: bool
) -> str | None:
    """Rota densa + mesmas guardas de saida. ``None`` = nao resolveu."""
    from app.reply.tools.reasoning import reasoning_route

    business, historico = promessa_contexto_step(tenant_id, phone)
    texto = reasoning_route(
        tenant_id=tenant_id,
        pergunta=(
            f"A pessoa perguntou e a atendente prometeu verificar: {question}. "
            "Confirme com base no catalogo o que pode ser dito agora; se nao "
            "estiver no catalogo, diga claramente que precisa da equipe."
        ),
        business_prompt=business,
        historico=historico,
        web_search=web_search,
    )
    if not texto:
        return None
    if not _saida_passa_na_guarda(texto):
        return None
    return texto


def _texto_assistencia(
    tenant_id: str, phone: str, question: str, web_search: bool
) -> str:
    """(C) Acompanhamento real; se a rota densa nao der, cai no fallback fixo."""
    from app.reply.tools.reasoning import reasoning_route

    business, historico = promessa_contexto_step(tenant_id, phone)
    texto = reasoning_route(
        tenant_id=tenant_id,
        pergunta=(
            "A conversa esta com a equipe e ainda nao houve retorno. Monte UM "
            "acompanhamento curto para a pessoa: o que ja se sabe do caso, o "
            "que ainda falta e o proximo passo. Sem cobrar resposta, sem prazo, "
            "sem urgencia, sem inventar valor ou horario. "
            f"Contexto do caso: {question}"
        ),
        business_prompt=business,
        historico=historico,
        web_search=web_search,
    )
    if not texto or not _saida_passa_na_guarda(texto):
        return ASSISTENCIA_FALLBACK
    return texto


def _envio_pendente(item_key: str, tenant_id: str, phone: str, detalhe: str) -> str:
    """Fecha o workflow como ``envio_pendente`` e avisa o chairman.

    O Manager existe para o paciente nao ficar sem retorno: quando o
    disparo nao confirma, ninguem mais vai olhar — so o alerta.
    """
    logger.warning("manager: %s %s", detalhe, item_key)
    try:
        ops_alert_step(
            f"proativo:{item_key}",
            ops_alert_text(
                "Manager: envio nao confirmado",
                tenant_id=tenant_id,
                contact=phone,
                error=detalhe,
            ),
        )
    except Exception:
        logger.warning("manager: alerta operacional nao enviado", exc_info=True)
    # 16/out/2026: item `open` para sempre mantinha o alerta critico ligado e o paciente
    # esquecido; o alerta acima e a unica acao possivel, entao o item fecha como stopped.
    try:
        mark_manager_item_txn(item_key, STATE_STOPPED, {"motivo": "envio_pendente", "detalhe": detalhe[:120]})
    except Exception:
        logger.warning("manager: item nao fechado apos envio pendente", exc_info=True)
    return "envio_pendente"


# --------------------------------------------------------------------------- #
# Triagem proativa (branch feat/triagem-proativa): resolver antes da equipe
# --------------------------------------------------------------------------- #


def _triagem_context(item: dict[str, Any]) -> dict[str, Any]:
    """Payload do item 'triagem' (``payload::text`` do SELECT) como dict."""
    import json

    raw = item.get("payload")
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _parse_triagem(texto: str) -> Any | None:
    """Valida o JSON do modelo com o contrato fechado. None em qualquer falha."""
    import json

    from pydantic import ValidationError

    from app.contracts import TriageResolution

    raw = (texto or "").strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end < start:
        logger.info("manager: triagem sem JSON na saida")
        return None
    try:
        data = json.loads(raw[start : end + 1])
    except (ValueError, TypeError):
        logger.info("manager: triagem devolveu JSON invalido")
        return None
    if not isinstance(data, dict):
        return None
    try:
        return TriageResolution.model_validate(data)
    except ValidationError:
        logger.info("manager: triagem nao passou no contrato TriageResolution")
        return None


def _resolve_triagem_llm(
    tenant_id: str, phone: str, ultimas_falas: list[str]
) -> Any | None:
    """Chama o Manager pedindo JSON. Qualquer falha devolve None
    (= fail-closed para a equipe)."""
    from app.reply.tools.reasoning import triage_route

    try:
        business, historico = promessa_contexto_step(tenant_id, phone)
        pergunta = " ".join(t.strip() for t in ultimas_falas if t and t.strip())
        texto = triage_route(
            tenant_id=tenant_id,
            business_prompt=business,
            historico=historico,
            pergunta=pergunta[-500:] or "A pessoa precisa de ajuda.",
            # Roda dentro de triagem_decisao_step: abrir ``@transaction`` aqui
            # levanta AssertionError. O teto diario e checado/registrado no corpo
            # do job (_resolve_triagem).
            conta_chamada=False,
        )
    except Exception:
        logger.warning("manager: rota de triagem falhou", exc_info=True)
        return None
    if not texto:
        return None
    return _parse_triagem(texto)


def triagem_decisao_step(
    tenant_id: str, phone: str, ultimas_falas: list[str]
) -> dict[str, Any] | None:
    """Decisão do Manager (triage_route + parse) como UM passo.

    Envolver a chamada e o parse num único passo é o que garante
    determinismo: numa retentativa a MESMA decisão (dit serializável) é
    reutilizada e o job nunca manda duas mensagens diferentes. Falha aqui
    devolve None (fail-closed para a equipe), nunca levanta.
    """
    try:
        resolucao = _resolve_triagem_llm(tenant_id, phone, ultimas_falas)
    except Exception:
        logger.warning("manager: decisao de triagem falhou", exc_info=True)
        return None
    if resolucao is None:
        return None
    return {
        "resolvido": bool(resolucao.resolvido),
        "resposta_paciente": resolucao.resposta_paciente or "",
        "resumo_para_equipe": resolucao.resumo_para_equipe or "",
    }


def _resolve_triagem(
    *,
    item_key: str,
    item: dict[str, Any],
    tenant_id: str,
    phone: str,
    remote_jid: str,
    instance: str,
    provider: str,
    origin_turn_id: str,
    delivery_mode: Literal["record_only", "live"],
) -> str:
    """Ramo 'triagem': tenta resolver sozinho; se não der, fail-closed à equipe.

    Nunca silencia: todo caminho termina em mensagem ao paciente.
    """
    from app.durable.flow import SAFE_HANDOFF_TEXT, handoff_marking_expires
    from app.durable.handoff_notice import build_human_notice
    from app.durable.persistence import apply_manager_handoff_txn

    ctx = _triagem_context(item)
    draft_original = str(ctx.get("draft_original") or "")
    causa = str(ctx.get("causa") or "") or None
    ultimas = [str(t) for t in (ctx.get("ultimas_falas") or [])]
    nome = str(ctx.get("nome") or "") or None

    # 1. A equipe já assumiu (Assumir / team_answered / estado crew): silêncio.
    _status, automation_state = client_state_step(tenant_id, phone)
    if automation_state == "crew":
        mark_manager_item_txn(item_key, STATE_DONE, {"motivo": "equipe_assinou"})
        return "equipe_assinou"
    if team_answered_step(tenant_id, phone, origin_turn_id):
        mark_manager_item_txn(item_key, STATE_DONE, {"motivo": "equipe_respondeu"})
        return "equipe_respondeu"

    # 2. Manager tenta resolver (JSON). Paciente ter falado de novo NÃO
    #    cancela a triagem (diferente da promessa). UM step: replay reusa a
    #    mesma decisão e nunca manda duas mensagens diferentes.
    #    Teto diario da rota densa no CORPO do job, nunca dentro do passo
    #    (@transaction dentro de outro passo levanta AssertionError e o
    #    try/except engoliria o teto em silencio). Teto estourado = fail-closed.
    from app.reply.tools.reasoning import (
        registra_uso_raciocinio_txn,
        uso_raciocinio_hoje_txn,
    )

    try:
        hoje = uso_raciocinio_hoje_txn(tenant_id)
    except Exception:
        logger.warning("manager: teto diario indisponivel; seguindo sem conta", exc_info=True)
        hoje = 0
    if hoje >= get_settings().MANAGER_REASONING_DAILY_MAX:
        logger.info("manager: triagem recusada pelo teto diario (%s)", tenant_id)
        decisao = None
    else:
        decisao = triagem_decisao_step(tenant_id, phone, ultimas)
        if decisao is not None:
            try:
                registra_uso_raciocinio_txn(tenant_id)
            except Exception:
                logger.warning("manager: falha ao contar uso da triagem", exc_info=True)

    # 3. Resolveu e passou na guarda de saída: devolve ao paciente na voz da
    #    atendente (mesmo cano do proativo). Conversa segue como resposta normal.
    if decisao is not None and decisao.get("resolvido"):
        resposta = (decisao.get("resposta_paciente") or "").strip()
        if resposta and _saida_passa_na_guarda(resposta):
            purpose = purpose_for_step(tenant_id, phone)
            enviado = _dispatch(
                _patient_commands(
                    tenant_id=tenant_id,
                    item_key=item_key,
                    instance=instance,
                    remote_jid=remote_jid,
                    text=resposta,
                    purpose=purpose,
                    delivery_mode=delivery_mode,
                )
            )
            if enviado:
                mark_manager_item_txn(item_key, STATE_RESOLVED, {"via": "triagem"})
                return "resolvido"
            return _envio_pendente(
                item_key, tenant_id, phone, "envio da triagem nao confirmado"
            )

    # 4. Fail-closed para a equipe: paciente recebe o handoff original (ou o
    #    texto seguro), owner recebe o aviso humano com o resumo, e a conversa é
    #    marcada como da equipe pelo MESMO mecanismo do handoff normal.
    resumo = (decisao.get("resumo_para_equipe") if decisao is not None else "") or ""
    texto_paciente = draft_original.strip() or SAFE_HANDOFF_TEXT
    if not _saida_passa_na_guarda(texto_paciente):
        texto_paciente = SAFE_HANDOFF_TEXT
    comandos = _patient_commands(
        tenant_id=tenant_id,
        item_key=item_key,
        instance=instance,
        remote_jid=remote_jid,
        text=texto_paciente,
        purpose="handoff",
        delivery_mode=delivery_mode,
    )
    aviso = _team_notice_command(
        tenant_id=tenant_id,
        item_key=item_key,
        instance=instance,
        remote_jid=remote_jid,
        question="triagem proativa",
        delivery_mode=delivery_mode,
        texto=build_human_notice(
            remote_jid=remote_jid,
            patient_texts=ultimas,
            name=nome,
            cause=causa,  # type: ignore[arg-type]
            resumo=resumo,
        ),
    )
    if aviso is not None:
        comandos.append(aviso)
    if not _dispatch(comandos):
        return _envio_pendente(
            item_key, tenant_id, phone, "escalonamento da triagem nao confirmado"
        )

    crew_expires = handoff_marking_expires(tenant_id, "promovido")
    apply_manager_handoff_txn(
        tenant_id=tenant_id, phone=phone, crew_expires=crew_expires
    )
    mark_manager_item_txn(item_key, STATE_ESCALATED, {"via": "triagem_equipe"})
    # Follow-up pelo mesmo mecanismo do handoff normal (não duplica a lógica).
    try:
        from types import SimpleNamespace

        from app.durable import flow

        turn_ns = SimpleNamespace(
            tenant_id=tenant_id,
            remote_jid=remote_jid,
            instance=instance,
            turn_id=origin_turn_id,
            provider=provider,
        )
        flow._start_follow_up_v2(
            turn_ns,
            phone,
            customer_status="open",
            needs_human=True,
            handoff_cause="promovido",
        )
    except Exception:
        logger.warning("manager: follow-up da triagem nao agendado", exc_info=True)
    return "escalado"


def register_turn_triagem(
    turn: Any, draft: Any, handoff_cause: HandoffCause | None, *, phone: str
) -> Any | None:
    """Triagem proativa no turno: se aplica, troca o handoff por um texto de
    espera, registra o item ``kind='triagem'`` e dispara o workflow.

    Devolve o novo ``ReplyDraft`` (``send_text`` de espera) quando a triagem
    assume o caso, ou ``None`` quando não aplica (o draft original segue).
    Nunca levanta: é chamada dentro de um try no fluxo.
    """
    from app.contracts import ReplyDraft
    from app.durable.handoff_notice import contact_name
    from app.reply.manager import (
        TRIAGEM_ESPERA_TEXT,
        TRIAGEM_KIND,
        triagem_aplica,
    )

    if getattr(draft, "action", None) != "handoff":
        return None
    patient_texts = [
        (getattr(e, "text_content", "") or "").strip()
        for e in getattr(turn, "events", [])
        if getattr(e, "direction", None) == "inbound"
    ]
    patient_texts = [t for t in patient_texts if t]
    if not triagem_aplica(
        tenant_id=getattr(turn, "tenant_id", ""),
        action=draft.action,
        handoff_cause=handoff_cause,
        patient_texts=patient_texts,
    ):
        return None

    settings = get_settings()
    nome = contact_name(getattr(turn, "events", []) or []) or ""
    payload = {
        "draft_original": getattr(draft, "text", "") or "",
        "causa": handoff_cause or "",
        "ultimas_falas": patient_texts[-4:],
        "nome": nome,
    }
    criado = register_manager_item_txn(
        tenant_id=turn.tenant_id,
        phone=phone,
        remote_jid=turn.remote_jid,
        instance=turn.instance,
        provider=turn.provider,
        origin_turn_id=turn.turn_id,
        kind=TRIAGEM_KIND,
        question="triagem proativa",
        payload=payload,
        due_minutes=1,
    )
    if criado:
        start_assist_workflow(
            criado,
            tenant_id=turn.tenant_id,
            remote_jid=turn.remote_jid,
            phone=phone,
            instance=turn.instance,
            provider=turn.provider,
            kind=TRIAGEM_KIND,
            question="triagem proativa",
            origin_turn_id=turn.turn_id,
            delay_seconds=settings.MANAGER_TRIAGE_DELAY_SECONDS,
        )
    # O voto de status le a fala do paciente, que nao mudou: segue junto.
    return ReplyDraft(
        action="send_text",
        text=TRIAGEM_ESPERA_TEXT,
        llm_model=draft.llm_model,
        status_signal=draft.status_signal,
        fatos=draft.fatos,
    )


# --------------------------------------------------------------------------- #
# Workflow de um unico disparo
# --------------------------------------------------------------------------- #


def manager_assist_workflow(
    item_key: str,
    tenant_id: str,
    remote_jid: str,
    phone: str,
    instance: str,
    provider: str,
    kind: str,
    question: str,
    origin_turn_id: str,
) -> str:
    """Devolve ao paciente ou encerra em silencio, ja passado o atraso.

    O atraso (5 min para promessa/assistencia, segundos na triagem) e o ``schedule_in`` do job
    (``start_assist_workflow``): o worker fica livre enquanto espera.
    """
    try:
        item = load_manager_item_step(item_key)
        if item is None or item.get("state") != STATE_OPEN:
            return "sem_pendencia"

        from app.settings import get_settings as _settings

        settings = _settings()
        delivery_mode = settings.DELIVERY_MODE

        if kind == TRIAGEM_KIND:
            return _resolve_triagem(
                item_key=item_key,
                item=item,
                tenant_id=tenant_id,
                phone=phone,
                remote_jid=remote_jid,
                instance=instance,
                provider=provider,
                origin_turn_id=origin_turn_id,
                delivery_mode=delivery_mode,
            )

        if newer_inbound_step(tenant_id, phone, origin_turn_id):
            mark_manager_item_txn(item_key, STATE_STOPPED, {"motivo": "paciente_respondeu"})
            return "paciente_respondeu"

        if kind == "promessa":
            texto = _resolve_promessa(
                tenant_id, phone, question, web_search=settings.EXA_API_KEY is not None
            )
            purpose = purpose_for_step(tenant_id, phone)
            if texto:
                enviado = _dispatch(
                    _patient_commands(
                        tenant_id=tenant_id,
                        item_key=item_key,
                        instance=instance,
                        remote_jid=remote_jid,
                        text=texto,
                        purpose=purpose,
                        delivery_mode=delivery_mode,
                    )
                )
                if enviado:
                    mark_manager_item_txn(item_key, STATE_RESOLVED, {"via": "rota_densa"})
                    return "resolvido"
                return _envio_pendente(
                    item_key, tenant_id, phone, "envio da promessa nao confirmado"
                )

            # Nao resolveu: equipe com contexto + paciente com assistencia honesta.
            comandos = _patient_commands(
                tenant_id=tenant_id,
                item_key=item_key,
                instance=instance,
                remote_jid=remote_jid,
                text=PROMESSA_NAO_RESOLVIDA_FALLBACK,
                purpose=purpose,
                delivery_mode=delivery_mode,
            )
            aviso = _team_notice_command(
                tenant_id=tenant_id,
                item_key=item_key,
                instance=instance,
                remote_jid=remote_jid,
                question=question,
                delivery_mode=delivery_mode,
            )
            if aviso is not None:
                comandos.append(aviso)
            if _saida_passa_na_guarda(PROMESSA_NAO_RESOLVIDA_FALLBACK) and _dispatch(comandos):
                mark_manager_item_txn(item_key, STATE_ESCALATED, {"via": "equipe"})
                return "escalado"
            return _envio_pendente(
                item_key, tenant_id, phone, "escalonamento nao confirmado"
            )

        # (C) Assistencia na vez da equipe.
        status, automation_state = client_state_step(tenant_id, phone)
        if status not in ("open", "") or automation_state != "crew":
            mark_manager_item_txn(item_key, STATE_DONE, {"motivo": "conversa_saiu_da_equipe"})
            return "conversa_saiu_da_equipe"
        if team_answered_step(tenant_id, phone, origin_turn_id):
            mark_manager_item_txn(item_key, STATE_DONE, {"motivo": "equipe_respondeu"})
            return "equipe_respondeu"

        texto = _texto_assistencia(
            tenant_id, phone, question, web_search=settings.EXA_API_KEY is not None
        )
        enviado = _dispatch(
            _patient_commands(
                tenant_id=tenant_id,
                item_key=item_key,
                instance=instance,
                remote_jid=remote_jid,
                text=texto,
                purpose="handoff",
                delivery_mode=delivery_mode,
            )
        )
        if enviado:
            mark_manager_item_txn(item_key, STATE_DONE, {"via": "assistencia"})
            return "assistencia_enviada"
        return _envio_pendente(
            item_key, tenant_id, phone, "envio da assistencia nao confirmado"
        )
    except Exception as exc:
        # Ordem do chairman 15/out/2026: falha do proativo avisa no WhatsApp.
        # A excecao original continua subindo exatamente igual.
        try:
            ops_alert_step(
                f"proativo:{item_key}",
                ops_alert_text(
                    "workflow proativo falhou",
                    tenant_id=tenant_id,
                    contact=phone,
                    error=f"{type(exc).__name__}: {exc}",
                ),
            )
        except Exception:
            logger.warning("manager: alerta operacional nao enviado", exc_info=True)
        if kind == TRIAGEM_KIND:
            _falha_fechada_triagem(
                item_key=item_key,
                tenant_id=tenant_id,
                phone=phone,
                remote_jid=remote_jid,
                instance=instance,
            )
        raise


def _falha_fechada_triagem(
    *, item_key: str, tenant_id: str, phone: str, remote_jid: str, instance: str
) -> None:
    """O workflow da triagem quebrou: o paciente nao pode ficar esperando.

    Melhor esforco, nunca levanta: manda o aviso humano a equipe (e o texto seguro ao paciente),
    e fecha o item (`escalated` se o envio confirmou, senao `stopped`). Sem isto o item ficava
    `open` para sempre (16/out/2026: duas triagens presas por 5 h).
    """
    try:
        from app.durable.flow import SAFE_HANDOFF_TEXT
        from app.durable.handoff_notice import build_human_notice
        from app.settings import get_settings as _settings

        item = load_manager_item_step(item_key)
        if item is None or item.get("state") != STATE_OPEN:
            return
        ctx = _triagem_context(item)
        delivery_mode = _settings().DELIVERY_MODE
        comandos = _patient_commands(
            tenant_id=tenant_id,
            item_key=item_key,
            instance=instance,
            remote_jid=remote_jid,
            text=str(ctx.get("draft_original") or "").strip() or SAFE_HANDOFF_TEXT,
            purpose="handoff",
            delivery_mode=delivery_mode,
        )
        aviso = _team_notice_command(
            tenant_id=tenant_id,
            item_key=item_key,
            instance=instance,
            remote_jid=remote_jid,
            question="triagem proativa",
            delivery_mode=delivery_mode,
            texto=build_human_notice(
                remote_jid=remote_jid,
                patient_texts=[str(t) for t in (ctx.get("ultimas_falas") or [])],
                name=str(ctx.get("nome") or "") or None,
                cause=(str(ctx.get("causa") or "") or None),  # type: ignore[arg-type]
            ),
        )
        if aviso is not None:
            comandos.append(aviso)
        enviado = _dispatch(comandos)
        mark_manager_item_txn(
            item_key,
            STATE_ESCALATED if enviado else STATE_STOPPED,
            {"via": "falha_fechada", "enviado": bool(enviado)},
        )
    except Exception:
        logger.warning("manager: falha-fechada da triagem tambem falhou", exc_info=True)
        try:
            mark_manager_item_txn(item_key, STATE_STOPPED, {"motivo": "erro_workflow"})
        except Exception:
            logger.warning("manager: item nao fechado apos erro", exc_info=True)


@jobs.task(name="manager_assist", queue=AUX_QUEUE, retry=RETRY_CADEIA)
def manager_assist_job(item_key: str, **kwargs: Any) -> None:
    manager_assist_workflow(item_key, **kwargs)


def start_assist_workflow(
    item_key: str,
    *,
    delay_minutes: int = 5,
    delay_seconds: int | None = None,
    **kwargs: Any,
) -> None:
    """Agenda o disparo para daqui ``delay``; o mesmo item nunca agenda duas vezes.

    ``delay_seconds`` (triagem) sobrescreve ``delay_minutes`` (promessa/assistencia).
    """
    if delay_seconds is not None:
        delay = max(0, int(delay_seconds))
    else:
        delay = max(1, int(delay_minutes)) * 60
    defer_once(
        manager_assist_job,
        queueing_lock=stable_id("proactive-assist", item_key),
        schedule_in=delay,
        item_key=item_key,
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# Gancho do turno (chamado por app/durable/flow.py)
# --------------------------------------------------------------------------- #


def register_turn_items(turn: Any, draft: Any, *, phone: str) -> None:
    """Registra as pendencias deste turno. Nao levanta: nunca derruba o turno.

    Chamado so quando a flag do tenant esta ligada e o caminho de seguranca
    (rota fixa / fallback de audio) nao assumiu o turno.
    """
    from app.reply.manager import (
        ASSISTENCIA_KIND,
        INDICACAO_KIND,
        PROMESSA_KIND,
        detect_indication,
        detect_promise,
        manager_enabled,
        turn_has_contact_card,
    )

    if not manager_enabled(getattr(turn, "tenant_id", "")):
        return

    settings = get_settings()
    base = {
        "tenant_id": turn.tenant_id,
        "phone": phone,
        "remote_jid": turn.remote_jid,
        "instance": turn.instance,
        "provider": turn.provider,
        "origin_turn_id": turn.turn_id,
    }

    try:
        # (D) Cartao de contato: lead para a equipe, sem workflow.
        if turn_has_contact_card(turn):
            register_manager_item_txn(
                **base,
                kind=INDICACAO_KIND,
                question="cartao de contato recebido no WhatsApp",
                payload={"origem": "contactMessage"},
                due_minutes=settings.MANAGER_ASSIST_DUE_MINUTES,
            )

        texto_resposta = getattr(draft, "text", "") or ""
        acao = getattr(draft, "action", "send_text")

        # (D) Indicacao dita em texto (a resposta do modelo ja respondeu).
        indicacao = detect_indication(texto_resposta)
        if indicacao is None:
            indicacao = detect_indication(
                " ".join(
                    (getattr(e, "text_content", "") or "")
                    for e in getattr(turn, "events", [])
                    if getattr(e, "direction", None) == "inbound"
                )
            )
        if indicacao:
            register_manager_item_txn(
                **base,
                kind=INDICACAO_KIND,
                question=indicacao,
                payload={"origem": "texto"},
                due_minutes=settings.MANAGER_ASSIST_DUE_MINUTES,
            )

        if acao == "handoff":
            # (C) Vez da equipe: um unico acompanhamento apos o prazo.
            item_key = manager_item_key(
                turn.tenant_id, turn.turn_id, ASSISTENCIA_KIND, "vez da equipe"
            )
            criado = register_manager_item_txn(
                **base,
                kind=ASSISTENCIA_KIND,
                question="vez da equipe: paciente aguardando retorno",
                payload={"handoff": True},
                due_minutes=settings.MANAGER_ASSIST_DUE_MINUTES,
            )
            if criado:
                start_assist_workflow(
                    criado,
                    tenant_id=turn.tenant_id,
                    remote_jid=turn.remote_jid,
                    phone=phone,
                    instance=turn.instance,
                    provider=turn.provider,
                    kind=ASSISTENCIA_KIND,
                    question="vez da equipe: paciente aguardando retorno",
                    origin_turn_id=turn.turn_id,
                    delay_minutes=settings.MANAGER_ASSIST_DUE_MINUTES,
                )
            else:
                logger.debug("manager: assistencia ja registrada %s", item_key)
            return

        # (A) Promessa: so em reply normal (handoff ja cuida do caso acima).
        assunto = detect_promise(texto_resposta)
        if assunto:
            question = f"{assunto} prometido pela atendente"
            criado = register_manager_item_txn(
                **base,
                kind=PROMESSA_KIND,
                question=question,
                payload={"assunto": assunto},
                due_minutes=settings.MANAGER_PROMISE_DUE_MINUTES,
            )
            if criado:
                start_assist_workflow(
                    criado,
                    tenant_id=turn.tenant_id,
                    remote_jid=turn.remote_jid,
                    phone=phone,
                    instance=turn.instance,
                    provider=turn.provider,
                    kind=PROMESSA_KIND,
                    question=question,
                    origin_turn_id=turn.turn_id,
                    delay_minutes=settings.MANAGER_PROMISE_DUE_MINUTES,
                )
    except Exception:
        # Iniciativa proativa nunca pode derrubar um turno que ja foi respondido.
        logger.warning("manager: falha ao registrar pendencias", exc_info=True)
