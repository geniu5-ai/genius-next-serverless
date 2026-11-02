"""Varredura de estado (23/out/2026): le as conversas e calcula o estado de cada uma.

Parte de LEITURA do tick por estado (docs/specs/vendedor-manager-prompts.md): nenhuma
escrita, nenhum LLM, nenhum envio. Devolve, por conversa, o estado projetado dos fatos
e o que o banco ja sabe (quem falou por ultimo e ha quanto tempo). O tick que age vem
depois, em cima desta mesma funcao.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

from app.durable.persistence import HUMAN_TEAM_OUTBOUND_PREDICATE
from app.state.projection import Entrada, Estado, estado_da_conversa

_SCAN_SQL = text(
    f"""
    /* state.scan */
    WITH ult AS (
        SELECT DISTINCT ON (e.phone)
               e.phone, e.direction, e.received_at
        FROM genius_next.events e
        WHERE e.tenant_id = :tenant_id
        ORDER BY e.phone, e.received_at DESC
    ),
    fatos AS (
        SELECT f.phone, array_agg(f.tipo ORDER BY f.created_at, f.ordinal) AS tipos
        FROM (
            SELECT phone, tipo, created_at, ordinal,
                   row_number() OVER (PARTITION BY phone ORDER BY created_at DESC, ordinal DESC) AS rn
            FROM genius_next.conversation_facts
            WHERE tenant_id = :tenant_id
        ) f
        WHERE f.rn <= 10
        GROUP BY f.phone
    )
    SELECT c.phone, c.status, c.automation_state, c.crew_in_at,
           u.direction AS ultima_direcao, u.received_at AS ultima_em,
           COALESCE(fa.tipos, ARRAY[]::text[]) AS tipos,
           EXISTS (SELECT 1 FROM genius_next.events e
                    WHERE e.tenant_id = c.tenant_id AND e.phone = c.phone
                      AND {HUMAN_TEAM_OUTBOUND_PREDICATE}
                      AND e.received_at > (SELECT max(f2.created_at) FROM genius_next.conversation_facts f2
                                            WHERE f2.tenant_id = c.tenant_id AND f2.phone = c.phone
                                              AND f2.tipo IN ('promessa', 'pergunta_aberta'))) AS humano_depois,
           EXISTS (SELECT 1 FROM genius_next.payments p
                    WHERE p.tenant_id = c.tenant_id AND p.phone = c.phone AND p.status = 'confirmado') AS pago,
           EXISTS (SELECT 1 FROM genius_next.appointments a
                    WHERE a.tenant_id = c.tenant_id AND a.phone = c.phone
                      AND a.status IN ('agendado', 'confirmado', 'compareceu')) AS agendado
    FROM genius_next.clients c
    LEFT JOIN ult u ON u.phone = c.phone
    LEFT JOIN fatos fa ON fa.phone = c.phone
    WHERE c.tenant_id = :tenant_id
    """
)


@dataclass(frozen=True)
class LinhaEstado:
    phone: str
    estado: Estado
    ultima_fala: str
    minutos_desde_ultima: int | None
    status_crm: str


def linha_para_entrada(row: Any, agora: datetime) -> Entrada:
    ultima = (
        "paciente"
        if row.ultima_direcao == "inbound"
        else "julia"
        if row.ultima_direcao == "outbound"
        else "nenhuma"
    )
    return Entrada(
        fatos=tuple(row.tipos or ()),
        ultima_fala=ultima,  # type: ignore[arg-type]
        automacao=row.automation_state if row.automation_state in ("open", "crew", "loss", "block") else "open",
        humano_depois_da_divida=bool(getattr(row, "humano_depois", False)),
        pagamento_confirmado=bool(getattr(row, "pago", False)),
        agendamento_registrado=bool(getattr(row, "agendado", False)),
        handoff_aberto_desde=row.crew_in_at,
        agora=agora,
    )


def varrer_estados(session: Any, tenant_id: str, agora: datetime | None = None) -> list[LinhaEstado]:
    agora = agora or datetime.now(timezone.utc)
    linhas: list[LinhaEstado] = []
    for row in session.execute(_SCAN_SQL, {"tenant_id": tenant_id}):
        entrada = linha_para_entrada(row, agora)
        minutos = (
            int((agora - row.ultima_em).total_seconds() // 60) if row.ultima_em is not None else None
        )
        linhas.append(
            LinhaEstado(
                phone=row.phone,
                estado=estado_da_conversa(entrada),
                ultima_fala=entrada.ultima_fala,
                minutos_desde_ultima=minutos,
                status_crm=row.status,
            )
        )
    return linhas
