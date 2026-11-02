"""Texto e regras das ferramentas da agenda que a Julia chama (23/out/2026).

Separado do registro da ferramenta para testar sem LLM. So devolve horarios que existem em
``agenda_slots`` e so confirma o que ``reservar`` gravou: nada e inventado.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.state.agenda import Horario, horarios_livres, reservar

FUSO = ZoneInfo("America/Fortaleza")
_DIAS = ("segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo")


def formatar_horario(h: Horario) -> str:
    local = h.inicio.astimezone(FUSO)
    quem = f" com {h.profissional}" if h.profissional else ""
    return f"ID {h.slot_id}: {_DIAS[local.weekday()]} {local:%d/%m} às {local:%H:%M}{quem}"


def listar(session: Any, *, tenant_id: str, servico: str, agora: datetime, limite: int = 4) -> str:
    horarios = horarios_livres(session, tenant_id=tenant_id, servico=servico.strip(), a_partir=agora, limite=max(1, min(limite, 6)))
    if not horarios:
        return "SEM_HORARIOS: nao ha horario livre cadastrado para este servico; a equipe confirma (use handoff)."
    return "HORARIOS_REAIS (ofereca no maximo dois destes, nenhum outro):\n" + "\n".join(formatar_horario(h) for h in horarios)


def reservar_horario(session: Any, *, tenant_id: str, phone: str, slot_id: int, agora: datetime) -> str:
    if not phone:
        return "INDISPONIVEL: paciente nao identificado; use handoff."
    appt = reservar(session, tenant_id=tenant_id, phone=phone, slot_id=slot_id, agora=agora, origem="julia")
    if appt is None:
        return "INDISPONIVEL: esse horario esgotou ou nao existe; chame listar_horarios de novo e ofereca outro."
    return f"RESERVADO: agendamento {appt} confirmado no Genius. Informe o dia e o horario ao paciente; a equipe e avisada."
