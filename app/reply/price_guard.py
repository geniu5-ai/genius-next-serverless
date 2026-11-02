"""Vigia determinístico de valores: a resposta só pode citar R$ que o modelo viu.

Regra simples, sem LLM e sem lista de preços: todo valor em reais que aparece na
resposta precisa existir em algum texto da ENTRADA do modelo (prompt do negócio,
catálogo inline, conversa, histórico e resumo). Valor que o modelo "calculou" ou
inventou não existe na entrada e a resposta vira handoff, sem preço. É preferível
passar a conversa para a equipe a arriscar um valor que a clínica nunca disse.

Só vale quando o catálogo inteiro está na entrada (estratégia ``inline``) e o
modelo não tem ferramenta que traga texto novo (busca na web, raciocínio): fora
disso a entrada não é a fonte completa e o vigia não opina.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# R$ 149,90 | R$149 | R$ 1.200,00 | R$1.200
_RS = re.compile(r"R\$\s?(\d{1,3}(?:\.\d{3})+|\d+)(?:,(\d{1,2}))?")
# "149 reais", "1.200,50 reais"
_REAIS = re.compile(r"\b(\d{1,3}(?:\.\d{3})+|\d+)(?:,(\d{1,2}))?\s+reais\b", re.IGNORECASE)

VALOR_NAO_CONFIRMADO_TEXT = "Vou confirmar o valor certo com a equipe e já te retorno por aqui."


def _canon(inteiro: str, centavos: str | None) -> str:
    return f"{int(inteiro.replace('.', ''))}.{(centavos or '').ljust(2, '0')}"


def valores(texto: str) -> set[str]:
    """Valores em reais do texto, em forma canônica (``149.90``, ``1200.00``)."""
    achados = {_canon(m.group(1), m.group(2)) for m in _RS.finditer(texto or "")}
    achados |= {_canon(m.group(1), m.group(2)) for m in _REAIS.finditer(texto or "")}
    return achados


def valores_fora_da_entrada(resposta: str, fontes: Iterable[str]) -> list[str]:
    """Valores citados na ``resposta`` que não existem em nenhuma das ``fontes``."""
    citados = valores(resposta)
    if not citados:
        return []
    vistos: set[str] = set()
    for fonte in fontes:
        vistos |= valores(fonte)
    return sorted(citados - vistos)
