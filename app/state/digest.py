"""Digest diario das conversas em que a clinica deve resposta ao paciente.

Funcao pura (sem banco, sem LLM, sem envio): recebe a varredura e o plano e devolve o
texto curto para o OPERADOR (WhatsApp do dono do Genius, nunca o paciente). Privacidade:
so os 4 ultimos digitos do telefone e a idade; nenhum texto de paciente, nenhum nome.
"""

from __future__ import annotations

from datetime import datetime

from app.state.plan import Acao
from app.state.scan import LinhaEstado

LIMITE_LISTA = 10
DIAS_ANTIGA = 3
FRASE_FINAL = "Responder essas conversas é a recuperação de venda mais direta."


def _idade(minutos: int) -> str:
    if minutos < 60:
        return f"há {max(minutos, 1)} min"
    if minutos < 24 * 60:
        return f"há {minutos // 60} h"
    dias = minutos // (24 * 60)
    return f"há {dias} dia" if dias == 1 else f"há {dias} dias"


def _final4(phone: str) -> str:
    digitos = "".join(c for c in phone if c.isdigit())
    return digitos[-4:] if digitos else "????"


def montar_digest(
    linhas: list[LinhaEstado], plano: list[Acao], agora: datetime, nome_tenant: str
) -> str:
    """Texto do digest; "" quando nao ha divida (nada e enviado)."""
    por_phone = {linha.phone: linha for linha in linhas}
    dividas: list[tuple[int, str]] = []
    for acao in plano:
        if acao.acao != "responder_divida":
            continue
        minutos = acao.minutos
        linha = por_phone.get(acao.phone)
        if minutos is None and linha is not None:
            minutos = linha.minutos_desde_ultima
        dividas.append((minutos or 0, acao.phone))
    if not dividas:
        return ""

    dividas.sort(key=lambda d: (-d[0], d[1]))
    total = len(dividas)
    antigas = sum(1 for m, _ in dividas if m >= DIAS_ANTIGA * 24 * 60)

    esperando = "1 conversa esperando" if total == 1 else f"{total} conversas esperando"
    resumo = f"{esperando} resposta da clínica."
    if antigas:
        resumo += f" {antigas} há mais de {DIAS_ANTIGA} dias."
    topo = dividas[:LIMITE_LISTA]
    titulo = "A mais antiga:" if len(topo) == 1 else f"As {len(topo)} mais antigas:"
    corpo = "\n".join(f"• …{_final4(p)} — {_idade(m)}" for m, p in topo)
    return "\n".join([f"📋 {nome_tenant} — {agora:%d/%m}", resumo, "", titulo, corpo, "", FRASE_FINAL])
