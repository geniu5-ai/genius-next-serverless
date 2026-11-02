"""Ferramentas da agenda do Genius para a Julia: listar_horarios e reservar_horario.

So aparecem para o modelo em tenants de ``AGENDA_TOOL_TENANTS`` (padrao vazio: nada muda).
Horario so existe se estiver em ``agenda_slots``; a reserva e atomica e idempotente.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.tools import ToolDefinition
from sqlalchemy.orm import Session

from app.reply.agent import ReplyDeps, reply_agent
from app.settings import get_settings
from app.state.agenda_tools import listar, reservar_horario


def _so_com_agenda(ctx: RunContext[ReplyDeps], tool_def: ToolDefinition) -> ToolDefinition | None:
    return tool_def if get_settings().agenda_tool_enabled(ctx.deps.tenant_id) else None


@contextmanager
def _sessao() -> Iterator[Any]:
    from app.durable.tenant_registry import get_registry_engine

    with Session(get_registry_engine(get_settings().DATABASE_URL)) as ses, ses.begin():
        yield ses


@reply_agent.tool(prepare=_so_com_agenda)
def listar_horarios(ctx: RunContext[ReplyDeps], servico: str, limite: int = 4) -> str:
    """Lista horarios REAIS livres de um servico (consulta, exame, vacina...). Chame antes de
    oferecer qualquer dia ou horario; nunca ofereca um horario que esta ferramenta nao devolveu."""
    with _sessao() as ses:
        return listar(ses, tenant_id=ctx.deps.tenant_id, servico=servico, agora=datetime.now(timezone.utc), limite=limite)


@reply_agent.tool(prepare=_so_com_agenda)
def reservar_horario_tool(ctx: RunContext[ReplyDeps], slot_id: int) -> str:
    """Reserva para o paciente o horario de ID `slot_id` (devolvido por listar_horarios) depois que
    ele ACEITOU esse horario. So considere agendado o que devolver RESERVADO."""
    with _sessao() as ses:
        return reservar_horario(
            ses, tenant_id=ctx.deps.tenant_id, phone=ctx.deps.phone, slot_id=slot_id, agora=datetime.now(timezone.utc)
        )
