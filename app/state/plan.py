"""Plano de acoes por estado (23/out/2026): o que o tick FARIA, sem enviar nada.

Funcao pura em cima de :class:`app.state.scan.LinhaEstado`. Cadencia do chairman,
ajustada pelos dados de 1.314 conversas humanas e pelo parecer de Mythos e Fable
(docs/specs/vendedor-manager-prompts.md): 1o toque 15-20 min, 2o no dia seguinte, 3o
depois de 2 dias. Divida da clinica (pergunta sem resposta) nao e follow-up: e acao
imediata. ``etapa`` e o numero do toque; o UNIQUE da tabela garante uma vez so.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

from app.state.scan import LinhaEstado

DIVIDA_MIN_MINUTOS = 15  # abaixo disso a Julia/equipe ainda esta no ritmo normal da conversa
FOLLOW_1_MIN = 20
FOLLOW_2_MIN = 20 * 60  # ~ dia seguinte; o horario comercial e checado por quem envia
FOLLOW_3_MIN = 48 * 60
JANELA_24H_MIN = 24 * 60


@dataclass(frozen=True)
class Acao:
    phone: str
    acao: str
    etapa: int
    estado: str
    motivo: str
    minutos: int | None


def planejar(linhas: list[LinhaEstado]) -> list[Acao]:
    plano: list[Acao] = []
    for linha in linhas:
        m = linha.minutos_desde_ultima
        if m is None or linha.status_crm in ("win", "loss", "block"):
            continue
        if linha.estado == "aguardando_clinica":
            if linha.ultima_fala == "paciente" and m >= DIVIDA_MIN_MINUTOS:
                plano.append(Acao(linha.phone, "responder_divida", 0, linha.estado, "paciente falou por ultimo e ninguem respondeu", m))
            elif linha.ultima_fala != "paciente":
                plano.append(Acao(linha.phone, "alertar_equipe", 0, linha.estado, "pendencia da clinica sem resposta", m))
        elif linha.estado == "com_equipe":
            continue
        elif linha.estado == "adiado":
            plano.append(Acao(linha.phone, "retomar_adiamento", 0, linha.estado, "prazo do adiamento a verificar", m))
        elif linha.estado == "aguardando_paciente" and linha.ultima_fala == "julia":
            if m >= FOLLOW_3_MIN:
                plano.append(Acao(linha.phone, "follow_3", 3, linha.estado, "template se a janela de 24h fechou" if m >= JANELA_24H_MIN else "", m))
            elif m >= FOLLOW_2_MIN:
                plano.append(Acao(linha.phone, "follow_2", 2, linha.estado, "dia seguinte, horario comercial", m))
            elif m >= FOLLOW_1_MIN:
                plano.append(Acao(linha.phone, "follow_1", 1, linha.estado, "primeiro toque leve", m))
    return plano


_INSERT_SQL = text(
    """
    /* state.actions.insert */
    INSERT INTO genius_next.state_actions (tenant_id, phone, acao, etapa, estado, motivo, minutos)
    VALUES (:tenant_id, :phone, :acao, :etapa, :estado, :motivo, :minutos)
    ON CONFLICT (tenant_id, phone, acao, etapa) DO NOTHING
    """
)


def registrar(session: Any, tenant_id: str, plano: list[Acao]) -> int:
    """Reserva idempotente: devolve quantas acoes eram NOVAS."""
    novas = 0
    for a in plano:
        novas += session.execute(
            _INSERT_SQL,
            {"tenant_id": tenant_id, "phone": a.phone, "acao": a.acao, "etapa": a.etapa, "estado": a.estado, "motivo": a.motivo, "minutos": a.minutos},
        ).rowcount
    return novas
