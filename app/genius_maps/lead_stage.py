"""Atualiza a etapa de um lead em ``genius_maps.leads`` a partir da conversa do
reply_agent (tenant google).

Integracao fina e best-effort: rodada depois que a resposta de Ricardo ja foi
gravada/despachada (app/durable/flow.py), dentro do job do turno, num passo que
NUNCA levanta excecao -- qualquer falha de banco/log e so log.

Mapeia a classificacao ja existente do Next (app.status.rules + app.reply.etapas)
em vez de criar classificador novo:
  - opt_out  : o cliente pediu parada (detect_status_by_rule == "loss" e is_opt_out)
    -- etapa terminal, nunca reabordada.
  - perdido  : recusa sem pedir parada (detect_status_by_rule == "loss").
  - ganho    : confirmacao explicita (detect_status_by_rule == "win").
  - negociando: interesse / pergunta de preco (etapa preco | fechamento).
  - respondeu: 1a resposta (lead em qualificado/abordado respondeu).
"""
from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.durable.tenant_registry import get_registry_engine
from app.reply.etapas import etapa_do_turno
from app.settings import get_settings
from app.status.rules import detect_status_by_rule, is_opt_out

logger = logging.getLogger(__name__)

_LEAD_TENANT = "google"

# Ordem do funil: nunca retrocede uma etapa. ganho/perdido/opt_out sao
# terminais (ordem 4) e portanto imutaveis a partir do momento em que entram.
_ORDEM_ESTAGIO: dict[str, int] = {
    "qualificado": 0,
    "abordado": 1,
    "respondeu": 2,
    "negociando": 3,
    "ganho": 4,
    "perdido": 4,
    "opt_out": 4,
}


def _candidate_stage(
    customer_text: str, has_history: bool, current_estagio: str | None
) -> str | None:
    """Deriva a nova etapa da classificacao existente do Next (sem novo modelo).

    Reusa ``detect_status_by_rule`` (classificacao de status, camada
    determinista usada tambem por app.status.conversation_audit) e
    ``etapa_do_turno`` (classe de intent). Nao inventa classificador.
    """
    if not customer_text or not customer_text.strip():
        return None
    rule = detect_status_by_rule(customer_text)
    if rule == "win":
        return "ganho"
    if rule == "loss":
        return "opt_out" if is_opt_out(customer_text) else "perdido"
    etapa = etapa_do_turno(customer_text, tem_historico=has_history)
    if etapa in ("preco", "fechamento"):
        return "negociando"
    if current_estagio in ("qualificado", "abordado"):
        return "respondeu"
    return None


def _apply_lead_stage(
    tenant_id: str,
    phone: str,
    customer_text: str,
    *,
    has_history: bool,
) -> None:
    """Best-effort: avanca a etapa do lead. Nunca levanta excecao."""
    if tenant_id != _LEAD_TENANT or not phone or not customer_text.strip():
        return
    try:
        settings = get_settings()
        with Session(get_registry_engine(settings.DATABASE_URL)) as session:
            atual = session.execute(
                text("SELECT estagio FROM genius_maps.leads WHERE telefone_e164 = :p"),
                {"p": phone},
            ).scalar()
            if atual is None:
                return  # lead nao esta em genius_maps (ainda) -- nada a fazer
            candidato = _candidate_stage(customer_text, has_history, atual)
            if candidato is None:
                return
            if _ORDEM_ESTAGIO.get(candidato, 99) <= _ORDEM_ESTAGIO.get(atual, 0):
                return  # nao retrocede o funil (opt_out/ganho/perdido mantidos)
            session.execute(
                text(
                    "UPDATE genius_maps.leads "
                    "SET estagio = :e, atualizado_em = now() "
                    "WHERE telefone_e164 = :p"
                ),
                {"e": candidato, "p": phone},
            )
            session.commit()
            logger.info("lead_stage %s: %s -> %s", phone, atual, candidato)
    except Exception:
        logger.warning(
            "lead_stage update falhou para %s/%s", tenant_id, phone, exc_info=True
        )


def advance_lead_stage_step(
    tenant_id: str,
    phone: str,
    customer_text: str,
    *,
    has_history: bool,
) -> None:
    """Avanco de etapa do lead (best-effort, nunca levanta)."""
    return _apply_lead_stage(
        tenant_id, phone, customer_text, has_history=has_history
    )
