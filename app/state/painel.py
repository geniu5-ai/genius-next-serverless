"""Painel de conversao por tenant (23/out/2026): numeros que se mostram a um cliente.

Somente leitura, sem LLM. Mede atendimento (quanto demora a 1a resposta, quantos leads
ficam sem resposta) e funil de status. Agendado/pago/compareceu entram quando a agenda e o Pix
do Genius gravarem a prova (docs/specs/vendedor-manager-prompts.md): hoje o painel mostra o que o
banco ja prova e diz o que ainda nao mede.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

_RESPOSTA_SQL = text(
    """
    /* painel.resposta */
    WITH p AS (
        SELECT phone, min(received_at) FILTER (WHERE direction = 'inbound') AS primeira_msg
        FROM genius_next.events
        WHERE tenant_id = :tenant_id AND received_at > now() - make_interval(days => :dias)
        GROUP BY phone
    ),
    r AS (
        SELECT p.phone, p.primeira_msg,
               (SELECT min(e.received_at) FROM genius_next.events e
                 WHERE e.tenant_id = :tenant_id AND e.phone = p.phone
                   AND e.direction = 'outbound' AND e.received_at >= p.primeira_msg) AS primeira_resposta
        FROM p WHERE p.primeira_msg IS NOT NULL
    )
    SELECT count(*) AS leads,
           count(primeira_resposta) AS respondidos,
           percentile_cont(0.5) WITHIN GROUP (
               ORDER BY extract(epoch FROM primeira_resposta - primeira_msg) / 60.0) AS mediana_min,
           percentile_cont(0.9) WITHIN GROUP (
               ORDER BY extract(epoch FROM primeira_resposta - primeira_msg) / 60.0) AS p90_min
    FROM r
    """
)

_STATUS_SQL = text(
    """
    /* painel.status */
    SELECT status, count(*) AS n FROM genius_next.clients WHERE tenant_id = :tenant_id GROUP BY status
    """
)


_FUNIL_SQL = text(
    """
    /* painel.funil */
    SELECT
      (SELECT count(DISTINCT phone) FROM genius_next.appointments
        WHERE tenant_id = :tenant_id AND created_at > now() - make_interval(days => :dias)
          AND status <> 'cancelado') AS agendados,
      (SELECT count(DISTINCT phone) FROM genius_next.payments
        WHERE tenant_id = :tenant_id AND status = 'confirmado'
          AND confirmado_em > now() - make_interval(days => :dias)) AS pagos,
      (SELECT coalesce(sum(valor_centavos), 0) FROM genius_next.payments
        WHERE tenant_id = :tenant_id AND status = 'confirmado'
          AND confirmado_em > now() - make_interval(days => :dias)) AS pago_centavos,
      (SELECT count(DISTINCT phone) FROM genius_next.appointments
        WHERE tenant_id = :tenant_id AND status = 'compareceu'
          AND updated_at > now() - make_interval(days => :dias)) AS compareceram
    """
)


def painel(session: Any, tenant_id: str, dias: int = 30) -> dict[str, Any]:
    r = session.execute(_RESPOSTA_SQL, {"tenant_id": tenant_id, "dias": dias}).one()
    status = {row.status: row.n for row in session.execute(_STATUS_SQL, {"tenant_id": tenant_id})}
    f = session.execute(_FUNIL_SQL, {"tenant_id": tenant_id, "dias": dias}).one()
    leads = int(r.leads or 0)
    respondidos = int(r.respondidos or 0)
    return {
        "dias": dias,
        "leads": leads,
        "respondidos": respondidos,
        "taxa_resposta_pct": round(100 * respondidos / leads) if leads else None,
        "mediana_primeira_resposta_min": round(float(r.mediana_min), 1) if r.mediana_min is not None else None,
        "p90_primeira_resposta_min": round(float(r.p90_min), 1) if r.p90_min is not None else None,
        "status": status,
        "agendados": int(f.agendados),
        "pagos": int(f.pagos),
        "pago_reais": round(int(f.pago_centavos) / 100, 2),
        "compareceram": int(f.compareceram),
        # Sem nenhum registro de prova o funil ainda nao esta instrumentado neste tenant.
        "nao_mede_ainda": [] if (f.agendados or f.pagos or f.compareceram) else ["agendado", "pago", "compareceu"],
    }
