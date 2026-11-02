"""Vigia de orfaos (30/out/2026): paciente que ficou sem resposta e ninguem viu.

So LEITURA: nenhuma escrita, nenhum LLM, nenhum envio ao paciente. Para cada conversa
cuja ultima fala do paciente (dentro da janela) nao teve resposta depois, diz POR QUE
ficou sem resposta, cruzando o turno que cobriu a mensagem, o outbox e o dono da conversa.

Resposta = mensagem humana da equipe depois da fala OU linha do outbox ``sent`` com
proposito ``reply``. NAO contam: linha ``failed``/``uncertain``/``recorded`` (a varredura
de pendentes conta qualquer linha do outbox; aqui nao), aviso de handoff ("vou chamar a
equipe") e mensagem de espera do fallback: avisam, mas nao respondem.

Silencio intencional nao e orfao: turno ``suppressed_trivial`` (so "obrigado", emoji)
e cliente ``block``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

from app.durable.persistence import HUMAN_TEAM_OUTBOUND_PREDICATE

_SEM_RESPOSTA_SQL = text(
    f"""
    /* state.orfaos.sem_resposta */
    WITH ult_in AS (
        SELECT DISTINCT ON (e.tenant_id, e.remote_jid)
               e.tenant_id, e.remote_jid, e.phone, e.received_at AS in_em,
               e.conversation_sequence AS in_seq
        FROM genius_next.events e
        WHERE e.direction = 'inbound' AND e.event_type = 'messages.upsert'
          AND e.received_at > now() - make_interval(hours => :janela_h)
        ORDER BY e.tenant_id, e.remote_jid, e.received_at DESC
    )
    SELECT u.tenant_id, u.remote_jid, u.phone, u.in_em,
           EXISTS (SELECT 1 FROM genius_next.events e
                    WHERE e.tenant_id = u.tenant_id AND e.remote_jid = u.remote_jid
                      AND e.received_at > u.in_em
                      AND {HUMAN_TEAM_OUTBOUND_PREDICATE}) AS teve_saida,
           EXISTS (SELECT 1 FROM genius_next.outbox ob
                    WHERE ob.tenant_id = u.tenant_id AND ob.destination_jid = u.remote_jid
                      AND ob.kind = 'customer' AND ob.delivery_status = 'sent'
                      AND ob.payload->>'purpose' = 'reply'
                      AND ob.created_at > u.in_em) AS teve_envio,
           (SELECT count(*) FROM genius_next.outbox ob
             WHERE ob.tenant_id = u.tenant_id AND ob.destination_jid = u.remote_jid
               AND ob.kind = 'customer' AND ob.delivery_status IN ('failed', 'uncertain')
               AND ob.created_at > u.in_em) AS envios_falhos,
           t.status AS turno_status, t.decision->>'action' AS turno_acao,
           c.status AS status_crm, c.automation_state, c.crew_in_at
    FROM ult_in u
    LEFT JOIN LATERAL (
        SELECT t.status, t.decision
        FROM genius_next.turns t
        WHERE t.tenant_id = u.tenant_id AND t.remote_jid = u.remote_jid
          AND u.in_seq BETWEEN t.first_sequence AND t.last_sequence
        ORDER BY t.created_at DESC
        LIMIT 1
    ) t ON true
    LEFT JOIN genius_next.clients c ON c.tenant_id = u.tenant_id AND c.phone = u.phone
    WHERE u.in_em < now() - make_interval(mins => :espera_min)
    """
)

_AGENDA_VENCIDA_SQL = text(
    """
    /* state.orfaos.agenda_vencida */
    SELECT tenant_id, phone, kind, due_at
    FROM genius_next.manager_items
    WHERE state = 'open' AND due_at IS NOT NULL
      AND due_at < now() - make_interval(mins => :espera_min)
    """
)

_CICLO_EM_REVISAO_SQL = text(
    """
    /* state.orfaos.ciclo_em_revisao */
    SELECT tenant_id, phone, updated_at
    FROM genius_next.follow_up_lifecycles
    WHERE state = 'review_required'
      AND updated_at > now() - make_interval(hours => :janela_h)
    """
)

# Motivo -> frase curta para o alerta. A ordem e a de gravidade.
MOTIVOS = {
    "sem_turno": "mensagem nunca virou turno",
    "turno_falhou": "turno falhou",
    "turno_preso": "turno parado sem terminar",
    "envio_falhou": "resposta nao foi entregue",
    "respondeu_sem_envio": "turno decidiu responder mas nada saiu",
    "equipe_deve": "conversa com a equipe e paciente esperando",
    "sem_motivo": "sem resposta, motivo nao identificado",
}


@dataclass(frozen=True)
class Orfao:
    tenant_id: str
    phone: str
    motivo: str
    minutos: int
    status_crm: str | None


def classificar(row: Any, agora: datetime) -> Orfao | None:
    """Devolve o orfao ou None quando a falta de resposta e legitima."""
    if row.teve_saida or row.teve_envio:
        return None
    if row.turno_acao == "suppressed_trivial" or row.status_crm == "block":
        return None
    minutos = int((agora - row.in_em).total_seconds() // 60)
    if row.automation_state == "crew" or row.turno_acao in ("suppressed_by_control", "handoff"):
        motivo = "equipe_deve"
    elif row.envios_falhos:
        motivo = "envio_falhou"
    elif row.turno_status is None:
        motivo = "sem_turno"
    elif row.turno_status == "failed":
        motivo = "turno_falhou"
    elif row.turno_status != "completed":
        motivo = "turno_preso"
    elif row.turno_acao == "send_text":
        motivo = "respondeu_sem_envio"
    else:
        motivo = "sem_motivo"
    return Orfao(row.tenant_id, row.phone, motivo, minutos, row.status_crm)


@dataclass(frozen=True)
class Relatorio:
    orfaos: list[Orfao]
    agenda_vencida: list[Any]
    ciclos_em_revisao: list[Any]

    @property
    def vazio(self) -> bool:
        return not (self.orfaos or self.agenda_vencida or self.ciclos_em_revisao)


def vigiar(
    session: Any, *, janela_h: int = 48, espera_min: int = 5, agora: datetime | None = None
) -> Relatorio:
    agora = agora or datetime.now(timezone.utc)
    params = {"janela_h": janela_h, "espera_min": espera_min}
    linhas = session.execute(_SEM_RESPOSTA_SQL, params).all()
    orfaos = [o for o in (classificar(r, agora) for r in linhas) if o is not None]
    orfaos.sort(key=lambda o: (list(MOTIVOS).index(o.motivo), -o.minutos))
    agenda = session.execute(_AGENDA_VENCIDA_SQL, {"espera_min": espera_min}).all()
    ciclos = session.execute(_CICLO_EM_REVISAO_SQL, {"janela_h": janela_h}).all()
    return Relatorio(orfaos, list(agenda), list(ciclos))


def _idade(minutos: int) -> str:
    if minutos < 120:
        return f"{minutos} min"
    if minutos < 48 * 60:
        return f"{minutos // 60} h"
    return f"{minutos // 1440} d"


def montar_texto(
    rel: Relatorio, rotulo: Any = lambda t: t, contato: Any = lambda p: p
) -> str:
    """Texto do alerta ao operador. ``rotulo`` troca o tenant_id pelo nome; ``contato`` mascara o telefone."""
    partes = ["Vigia de orfaos: paciente sem resposta"]
    por_motivo: dict[str, list[Orfao]] = {}
    for o in rel.orfaos:
        por_motivo.setdefault(o.motivo, []).append(o)
    for motivo, lista in por_motivo.items():
        partes.append(f"\n{MOTIVOS[motivo]} ({len(lista)}):")
        for o in lista[:8]:
            partes.append(f"- {rotulo(o.tenant_id)} {contato(o.phone)} ha {_idade(o.minutos)} [{o.status_crm or '-'}]")
        if len(lista) > 8:
            partes.append(f"- e mais {len(lista) - 8}")
    if rel.agenda_vencida:
        partes.append(f"\nagenda vencida sem executar ({len(rel.agenda_vencida)}):")
        for a in rel.agenda_vencida[:8]:
            partes.append(f"- {rotulo(a.tenant_id)} {contato(a.phone)} {a.kind}")
    if rel.ciclos_em_revisao:
        partes.append(f"\nfollow-up parado em revisao ({len(rel.ciclos_em_revisao)}):")
        for c in rel.ciclos_em_revisao[:8]:
            partes.append(f"- {rotulo(c.tenant_id)} {contato(c.phone)}")
    return "\n".join(partes)
