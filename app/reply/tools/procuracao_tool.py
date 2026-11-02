"""Registra criar_procuracao e verificar_procuracao_assinada como reply_agent.tools.

Tenants habilitados: claudiolopes, genius (teste).
"""
from __future__ import annotations

import logging

from pydantic_ai import RunContext
from pydantic_ai.tools import ToolDefinition

from app.reply.agent import ReplyDeps, reply_agent
from app.tools.procuracao import (
    GENERO_REQUERIDO,
    criar_link_procuracao,
    lookup_cpf,
    lookup_cep,
    nome_parece_valido,
    verificar_assinatura_por_slug,
)

logger = logging.getLogger(__name__)


_PROCURACAO_TENANTS = frozenset({
    "claudiolopes",
    "genius",
    "d0b00bad-814d-4be4-835c-5762c9ac374d",  # genius prod UUID (Cláudio Lopes)
})


def _so_para_claudiolopes(ctx: RunContext[ReplyDeps], tool_def: ToolDefinition) -> ToolDefinition | None:
    return tool_def if ctx.deps.tenant_id in _PROCURACAO_TENANTS else None


@reply_agent.tool(prepare=_so_para_claudiolopes)
def criar_procuracao(
    ctx: RunContext[ReplyDeps],
    tipo: str,
    cpf: str,
    cep: str = "",
    nome_cliente: str = "",
) -> str:
    """Gera procuração e retorna link de assinatura DocuSeal.

    - tipo: inss_beneficio | inss_rural | bpc_loas | gestante | financiamento_invalidez
    - cpf: CPF do cliente (só números ou formatado)
    - cep: opcional — se o cliente informou o CEP, preenche endereço automaticamente
    - nome_cliente: passar SOMENTE se o nome da API tiver emojis/símbolos ou o cliente
      tiver confirmado um nome diferente

    Retorna URL https://docuseal.com/s/<slug> ou código de erro:
      CPF_NAO_LOCALIZADO | NOME_INVALIDO | CEP_INVALIDO | GENERO_INCOMPATIVEL | PROCURACAO_INDISPONIVEL
    """
    # 1. Busca dados do CPF
    try:
        dados_cpf = lookup_cpf(cpf)
    except Exception:
        logger.warning("lookup_cpf falhou para %.6s", cpf, exc_info=True)
        return "CPF_NAO_LOCALIZADO"

    # 1b. Valida compatibilidade de gênero (ex.: gestante exige CPF de mulher)
    genero_requerido = GENERO_REQUERIDO.get(tipo)
    if genero_requerido and dados_cpf.get("genero") != genero_requerido:
        logger.warning(
            "genero incompativel para %.6s: tipo=%s requer=%s cpf_genero=%s",
            cpf, tipo, genero_requerido, dados_cpf.get("genero"),
        )
        return "GENERO_INCOMPATIVEL"

    # 2. Resolve nome: preferência para nome_cliente confirmado pelo usuário
    nome_api = dados_cpf.get("nome_completo", "")
    if nome_cliente and nome_parece_valido(nome_cliente):
        nome_final = nome_cliente.strip().title()
    elif nome_parece_valido(nome_api):
        nome_final = nome_api
    else:
        logger.info("nome da API inválido para %.6s: %r", cpf, nome_api)
        return "NOME_INVALIDO"

    # 3. Resolve endereço via ViaCEP (opcional)
    endereco = cidade = estado = ""
    if cep:
        try:
            dados_cep = lookup_cep(cep)
            endereco = dados_cep.get("endereco", "")
            cidade   = dados_cep.get("cidade", "")
            estado   = dados_cep.get("estado", "")
        except Exception:
            logger.warning("lookup_cep falhou para %s", cep, exc_info=True)
            return "CEP_INVALIDO"

    lead_data = {
        "nome_completo":  nome_final,
        "cpf":            cpf,
        "rg":             "",
        "orgao_rg":       "",
        "data_nascimento": dados_cpf.get("data_nascimento", ""),
        "estado_civil":   "",
        "profissao":      "",
        "endereco":       endereco,
        "cidade":         cidade,
        "estado":         estado,
        "genero":         dados_cpf.get("genero", "M"),
    }

    try:
        url = criar_link_procuracao(tipo, lead_data)
        logger.info("procuracao gerada para %.6s tipo=%s cep=%s", cpf, tipo, bool(cep))
        return url
    except Exception:
        logger.warning("criar_link_procuracao falhou tipo=%s", tipo, exc_info=True)
        return "PROCURACAO_INDISPONIVEL"


@reply_agent.tool(prepare=_so_para_claudiolopes)
def verificar_procuracao_assinada(ctx: RunContext[ReplyDeps], slug: str) -> str:
    """Verifica se o cliente já assinou a procuração enviada.

    slug: slug ou URL completa do link enviado ao cliente
    Retorna: ASSINADO | PENDENTE | NAO_ENCONTRADO
    """
    try:
        return verificar_assinatura_por_slug(slug)
    except Exception:
        logger.warning("verificar_assinatura_por_slug falhou slug=%.12s", slug, exc_info=True)
        return "PENDENTE"
