"""Escolha de template de follow-up com rodizio e limite por paciente (23/out/2026).

So escolhe e registra; nao envia. O envio (e a aprovacao da Meta) sao de outra etapa. Regras:
somente templates `aprovado`; nunca repetir um template que o paciente ja recebeu; no maximo um
template por paciente em `intervalo_dias`; entre os elegiveis, o menos usado recentemente.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text

_ESCOLHER_SQL = text(
    """
    /* templates.escolher */
    SELECT t.id, t.nome, t.texto, t.categoria,
           (SELECT count(*) FROM genius_next.follow_template_uses u
             WHERE u.template_id = t.id AND u.usado_em > :agora - interval '30 days') AS usos_recentes
    FROM genius_next.follow_templates t
    WHERE t.tenant_id = :tenant_id AND t.motivo = :motivo AND t.status = 'aprovado'
      AND NOT EXISTS (SELECT 1 FROM genius_next.follow_template_uses u
                       WHERE u.template_id = t.id AND u.tenant_id = :tenant_id AND u.phone = :phone)
      AND NOT EXISTS (SELECT 1 FROM genius_next.follow_template_uses u
                       WHERE u.tenant_id = :tenant_id AND u.phone = :phone
                         AND u.usado_em > :agora - make_interval(days => :intervalo_dias))
    ORDER BY usos_recentes, t.id
    LIMIT 1
    """
)

_USO_SQL = text(
    """
    /* templates.uso */
    INSERT INTO genius_next.follow_template_uses (tenant_id, phone, template_id, usado_em)
    VALUES (:tenant_id, :phone, :template_id, :agora)
    """
)


@dataclass(frozen=True)
class TemplateEscolhido:
    template_id: int
    nome: str
    texto: str
    categoria: str


def escolher_template(
    session: Any, *, tenant_id: str, phone: str, motivo: str, agora: datetime, intervalo_dias: int = 7
) -> TemplateEscolhido | None:
    row = session.execute(
        _ESCOLHER_SQL,
        {"tenant_id": tenant_id, "phone": phone, "motivo": motivo, "agora": agora, "intervalo_dias": intervalo_dias},
    ).first()
    return TemplateEscolhido(row.id, row.nome, row.texto, row.categoria) if row else None


def registrar_uso(session: Any, *, tenant_id: str, phone: str, template_id: int, agora: datetime) -> None:
    session.execute(_USO_SQL, {"tenant_id": tenant_id, "phone": phone, "template_id": template_id, "agora": agora})
