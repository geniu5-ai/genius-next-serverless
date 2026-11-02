"""Prova de venda do Genius: agendamento e pagamento registrados (23/out/2026).

Idempotente e sem efeito colateral sobre status: so grava a prova. O estado e o painel leem
daqui; quem muda o status do cliente e decisao separada (win por prova, ver
docs/specs/vendedor-manager-prompts.md).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import text

_APPT_SQL = text(
    """
    /* prova.appointment.upsert */
    INSERT INTO genius_next.appointments (tenant_id, phone, servico, inicio, status, origem, externo_id, criado_por)
    VALUES (:tenant_id, :phone, :servico, :inicio, :status, :origem, :externo_id, :criado_por)
    ON CONFLICT (tenant_id, phone, inicio, servico) DO UPDATE
        SET status = EXCLUDED.status, updated_at = now()
        WHERE genius_next.appointments.status <> EXCLUDED.status
    RETURNING id
    """
)

_PAY_SQL = text(
    """
    /* prova.payment.insert */
    INSERT INTO genius_next.payments
        (tenant_id, phone, valor_centavos, metodo, status, referencia, origem, confirmado_em)
    VALUES (:tenant_id, :phone, :valor, :metodo, :status, :referencia, :origem,
            CASE WHEN :status = 'confirmado' THEN now() END)
    ON CONFLICT (tenant_id, referencia) WHERE referencia IS NOT NULL DO UPDATE
        SET status = EXCLUDED.status,
            confirmado_em = CASE WHEN EXCLUDED.status = 'confirmado'
                                 THEN COALESCE(genius_next.payments.confirmado_em, now()) END
        WHERE genius_next.payments.status <> EXCLUDED.status
    RETURNING id
    """
)


def registrar_agendamento(
    session: Any,
    *,
    tenant_id: str,
    phone: str,
    inicio: datetime,
    servico: str = "",
    status: str = "agendado",
    origem: str = "manual",
    externo_id: str | None = None,
    criado_por: str = "",
) -> bool:
    """True se criou ou mudou o status; False se ja era exatamente isso (replay)."""
    return (
        session.execute(
            _APPT_SQL,
            {
                "tenant_id": tenant_id, "phone": phone, "servico": servico, "inicio": inicio,
                "status": status, "origem": origem, "externo_id": externo_id, "criado_por": criado_por,
            },
        ).first()
        is not None
    )


def registrar_pagamento(
    session: Any,
    *,
    tenant_id: str,
    phone: str,
    valor_centavos: int,
    referencia: str | None,
    status: str = "confirmado",
    metodo: str = "pix",
    origem: str = "manual",
) -> bool:
    """Idempotente por (tenant, referencia): o mesmo txid nunca vira dois pagamentos."""
    return (
        session.execute(
            _PAY_SQL,
            {
                "tenant_id": tenant_id, "phone": phone, "valor": valor_centavos, "metodo": metodo,
                "status": status, "referencia": referencia, "origem": origem,
            },
        ).first()
        is not None
    )
