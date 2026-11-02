"""Manager de pendências: decide quem abordar e fala com o cliente de novo.

Ordem do chairman (16/out/2026): paciente que escreveu e ficou sem resposta (conversa
presa na equipe, turno suprimido, falha) não pode esperar a próxima mensagem dela. A
cada 2 minutos este varredor procura, nos tenants de ``MANAGER_PENDENTES_TENANTS``,
os contatos cuja ÚLTIMA mensagem não recebeu NENHUMA resposta (nem do bot, nem de
humano da equipe) e entrega cada um ao Manager de triagem
(:mod:`app.durable.manager`, ``kind='triagem'``): ele resolve com o catálogo e
responde, ou escala para a equipe com o aviso humano. Nada novo de decisão aqui: só a
descoberta de quem precisa ser abordado.

Guardas (todas no SQL ou na função pura, sem LLM):
* só conversa devolvida ao bot (``automation_state='open'``) e status ``open``: quem
  está com a equipe fica com a equipe;
* última mensagem com 3 min a 23 h (a janela de 24 h do WhatsApp/Meta fecha em 24 h);
* nenhuma saída depois dela (evento outbound, humano ou bot, nem linha no outbox) e
  nenhum turno em andamento;
* sem item de triagem para o contato nas últimas 6 h (nunca duas abordagens seguidas);
* ``ok``, ``obrigada``, emoji e adiamento ("depois eu vejo") não abrem abordagem;
* no máximo 3 contatos por varredura.

Regras desta casa: a descoberta lê com ``Session`` simples
(``sql_session()`` só existe dentro de ``@transaction``, ``TRAPS.md`` #60); a
criação do item e o disparo do job ficam fora da transação (``TRAPS.md`` #51).
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text as _sql_text
from sqlalchemy.orm import Session

from app.jobs import CRON_QUEUE, jobs
from app.settings import get_settings

logger = logging.getLogger(__name__)

PENDENTES_CRON = "*/30 * * * *"  # cinco campos, UTC: a cada 30 minutos
MAX_POR_VARREDURA = 3
JANELA_META_HORAS = 23
ESPERA_MINIMA_SEGUNDOS = 180
SEM_ABORDAR_HORAS = 6
PENDENTE_KIND = "triagem"

_CANDIDATOS_SQL = _sql_text(
    """
    /* pendentes.candidatos */
    WITH ult AS (
        SELECT DISTINCT ON (e.tenant_id, e.phone)
               e.message_id AS event_id, e.tenant_id, e.phone, e.instance, e.provider,
               e.remote_jid, e.received_at, coalesce(e.text_content, '') AS texto
        FROM genius_next.events AS e
        WHERE e.tenant_id = ANY(:tenants)
          AND e.direction = 'inbound'
          AND e.received_at > now() - make_interval(hours => :janela_h)
        ORDER BY e.tenant_id, e.phone, e.received_at DESC, e.id DESC
    )
    SELECT u.event_id, u.tenant_id, u.phone, u.instance, u.provider, u.remote_jid,
           u.texto, coalesce(em.status, '') AS media_status,
           coalesce(c.name, c.push_name, '') AS nome,
           (SELECT t.id FROM genius_next.turns AS t
             WHERE t.tenant_id = u.tenant_id AND t.remote_jid = u.remote_jid
             ORDER BY t.created_at DESC LIMIT 1) AS origin_turn_id
    FROM ult AS u
    JOIN genius_next.clients AS c
      ON c.tenant_id = u.tenant_id AND c.phone = u.phone
    LEFT JOIN genius_next.event_media AS em
      ON em.tenant_id = u.tenant_id AND em.provider = u.provider
     AND em.message_id = u.event_id
    WHERE (u.texto <> '' OR em.status = 'failed')
      AND c.automation_state = 'open'
      AND c.status = 'open'
      AND u.received_at < now() - make_interval(secs => :espera_s)
      AND NOT EXISTS (
          SELECT 1 FROM genius_next.events AS o
          WHERE o.tenant_id = u.tenant_id AND o.phone = u.phone
            AND o.direction = 'outbound' AND o.received_at > u.received_at
      )
      AND NOT EXISTS (
          SELECT 1 FROM genius_next.outbox AS ob
          WHERE ob.tenant_id = u.tenant_id AND ob.destination_jid = u.remote_jid
            AND ob.kind IN ('customer', 'team') AND ob.created_at > u.received_at
      )
      AND NOT EXISTS (
          SELECT 1 FROM genius_next.turns AS t
          WHERE t.tenant_id = u.tenant_id AND t.remote_jid = u.remote_jid
            AND t.status NOT IN ('completed', 'failed')
      )
      AND NOT EXISTS (
          SELECT 1 FROM genius_next.manager_items AS p
          WHERE p.tenant_id = u.tenant_id AND p.phone = u.phone
            AND p.kind = 'triagem'
            AND p.created_at > now() - make_interval(hours => :sem_abordar_h)
            -- 16/out: so bloqueia se a abordagem veio DEPOIS da ultima fala (caso Maria:
            -- escalada 19h20, escreveu de novo 19h21 e ficou sem resposta)
            AND p.created_at > u.received_at
      )
    ORDER BY u.received_at
    LIMIT :limite
    """
)

_FALAS_SQL = _sql_text(
    """
    /* pendentes.falas */
    SELECT coalesce(text_content, '')
    FROM genius_next.events
    WHERE tenant_id = :tenant_id AND phone = :phone AND direction = 'inbound'
      AND received_at > now() - interval '2 hours'
    ORDER BY received_at DESC, id DESC
    LIMIT 4
    """
)


def pendentes_tenants() -> list[str]:
    """Tenants com o agente de pendências ligado (CSV; vazio = desligado)."""
    raw = getattr(get_settings(), "MANAGER_PENDENTES_TENANTS", "") or ""
    return sorted({t.strip() for t in raw.split(",") if t.strip()})


def _leitura(tenants: list[str], limite: int) -> list[dict[str, Any]]:
    from app.durable.tenant_registry import get_registry_engine

    with Session(get_registry_engine(get_settings().DATABASE_URL)) as session:
        rows = (
            session.execute(
                _CANDIDATOS_SQL,
                {
                    "tenants": tenants,
                    "janela_h": JANELA_META_HORAS,
                    "espera_s": ESPERA_MINIMA_SEGUNDOS,
                    "sem_abordar_h": SEM_ABORDAR_HORAS,
                    "limite": limite,
                },
            )
            .mappings()
            .all()
        )
        saida: list[dict[str, Any]] = []
        for row in rows:
            falas = [
                str(f)
                for f in session.execute(
                    _FALAS_SQL,
                    {"tenant_id": row["tenant_id"], "phone": row["phone"]},
                ).scalars()
                if str(f).strip()
            ]
            falas.reverse()
            saida.append({**dict(row), "event_id": str(row["event_id"]), "falas": falas})
        return saida


def listar_pendentes_step(tenants: list[str], limite: int) -> list[dict[str, Any]]:
    return _leitura(tenants, limite)


def deve_abordar(candidato: dict[str, Any]) -> bool:
    """Filtro de conteúdo: só aborda quem fez uma pergunta ou pedido de verdade."""
    from app.status.rules import is_deferral_or_closing, is_trivial_no_reply

    texto = str(candidato.get("texto") or "").strip()
    if not texto:
        return bool(candidato.get("media_status") == "failed" and candidato.get("origin_turn_id"))
    if is_trivial_no_reply(texto) or is_deferral_or_closing(texto):
        return False
    return bool(candidato.get("origin_turn_id"))


def processar_pendentes(candidatos: list[dict[str, Any]]) -> list[str]:
    """Registra e dispara a triagem de cada pendente. Devolve os item_keys criados.

    Falha de um contato nunca impede os demais nem derruba a varredura.
    """
    from app.durable.manager import (
        register_manager_item_txn,
        start_assist_workflow,
    )

    criados: list[str] = []
    for c in candidatos:
        if not deve_abordar(c):
            continue
        try:
            question = f"pendente:{c['event_id']}"
            payload = {
                "draft_original": "",
                "causa": "promovido",
                "ultimas_falas": list(c.get("falas") or [c["texto"]])[-4:],
                "nome": str(c.get("nome") or ""),
                "origem": "pendentes",
            }
            item_key = register_manager_item_txn(
                tenant_id=c["tenant_id"],
                phone=c["phone"],
                remote_jid=c["remote_jid"],
                instance=c["instance"],
                provider=c["provider"],
                origin_turn_id=str(c["origin_turn_id"]),
                kind=PENDENTE_KIND,
                question=question,
                payload=payload,
                due_minutes=1,
            )
            if not item_key:  # já registrado (replay): nunca duas vezes
                continue
            start_assist_workflow(
                item_key,
                tenant_id=c["tenant_id"],
                remote_jid=c["remote_jid"],
                phone=c["phone"],
                instance=c["instance"],
                provider=c["provider"],
                kind=PENDENTE_KIND,
                question=question,
                origin_turn_id=str(c["origin_turn_id"]),
                delay_seconds=0,
            )
            criados.append(item_key)
            logger.info(
                "pendentes: abordando contato ...%s (tenant %s)",
                str(c["phone"])[-4:],
                str(c["tenant_id"])[:8],
            )
        except Exception:
            logger.warning(
                "pendentes: falha ao abordar contato ...%s", str(c.get("phone"))[-4:], exc_info=True
            )
    return criados


@jobs.periodic(cron=PENDENTES_CRON)
@jobs.task(name="pendentes_sweep", queue=CRON_QUEUE)
def pendentes_sweep_scheduled(timestamp: int) -> None:
    tenants = pendentes_tenants()
    if not tenants:
        return
    candidatos = listar_pendentes_step(tenants, MAX_POR_VARREDURA)
    if candidatos:
        processar_pendentes(candidatos)
