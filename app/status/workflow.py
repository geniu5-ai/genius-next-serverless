"""Workflow durável pós-rascunho do módulo de status."""

from __future__ import annotations

from typing import Literal

import logging

import httpx
from app.jobs import AUX_QUEUE, jobs
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.contracts import AgentStatusSignal, FatoConversa, ReplyDraft
from app.db import get_engine
from app.durable.persistence import ClaimedTurn, apply_status_txn, record_conversation_facts_txn
from app.identity_primitives import stable_id
from app.ingress.evolution import HistoryResult
from app.settings import get_settings
from app.state.fatos_regra import derivar_fatos
from app.status.audit import (
    EMBED_SCORE_TIMEOUT_SECONDS,
    StatusInput,
    StatusResult,
    classify_status,
)

logger = logging.getLogger(__name__)

# Os primeiros julgamentos de cada paciente pulam o LLM do juiz (regra e trivial continuam).
JULGAMENTOS_SEM_JUIZ = 2
_JULGAMENTOS_ANTERIORES_SQL = text(
    "/* status.julgamentos_anteriores */ SELECT count(*) FROM genius_next.status_audits "
    "WHERE tenant_id = :tenant_id AND phone = :phone"
)


def inicio_de_conversa(tenant_id: str, phone: str) -> bool:
    """Ha menos de ``JULGAMENTOS_SEM_JUIZ`` julgamentos gravados deste paciente.

    Falha de banco devolve ``False``: o juiz roda como antes, nunca deixa de julgar por erro.
    """
    try:
        with Session(get_engine(get_settings().DATABASE_URL)) as session:
            anteriores = session.execute(
                _JULGAMENTOS_ANTERIORES_SQL, {"tenant_id": tenant_id, "phone": phone}
            ).scalar_one()
    except Exception:
        logger.warning("contagem de julgamentos falhou; juiz roda", exc_info=True)
        return False
    return int(anteriores) < JULGAMENTOS_SEM_JUIZ


class StatusWorkflowInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    tenant_id: str = Field(min_length=1)
    turn_id: str = Field(min_length=1)
    phone: str = Field(min_length=1)
    remote_jid: str = Field(min_length=1)
    instance: str = Field(min_length=1)
    last_sequence: int = Field(ge=1)
    history: list[tuple[Literal["customer", "assistant"], str]] = Field(
        default_factory=list
    )
    current_lead: str = Field(min_length=1, max_length=4_096)
    # 13/out/2026: turnos auditados SEM resposta do agente (supressao por
    # controle humano ou turno trivial) enviam "" — ver _start_status_sem_resposta.
    agent_reply: str = Field(default="", max_length=2_048)
    draft_action: str = Field(min_length=1, max_length=64)
    audit_model: str = Field(default="genius-next-default", min_length=1, max_length=128)
    # 20/out/2026: voto do agente principal (so tenant em STATUS_AGENT_*).
    # Default None mantem payloads antigos (sem o campo) validos.
    agent_signal: AgentStatusSignal | None = None
    # 23/out/2026: fatos da conversa (sombra). Default vazio mantem payloads antigos validos.
    fatos: list[FatoConversa] = Field(default_factory=list, max_length=8)
    # 27/ago/2026: needs_human EXISTIA aqui, era coletado e validado, e
    # nunca chegava a apply_status_txn -- a SQL que este workflow aplica
    # (_STATUS_CLIENT_APPLY_SQL) nao tem esse parametro. Achado por duas
    # revisoes externas (Kimi K3 e GLM-5.3), verificado lendo a SQL real.
    # Removido em vez de religado: o caminho sincrono (build_decision_and_
    # commands, antes deste workflow assincrono existir) ja resolve
    # crew na hora certa -- este campo era redundante por desenho,
    # nao uma funcionalidade faltando.


def status_workflow_id(tenant_id: str, turn_id: str) -> str:
    return stable_id("status", tenant_id, turn_id)


def build_status_workflow_input(
    turn: ClaimedTurn,
    history: HistoryResult,
    draft: ReplyDraft | None = None,
    *,
    phone: str | None = None,
    audit_model: str | None = None,
) -> StatusWorkflowInput:
    """Monta o payload do audit. ``draft=None`` = turno auditado sem resposta
    do agente (controle humano ou supressao trivial): agent_reply vazio e
    draft_action "sem_resposta"."""
    customer_texts = [
        (event.text_content or "").strip()
        for event in turn.events
        if event.direction == "inbound" and (event.text_content or "").strip()
    ]
    if not customer_texts:
        raise ValueError("status classification requires customer text")

    serialized_history = [
        (message.role, message.text) for message in history.messages if message.text.strip()
    ]
    resolved_phone = phone or getattr(turn, "phone", None)
    if not resolved_phone:
        raise ValueError("status classification requires normalized phone")

    return StatusWorkflowInput(
        tenant_id=turn.tenant_id,
        turn_id=turn.turn_id,
        phone=resolved_phone,
        remote_jid=turn.remote_jid,
        instance=turn.instance,
        last_sequence=turn.last_sequence,
        history=serialized_history,
        current_lead="\n".join(customer_texts),
        agent_reply=draft.text.strip() if draft is not None else "",
        draft_action=draft.action if draft is not None else "sem_resposta",
        audit_model=audit_model or "genius-next-default",
        agent_signal=draft.status_signal if draft is not None else None,
        fatos=list(draft.fatos) if draft is not None else [],
    )


def run_status_step(payload: StatusWorkflowInput) -> StatusResult:
    settings = get_settings()
    status_input = StatusInput(
        history=payload.history,
        current_lead=payload.current_lead,
        agent_reply=payload.agent_reply,
    )
    http = httpx.Client(timeout=settings.REPLY_TIMEOUT_SECONDS)
    embed = httpx.Client(timeout=EMBED_SCORE_TIMEOUT_SECONDS)
    try:
        return classify_status(
            status_input,
            settings=settings,
            http=http,
            embed_client=embed,
            model_override=payload.audit_model,
            agent_signal=payload.agent_signal,
            vote=settings.status_agent_vote_enabled(payload.tenant_id),
            record_evidence=settings.status_agent_enabled(payload.tenant_id),
            inicio_de_conversa=inicio_de_conversa(payload.tenant_id, payload.phone),
        )
    finally:
        http.close()
        embed.close()


def status_workflow(payload: StatusWorkflowInput) -> StatusResult:
    result = run_status_step(payload)
    apply_status_txn(
        payload.tenant_id,
        payload.turn_id,
        payload.phone,
        result,
        payload.audit_model,
        source_sequence=payload.last_sequence,
    )
    fatos = fatos_do_turno(payload, get_settings())
    if fatos:
        _gravar_fatos(payload, fatos)
    return result


def fatos_do_turno(payload: StatusWorkflowInput, settings: object) -> list[FatoConversa]:
    """Fatos do agente (sombra no tenant de teste) ou, se a flag de regra estiver ligada, derivados por regra."""
    if payload.fatos:
        return list(payload.fatos)
    if settings.fatos_regra_enabled(payload.tenant_id):  # type: ignore[attr-defined]
        return derivar_fatos(payload.current_lead, payload.agent_reply, payload.draft_action)
    return []


def _gravar_fatos(payload: StatusWorkflowInput, fatos: list[FatoConversa]) -> None:
    """Sombra: gravar fatos nunca derruba o status (tabela ausente, erro de banco)."""
    try:
        record_conversation_facts_txn(
            payload.tenant_id,
            payload.phone,
            payload.turn_id,
            [(f.tipo, f.detalhe) for f in fatos],
        )
    except Exception:  # noqa: BLE001 - sombra: so o registro dos fatos se perde
        logger.warning("fatos nao gravados para %s", payload.turn_id, exc_info=True)


@jobs.task(name="status_audit", queue=AUX_QUEUE)
def status_audit_job(payload: dict) -> None:
    status_workflow(StatusWorkflowInput.model_validate(payload))
