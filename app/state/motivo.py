"""Motivo do silencio do paciente (23/out/2026): regex sobre as ultimas falas, sem LLM.

O motivo escolhe o template e o angulo do follow-up. Regex serve ao que e explicito e barato; na duvida
cai em ``sumiu_sem_motivo`` (o mais neutro). Funcao pura.
"""

from __future__ import annotations

import re
import unicodedata

MOTIVOS = (
    "sumiu_sem_motivo", "pediu_preco_e_calou", "adiou", "esperando_equipe",
    "objecao_nao_resolvida", "agendou_sem_confirmar", "recusou_leve",
)

_PRECO = re.compile(r"\b(quanto|valor|preco|custa|custo|cobra|sai por|fica quanto)\b")
_OBJECAO = re.compile(r"\b(caro|pesado|nao tenho como|sem condicoes|muito alto|nao cabe|desconto|parcel)\b")
_RECUSA_LEVE = re.compile(r"\b(acho que nao|nao sei se|nao vou conseguir|talvez depois|nao e pra mim)\b")
_AGENDA = re.compile(r"\b(segunda|terca|quarta|quinta|sexta|sabado|domingo|amanha|\d{1,2}\s*(h|:)\s*\d{0,2})\b")
_PROMESSA_EQUIPE = re.compile(r"\b(confirmar com a equipe|ja te retorno|equipe (confirma|retorna|verifica))\b")


def _norm(texto: str) -> str:
    s = unicodedata.normalize("NFKD", texto.lower())
    return "".join(c for c in s if not unicodedata.combining(c))


def motivo_do_silencio(
    fatos: tuple[str, ...], ultima_do_paciente: str = "", ultima_da_julia: str = ""
) -> str:
    """Primeira regra que casar decide; precedencia do que e mais especifico."""
    if "adiamento" in fatos or "pedido_retorno" in fatos:
        return "adiou"
    julia = _norm(ultima_da_julia)
    paciente = _norm(ultima_do_paciente)
    if "promessa" in fatos or _PROMESSA_EQUIPE.search(julia):
        return "esperando_equipe"
    if "agendamento" in fatos or (_AGENDA.search(julia) and "?" in ultima_da_julia):
        return "agendou_sem_confirmar"
    if "recusa" in fatos or _RECUSA_LEVE.search(paciente):
        return "recusou_leve"
    if _OBJECAO.search(paciente):
        return "objecao_nao_resolvida"
    if _PRECO.search(paciente) or (re.search(r"r\$\s*\d", julia) and not paciente):
        return "pediu_preco_e_calou"
    return "sumiu_sem_motivo"
