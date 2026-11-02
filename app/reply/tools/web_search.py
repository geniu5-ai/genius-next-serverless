"""Ferramenta ``pesquisar_web`` do reply_agent — busca Exa sob demanda, opt-in por tenant.

Motivo (ver comentrio em app/reply/agent.py): tools custam ~3k tokens de entrada
e ida ao provedor. Busca web so rende quando a pergunta exige dado externo
(mercado, artigo, concorrente, "como funciona X no Google"). Por isso a tool:
  - e oferecida SO para tenants com flag `web_search=true` no tenant_registry
    (migration-069; google liberado); o `prepare` abaixo e o portao.
  - tem teto de 1 consulta por turno (ReplyDeps.web_search_consultas),
    seguindo o mesmo padrao de ReplyDeps.consultas (catalogo).
  - timeout de 4s; resultado resumido (titulo + trecho, <=3 itens, ~1200 chars).
  - falha do Exa devolve texto neutro e NUNCA quebra o turno.
"""
from __future__ import annotations

import logging

import httpx
from pydantic_ai import RunContext
from pydantic_ai.tools import ToolDefinition

from app.reply.agent import ReplyDeps, reply_agent
from app.settings import get_settings

logger = logging.getLogger(__name__)

_MAX_RESULTS = 3
_MAX_RESUMO_CHARS = 1200
_TIMEOUT_SEGUNDOS = 4.0
_EXA_ENDPOINT = "https://api.exa.ai/search"

_NEUTRO_FALHA = "Não consegui encontrar informações atualizadas sobre isso agora."
_LIMITE_MSG = "Limite de buscas por turno atingido."


def _so_para_web_search(
    ctx: RunContext[ReplyDeps], tool_def: ToolDefinition
) -> ToolDefinition | None:
    """Oferta a tool só para tenants com web_search habilitado no registry."""
    return tool_def if ctx.deps.web_search else None


def fetch_exa_search(
    query: str, *, api_key: str, timeout: float = _TIMEOUT_SEGUNDOS
) -> list[dict[str, str]]:
    """HTTP direto ao Exa (POST /search). Levanta em qualquer falha.

    Normaliza a resposta bruta em uma lista de ``{"title", "snippet"}`` para
    as primeiras ``_MAX_RESULTS`` ocorrencias. A excecao sobe para
    ``pesquisar_web`` tratar — aqui nao ha fallback, para ficar testavel
    por mock (veja tests/test_web_search_tool.py).
    """
    resposta = httpx.post(
        _EXA_ENDPOINT,
        headers={"x-api-key": api_key, "content-type": "application/json"},
        json={
            "query": query,
            "type": "auto",
            "numResults": _MAX_RESULTS,
            "contents": {"highlights": True},
        },
        timeout=timeout,
    )
    resposta.raise_for_status()
    dados = resposta.json()
    itens: list[dict[str, str]] = []
    for item in dados.get("results", [])[:_MAX_RESULTS]:
        titulo = item.get("title") or item.get("name") or ""
        trecho = ""
        highlights = item.get("highlights") or []
        if isinstance(highlights, list) and highlights:
            primeiro = highlights[0]
            trecho = primeiro.get("text", "") if isinstance(primeiro, dict) else str(primeiro)
        if not trecho:
            trecho = item.get("summary", "") or ""
        itens.append({"title": titulo, "snippet": trecho})
    return itens


def _resumir(itens: list[dict[str, str]]) -> str:
    """Formata titulo + trecho (<=3 itens) em texto plano, cortado em ~1200 chars."""
    blocos: list[str] = []
    for item in itens[:_MAX_RESULTS]:
        titulo = (item.get("title") or "").strip()
        trecho = (item.get("snippet") or "").strip()
        if trecho:
            blocos.append(f"- {titulo}: {trecho}")
        elif titulo:
            blocos.append(f"- {titulo}")
    texto = "\n".join(blocos).strip() or _NEUTRO_FALHA
    if len(texto) > _MAX_RESUMO_CHARS:
        texto = texto[: _MAX_RESUMO_CHARS - 1].rstrip() + "…"
    return texto


@reply_agent.tool(prepare=_so_para_web_search)
def pesquisar_web(ctx: RunContext[ReplyDeps], query: str) -> str:
    """Pesquisa na web sobre um tema usando Exa e devolve um resumo curto.

    - query: o que pesquisar (frase curta ou longa).
    Uma busca por turno no maximo (contada em ``ctx.deps.web_search_consultas``).
    Exa offline / sem internet / sem chave devolve texto neutro e nao quebra
    o turno — o modelo continua sem inventar dados fora da ferramenta.
    """
    deps = ctx.deps
    deps.web_search_consultas.append(query)
    if len(deps.web_search_consultas) > get_settings().WEB_SEARCH_MAX_CONSULTAS:
        return _LIMITE_MSG

    settings = get_settings()
    if settings.EXA_API_KEY is None:
        return _NEUTRO_FALHA
    api_key = settings.EXA_API_KEY.get_secret_value()
    if not api_key:
        return _NEUTRO_FALHA

    try:
        itens = fetch_exa_search(query, api_key=api_key)
    except Exception:
        logger.warning("pesquisar_web falhou para %r", query, exc_info=True)
        return _NEUTRO_FALHA
    return _resumir(itens)
