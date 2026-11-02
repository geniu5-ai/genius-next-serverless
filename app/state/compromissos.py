"""Compromissos (30/out/2026): o tempo vira dado, numa linha com prazo, dono e geracao.

Fase 1 = SOMBRA, so ``resposta_devida``: toda fala do paciente abre (ou renova) o
compromisso de responder, na MESMA transacao que grava o evento (se ficasse para
depois, um crash entre os dois passos seria justamente o abandono que a regra existe
para pegar). Quem fecha e a reconciliacao por prova, a mesma regra do vigia de orfaos:
mensagem humana da equipe ou resposta do bot (``purpose='reply'``) depois da ultima
fala. Aviso de handoff e espera do fallback nao fecham.

Regras do upsert de ``resposta_devida`` (um aberto por conversa):
- ``due_at`` fica o MAIS CEDO (LEAST): paciente que escreve a cada 2 min com o bot
  quebrado nao empurra o prazo para frente nunca;
- ``geracao`` e ``ultima_fala_em`` ficam os MAIS NOVOS: fechar exige prova posterior a
  ultima fala (compare-and-set implicito no UPDATE).

Nada aqui manda mensagem nem muda status. O vigia compara os vencidos daqui com os
orfaos dele; quando os dois baterem por semanas, esta tabela vira a fonte do tick.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text

from app.durable.persistence import HUMAN_TEAM_OUTBOUND_PREDICATE

PRAZO_RESPOSTA_MIN = 3

_ABRIR_RESPOSTA_SQL = text(
    """
    /* compromissos.abrir_resposta_devida */
    INSERT INTO genius_next.compromissos (
        tenant_id, phone, remote_jid, instance, provider, kind, dono,
        geracao, ultima_fala_em, due_at
    )
    SELECT :tenant_id, :phone, :remote_jid, :instance, :provider, 'resposta_devida',
           CASE WHEN c.automation_state = 'crew' THEN 'equipe' ELSE 'bot' END,
           :geracao, now(), now() + make_interval(mins => :prazo_min)
    FROM (SELECT 1) AS um
    LEFT JOIN genius_next.clients c ON c.tenant_id = :tenant_id AND c.phone = :phone
    ON CONFLICT (tenant_id, remote_jid, kind) WHERE state = 'aberto'
    DO UPDATE SET
        geracao = GREATEST(compromissos.geracao, EXCLUDED.geracao),
        ultima_fala_em = GREATEST(compromissos.ultima_fala_em, EXCLUDED.ultima_fala_em),
        due_at = LEAST(compromissos.due_at, EXCLUDED.due_at),
        dono = EXCLUDED.dono,
        updated_at = now()
    """
)

# Fecha por prova. O UPDATE avalia a linha atual: se uma fala nova renovou a geracao
# no meio do caminho, a prova tem de ser posterior a ela (compare-and-set).
_RECONCILIAR_SQL = text(
    f"""
    /* compromissos.reconciliar */
    UPDATE genius_next.compromissos AS k
    SET state = CASE WHEN prova.motivo = 'dispensado' THEN 'dispensado' ELSE 'feito' END,
        motivo_fecho = prova.motivo, fechado_em = now(), updated_at = now()
    FROM (
        SELECT k2.id,
               CASE
                 WHEN EXISTS (SELECT 1 FROM genius_next.events e
                               WHERE e.tenant_id = k2.tenant_id AND e.remote_jid = k2.remote_jid
                                 AND e.received_at > k2.ultima_fala_em
                                 AND {HUMAN_TEAM_OUTBOUND_PREDICATE}) THEN 'equipe_respondeu'
                 WHEN EXISTS (SELECT 1 FROM genius_next.outbox ob
                               WHERE ob.tenant_id = k2.tenant_id AND ob.destination_jid = k2.remote_jid
                                 AND ob.kind = 'customer' AND ob.delivery_status = 'sent'
                                 AND ob.payload->>'purpose' = 'reply'
                                 AND ob.created_at > k2.ultima_fala_em) THEN 'bot_respondeu'
                 WHEN EXISTS (SELECT 1 FROM genius_next.turns t
                               WHERE t.tenant_id = k2.tenant_id AND t.remote_jid = k2.remote_jid
                                 AND k2.geracao BETWEEN t.first_sequence AND t.last_sequence
                                 AND t.decision->>'action' = 'suppressed_trivial') THEN 'dispensado'
                 WHEN EXISTS (SELECT 1 FROM genius_next.clients c
                               WHERE c.tenant_id = k2.tenant_id AND c.phone = k2.phone
                                 AND c.status = 'block') THEN 'dispensado'
               END AS motivo
        FROM genius_next.compromissos k2
        WHERE k2.state = 'aberto' AND k2.kind = 'resposta_devida'
    ) AS prova
    WHERE k.id = prova.id AND prova.motivo IS NOT NULL
    """
)

_VENCIDOS_SQL = text(
    """
    /* compromissos.vencidos */
    SELECT tenant_id, phone, kind, dono, due_at
    FROM genius_next.compromissos
    WHERE state = 'aberto' AND due_at < now() - make_interval(mins => :folga_min)
    ORDER BY due_at
    """
)


def abrir_resposta_devida(
    session: Any,
    *,
    tenant_id: str,
    phone: str,
    remote_jid: str,
    instance: str,
    provider: str,
    geracao: int,
) -> None:
    """``now()`` da transacao = ``received_at`` do evento gravado na mesma transacao."""
    session.execute(
        _ABRIR_RESPOSTA_SQL,
        {
            "tenant_id": tenant_id,
            "phone": phone,
            "remote_jid": remote_jid,
            "instance": instance,
            "provider": provider,
            "geracao": geracao,
            "prazo_min": PRAZO_RESPOSTA_MIN,
        },
    )


def reconciliar(session: Any) -> int:
    """Fecha por prova; devolve quantos fechou. Idempotente."""
    return session.execute(_RECONCILIAR_SQL).rowcount or 0


@dataclass(frozen=True)
class Vencido:
    tenant_id: str
    phone: str
    kind: str
    dono: str
    due_at: datetime


def vencidos(session: Any, folga_min: int = 2) -> list[Vencido]:
    rows = session.execute(_VENCIDOS_SQL, {"folga_min": folga_min}).all()
    return [Vencido(r.tenant_id, r.phone, r.kind, r.dono, r.due_at) for r in rows]
