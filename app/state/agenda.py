"""Agenda do Genius: horarios disponiveis e reserva atomica (23/out/2026).

A reserva e a prova de `agendado`: a Julia so oferece o que esta em ``horarios_livres`` e so
considera agendado o que ``reservar`` confirmou. Tudo roda na transacao do chamador; a
capacidade e garantida por um UPDATE condicional (sem corrida, sem overbooking).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text

_ABRIR_SQL = text(
    """
    /* agenda.abrir */
    INSERT INTO genius_next.agenda_slots (tenant_id, servico, profissional, inicio, fim, capacidade, origem, externo_id)
    VALUES (:tenant_id, :servico, :profissional, :inicio, :fim, :capacidade, :origem, :externo_id)
    ON CONFLICT (tenant_id, servico, profissional, inicio) DO NOTHING
    RETURNING id
    """
)

_LIVRES_SQL = text(
    """
    /* agenda.livres */
    SELECT id, servico, profissional, inicio, fim, capacidade - reservados AS vagas
    FROM genius_next.agenda_slots
    WHERE tenant_id = :tenant_id AND ativo
      AND (lower(servico) = lower(:servico)
           OR lower(servico) LIKE '%' || lower(:servico) || '%'
           OR lower(:servico) LIKE '%' || lower(servico) || '%')
      AND reservados < capacidade AND inicio > :a_partir
    ORDER BY inicio
    LIMIT :limite
    """
)

_EXISTE_SQL = text(
    """
    /* agenda.reserva.existente */
    SELECT a.id FROM genius_next.appointments a
    WHERE a.tenant_id = :tenant_id AND a.phone = :phone AND a.slot_id = :slot_id
      AND a.status <> 'cancelado'
    """
)

_TOMAR_SQL = text(
    """
    /* agenda.reserva.tomar */
    UPDATE genius_next.agenda_slots
    SET reservados = reservados + 1
    WHERE id = :slot_id AND tenant_id = :tenant_id AND ativo
      AND reservados < capacidade AND inicio > :agora
    RETURNING servico, inicio
    """
)

_APPT_SQL = text(
    """
    /* agenda.reserva.appointment */
    INSERT INTO genius_next.appointments (tenant_id, phone, servico, inicio, status, origem, criado_por, slot_id)
    VALUES (:tenant_id, :phone, :servico, :inicio, 'agendado', :origem, :criado_por, :slot_id)
    ON CONFLICT (tenant_id, phone, inicio, servico) DO UPDATE SET status = 'agendado', slot_id = :slot_id, updated_at = now()
    RETURNING id
    """
)

_CANCELAR_SQL = text(
    """
    /* agenda.cancelar */
    UPDATE genius_next.appointments SET status = 'cancelado', updated_at = now()
    WHERE tenant_id = :tenant_id AND phone = :phone AND slot_id = :slot_id AND status <> 'cancelado'
    RETURNING id
    """
)

_LIBERAR_SQL = text(
    """
    /* agenda.liberar */
    UPDATE genius_next.agenda_slots SET reservados = greatest(reservados - 1, 0)
    WHERE id = :slot_id AND tenant_id = :tenant_id
    """
)


@dataclass(frozen=True)
class Horario:
    slot_id: int
    servico: str
    profissional: str
    inicio: datetime
    fim: datetime
    vagas: int


def abrir_horario(
    session: Any,
    *,
    tenant_id: str,
    servico: str,
    inicio: datetime,
    fim: datetime,
    profissional: str = "",
    capacidade: int = 1,
    origem: str = "manual",
    externo_id: str | None = None,
) -> int | None:
    """Id do slot novo; None se ja existia (idempotente no sync)."""
    row = session.execute(
        _ABRIR_SQL,
        {
            "tenant_id": tenant_id, "servico": servico, "profissional": profissional, "inicio": inicio,
            "fim": fim, "capacidade": capacidade, "origem": origem, "externo_id": externo_id,
        },
    ).first()
    return int(row[0]) if row else None


def horarios_livres(
    session: Any, *, tenant_id: str, servico: str, a_partir: datetime, limite: int = 6
) -> list[Horario]:
    rows = session.execute(
        _LIVRES_SQL, {"tenant_id": tenant_id, "servico": servico, "a_partir": a_partir, "limite": limite}
    )
    return [Horario(r.id, r.servico, r.profissional, r.inicio, r.fim, r.vagas) for r in rows]


def reservar(
    session: Any,
    *,
    tenant_id: str,
    phone: str,
    slot_id: int,
    agora: datetime,
    origem: str = "julia",
    criado_por: str = "",
) -> int | None:
    """Id do agendamento, ou None se o horario esgotou/passou/nao existe.

    Idempotente: reservar de novo o mesmo horario para o mesmo paciente devolve o mesmo
    agendamento sem consumir outra vaga.
    """
    ja = session.execute(_EXISTE_SQL, {"tenant_id": tenant_id, "phone": phone, "slot_id": slot_id}).first()
    if ja is not None:
        return int(ja[0])
    tomado = session.execute(_TOMAR_SQL, {"slot_id": slot_id, "tenant_id": tenant_id, "agora": agora}).first()
    if tomado is None:
        return None
    appt = session.execute(
        _APPT_SQL,
        {
            "tenant_id": tenant_id, "phone": phone, "servico": tomado.servico, "inicio": tomado.inicio,
            "origem": origem, "criado_por": criado_por, "slot_id": slot_id,
        },
    ).one()
    return int(appt[0])


def cancelar(session: Any, *, tenant_id: str, phone: str, slot_id: int) -> bool:
    """Cancela e devolve a vaga; False se nao havia agendamento ativo (replay)."""
    if session.execute(_CANCELAR_SQL, {"tenant_id": tenant_id, "phone": phone, "slot_id": slot_id}).first() is None:
        return False
    session.execute(_LIBERAR_SQL, {"slot_id": slot_id, "tenant_id": tenant_id})
    return True
