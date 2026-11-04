"""Rota de raciocínio (B): ferramenta ``raciocinar`` + função compartilhada.

Quando o caso pede mais que o catálogo — pergunta fora do catálogo, lista de
exames, indicação, negociação — a atendente invoca ``raciocinar(pergunta)``.
A função monta um prompt denso (doutrina do tenant + catálogo inteiro +
histórico + regras de saída) e chama um modelo mais forte que o do turno
(``MANAGER_REASONING_MODEL``, mesmo provedor do turno), com três tetos:

* ``MANAGER_REASONING_MAX_PER_TURN`` — chamadas por turno (``ReplyDeps.raciocinios``);
* ``MANAGER_REASONING_DAILY_MAX`` — chamadas por tenant por dia
  (``genius_next.manager_usage``, incremento atômico);
* ``MANAGER_REASONING_MAX_TOKENS`` + ``MANAGER_REASONING_TIMEOUT_SECONDS``
  — teto de saída e de espera por chamada.

A busca Exa (``EXA_API_KEY``) entra só quando a pergunta precisa de dado
externo E o tenant tem ``web_search`` liberado — mesma chave de opt-in de
``pesquisar_web``, teto de 1 por chamada.

O texto devolvido é CONTEXTO para a atendente, nunca resposta final: ele
volta para o modelo do turno, cujo draft passa pelas mesmas guardas de saída
de sempre (``guard_assess_step`` em ``app/durable/flow.py``). Aqui não há
segundo caminho de entrega.

Toda falha devolve texto neutro e **nunca quebra o turno** (mesmo contrato de
``pesquisar_web`` e ``abrir``).
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
from pydantic_ai import RunContext
from pydantic_ai.tools import ToolDefinition
from sqlalchemy import text as _sql_text

from app.db import sql_session, transaction
from app.reply.agent import ReplyDeps, reply_agent
from app.settings import get_settings

logger = logging.getLogger(__name__)

NEUTRO = (
    "Não consegui confirmar isso agora. Se quiser, sigo com a equipe para "
    "te trazer a resposta certa."
)
LIMITE_TURNO = "Limite de consultas avançadas por mensagem atingido."
LIMITE_DIA = "Limite de consultas avançadas de hoje atingido."

_TIMEOUT_BUSCA = 4.0

_USO_LEITURA_SQL = _sql_text(
    """
    /* manager.usage_read */
    SELECT calls FROM genius_next.manager_usage
    WHERE tenant_id = :tenant_id AND day = CURRENT_DATE
    """
)
_USO_INCREMENTO_SQL = _sql_text(
    """
    /* manager.usage_inc */
    INSERT INTO genius_next.manager_usage (tenant_id, day, calls)
    VALUES (:tenant_id, CURRENT_DATE, 1)
    ON CONFLICT (tenant_id, day)
    DO UPDATE SET calls = genius_next.manager_usage.calls + 1
    RETURNING calls
    """
)


def _so_para_manager(
    ctx: RunContext[ReplyDeps], tool_def: ToolDefinition
) -> ToolDefinition | None:
    """Esconde ``raciocinar`` de quem não está em MANAGER_AGENT_TENANTS."""
    return tool_def if ctx.deps.manager else None


# --------------------------------------------------------------------------- #
# Tetos diários — chamadas por dia por tenant
# --------------------------------------------------------------------------- #


@transaction(isolation_level="SERIALIZABLE")
def uso_raciocinio_hoje_txn(tenant_id: str) -> int:
    """Chamadas densas já registradas hoje (UTC) para o tenant."""
    row = sql_session().execute(
        _USO_LEITURA_SQL,
        {"tenant_id": tenant_id},
    ).scalar_one_or_none()
    return int(row or 0)


@transaction(isolation_level="SERIALIZABLE")
def registra_uso_raciocinio_txn(tenant_id: str) -> int:
    """Incrementa o contador do dia e devolve o total após o incremento."""
    row = sql_session().execute(
        _USO_INCREMENTO_SQL,
        {"tenant_id": tenant_id},
    ).scalar_one_or_none()
    return int(row or 0)


def catalogo_denso_step(tenant_id: str) -> tuple[tuple[str, str, str], ...]:
    """Catálogo inteiro do tenant para o prompt denso (uma leitura, sem cachê)."""
    from sqlalchemy.orm import Session

    from app.context.knowledge import load_full_catalog
    from app.durable.tenant_registry import get_registry_engine

    settings = get_settings()
    with Session(get_registry_engine(settings.DATABASE_URL)) as session:
        return tuple(
            load_full_catalog(
                session,
                tenant_id=tenant_id,
                max_chars=max(settings.KNOWLEDGE_INLINE_MAX_CHARS, 40_000),
            )
        )


# --------------------------------------------------------------------------- #
# Montagem e chamada (testáveis sem banco e sem rede)
# --------------------------------------------------------------------------- #

_REGRAS_SAIDA = (
    "Voce e a rota de raciocinio do atendimento. Responda APENAS com o texto "
    "que a atendente pode usar com a pessoa, em portugues, tom acolhedor e "
    "curto (ate 6 linhas).",
    "Use somente o catalogo e as informacoes do historico. O que nao estiver "
    "ai, voce nao sabe: diga que vai confirmar com a equipe, nunca invente "
    "valor, horario, disponibilidade, prazo, preparo, convênio nem promessa "
    "de entrega.",
    "Nao cobre, nao crie urgencia nem escassez e nao prometa prazo. Nao fale "
    "de internos, sistemas, senhas ou nomes de equipe.",
)


def monta_prompt_denso(
    *,
    business_prompt: str,
    catalogo: tuple[tuple[str, str, str], ...],
    historico: tuple[tuple[str, str], ...],
    pergunta: str,
    busca_web: tuple[dict[str, str], ...] = (),
) -> tuple[str, str]:
    """(system, user) do chamado denso. Puro: sem banco, sem rede."""
    partes = [business_prompt.strip(), ""]
    if catalogo:
        blocos = "\n\n".join(
            f"[{chave}] {titulo}\n{conteudo}" for chave, titulo, conteudo in catalogo
        )
        partes.append(
            "CATALOGO COMPLETO (unica fonte de dado da empresa):\n" + blocos
        )
        partes.append("")
    partes.extend(_REGRAS_SAIDA)
    if busca_web:
        linhas = "\n".join(
            f"- {item.get('title', '')}: {item.get('snippet', '')}"
            for item in busca_web
        )
        partes.append(
            "Resultados de busca externa (informativo; nao e dado da empresa):\n"
            + linhas
        )
    system = "\n".join(p for p in partes if p)

    user = json.dumps(
        {
            "pergunta": pergunta,
            "historico": [
                {"role": papel, "text": texto} for papel, texto in historico
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return system, user


def chama_modelo_denso(
    system: str,
    user: str,
    *,
    model: str,
    timeout: float,
    max_tokens: int,
    temperature: float = 0.4,
    extra_body: dict[str, Any] | None = None,
    _tentou_fallback: bool = False,
) -> str | None:
    """POST /chat/completions. ``None`` em qualquer falha ou saída truncada.

    HTTP != 200 repete UMA vez com ``OMNIROUTE_MODEL`` (modelo do turno) quando difere de ``model``.

    ``extra_body`` entra no corpo do POST (ex.: ``{"thinking": {"type":
    "disabled"}}`` na triagem, TRAPS #45); padrao ``None`` = corpo de sempre.
    """
    settings = get_settings()
    base = settings.OMNIROUTE_BASE_URL.rstrip("/")
    chave = settings.OMNIROUTE_API_KEY
    try:
        response = httpx.post(
            f"{base}/chat/completions",
            headers={
                "Authorization": f"Bearer {chave.get_secret_value() if chave else ''}",
                "Content-Type": "application/json",
                "x-omniroute-compression": "off",
            },
            json={
                "model": model,
                "temperature": temperature,
                "stream": False,
                "max_tokens": max_tokens,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                **(extra_body or {}),
            },
            timeout=timeout,
        )
        if response.status_code != 200:
            logger.warning("rota densa HTTP %s (modelo %s)", response.status_code, model)
            # 03/nov/2026: o modelo da rota densa saiu do catalogo do roteador (404) e TODA
            # triagem caiu em handoff. Uma vez, no modelo do turno (que responde); as saidas
            # seguem pelas mesmas guardas de quem chama.
            fallback = settings.OMNIROUTE_MODEL
            if fallback and fallback != model and not _tentou_fallback:
                return chama_modelo_denso(
                    system,
                    user,
                    model=fallback,
                    timeout=timeout,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    extra_body=extra_body,
                    _tentou_fallback=True,
                )
            return None
        payload = response.json()
        choice = (payload.get("choices") or [{}])[0]
        if choice.get("finish_reason") != "stop":
            logger.warning("rota densa saiu truncada (%s)", choice.get("finish_reason"))
            return None
        texto = ((choice.get("message") or {}).get("content") or "").strip()
        return texto or None
    except Exception:
        logger.warning("rota densa falhou", exc_info=True)
        return None


def _busca_exa(pergunta: str, *, web_search: bool) -> tuple[dict[str, str], ...]:
    """Exa, só com opt-in do tenant e chave presente. Falha = sem busca."""
    if not web_search:
        return ()
    settings = get_settings()
    if settings.EXA_API_KEY is None or not settings.EXA_API_KEY.get_secret_value():
        return ()
    try:
        from app.reply.tools.web_search import fetch_exa_search

        return tuple(
            fetch_exa_search(
                pergunta,
                api_key=settings.EXA_API_KEY.get_secret_value(),
                timeout=_TIMEOUT_BUSCA,
            )
        )
    except Exception:
        logger.warning("busca exa da rota densa falhou", exc_info=True)
        return ()


def reasoning_route(
    *,
    tenant_id: str,
    pergunta: str,
    business_prompt: str,
    historico: tuple[tuple[str, str], ...] = (),
    web_search: bool = False,
    catalogo: tuple[tuple[str, str, str], ...] | None = None,
    conta_chamada: bool = True,
) -> str | None:
    """Chamada densa completa, com tetos. ``None`` quando não deu.

    ``catalogo`` pré-carregado evita uma leitura de banco quando o chamador já
    tem (turno inline); ``conta_chamada=False`` é só para teste.
    """
    settings = get_settings()
    pergunta = (pergunta or "").strip()
    if not pergunta:
        return None
    if not conta_chamada:
        # Caminho de teste / reuso sem teto diário.
        pass
    else:
        try:
            hoje = uso_raciocinio_hoje_txn(tenant_id)
        except Exception:
            logger.warning("teto diario indisponivel; seguindo sem conta", exc_info=True)
            hoje = 0
        if hoje >= settings.MANAGER_REASONING_DAILY_MAX:
            logger.info(
                "rota densa recusada: teto diario do tenant (%s)", tenant_id
            )
            return None

    if catalogo is None:
        try:
            catalogo = catalogo_denso_step(tenant_id)
        except Exception:
            logger.warning("catalogo denso indisponivel", exc_info=True)
            catalogo = ()

    system, user = monta_prompt_denso(
        business_prompt=business_prompt,
        catalogo=catalogo or (),
        historico=historico,
        pergunta=pergunta,
        busca_web=_busca_exa(pergunta, web_search=web_search),
    )
    texto = chama_modelo_denso(
        system,
        user,
        model=settings.MANAGER_REASONING_MODEL,
        timeout=settings.MANAGER_REASONING_TIMEOUT_SECONDS,
        max_tokens=settings.MANAGER_REASONING_MAX_TOKENS,
    )
    if texto is None:
        return None
    if conta_chamada:
        try:
            registra_uso_raciocinio_txn(tenant_id)
        except Exception:
            logger.warning("falha ao contar uso da rota densa", exc_info=True)
    return texto


# --------------------------------------------------------------------------- #
# Triagem proativa (branch feat/triagem-proativa) — mesma rota densa, saida JSON
# --------------------------------------------------------------------------- #

# Papel padrao do agente de triagem, usado quando o tenant nao tem um prompt
# 'manager_triage' ativo no banco. Colega experiente da recepcao que resolve
# o que a atendente nao resolveu, usando SO o catalogo e as regras do negocio.
DEFAULT_TRIAGE_PROMPT = (
    "Voce e uma colega experiente da recepcao. A atendente nao conseguiu "
    "responder uma pessoa e voce vai tentar resolver antes de acionar a equipe, "
    "usando SOMENTE o catalogo, os precos, os horarios e as regras do negocio "
    "que estao nesta conversa.\n"
    "Momento: a atendente ja acolheu a pessoa e ela ja esperou a sua resposta. "
    "Sua mensagem e a resposta final dessa espera, nao uma nova abertura.\n"
    "Resolve: preco, preparo, endereco, convenio, se faz exame X, qual "
    "especialista atende um sintoma Y — mas so quando essa informacao estiver "
    "no catalogo.\n"
    "NAO resolve (deixe resolvido=false): marcar horario de verdade, confirmar "
    "vaga, dar desconto fora da tabela, resultado de exame, duvida clinica, "
    "reclamacao e pedido para falar com uma pessoa. Na duvida, resolvido=false.\n"
    "Nunca invente valor, horario, nome de medico nem prazo. Se nao estiver no "
    "catalogo, voce nao sabe: nao resolve.\n"
    "Como escreve a resposta_paciente (quando resolver):\n"
    "- traga a resposta COMPLETA agora, com tudo que o catalogo tiver sobre "
    "aquilo (o valor, o preparo, o endereco), em 1 a 3 frases;\n"
    "- nunca cumprimente de novo (a saudacao ja aconteceu) e nunca faca "
    "pergunta de acolhimento antes da resposta: \"e pra voce?\", \"pra quem "
    "e?\", \"qual exame?\" sao proibidos aqui — a pessoa ja foi acolhida e ja "
    "esperou;\n"
    "- no maximo UMA pergunta curta de proximo passo, e so quando faz sentido: "
    "para consulta ou vacina pode terminar com \"quer que eu veja um "
    "horario?\"; para exame ou ultrassom nao tem agendamento — se constar no "
    "catalogo, diga que e so vir com documento (e o pedido do medico, se "
    "tiver), sem oferecer horario;\n"
    "- escreva como a atendente escreveria — calorosa, curta, uma ideia por "
    "mensagem — sem citar equipe, IA, sistema ou robo.\n"
    "Diante de agressividade, ameaca ou suspeita de golpe, nao resolva: deixe "
    "resolvido=false para a equipe agir."
)

# Precedencia: o roteiro da atendente vira so dado (precos, servicos, regras de
# exame, endereco); a conduta dela nao vale na triagem.
_TRIAGE_PRECEDENCIA = (
    "PRECEDENCIA: as instrucoes acima valem sobre todo o material abaixo. O "
    "texto entre <informacoes_da_clinica> e o roteiro da atendente: use-o SO "
    "como fonte de dados (servicos, precos, preparo, regras de exame, "
    "endereco, horarios de funcionamento). As regras de conduta da atendente "
    "NAO valem aqui: nao se apresente, nao diga o nome dela nem da clinica "
    "como abertura, nao acolha de novo e nao pergunte \"e pra voce?\" ou "
    "\"pra quem e?\" antes de responder."
)

# Lembrete final (recencia): o modelo segue melhor a ultima instrucao.
_TRIAGE_LEMBRETE = (
    "Lembrete final: se a resposta esta nos dados acima, resposta_paciente ja "
    "traz a informacao completa (inclusive o valor), sem se apresentar e sem "
    "pergunta antes da resposta."
)

# Instrucao de saida: JSON estrito, sem markdown nem texto ao redor.
_TRIAGE_SAIDA = (
    "Responda EXCLUSIVAMENTE com um JSON valido, sem markdown e sem texto ao "
    'redor, no formato: {"resolvido": true ou false, "resposta_paciente": '
    '"texto que a atendente manda a pessoa", "resumo_para_equipe": "resumo '
    'curto para a equipe"}. resolvido=true so se voce resolver com o catalogo; '
    "resposta_paciente e o texto final para a pessoa (vazio quando "
    "resolvido=false); resumo_para_equipe e um resumo de no maximo 200 "
    "caracteres do caso para a equipe."
)


def monta_prompt_triage(
    *,
    business_prompt: str,
    catalogo: tuple[tuple[str, str, str], ...],
    historico: tuple[tuple[str, str], ...],
    triage_prompt: str,
    pergunta: str,
) -> tuple[str, str]:
    """(system, user) do chamado de triagem. Puro: sem banco, sem rede."""
    partes = [triage_prompt.strip(), ""]
    if business_prompt.strip():
        # O prompt da atendente entra como FONTE DE DADOS, nunca como conduta:
        # medicao de 15/out mostrou as regras dela ("se apresente", "acolha
        # antes do valor", "e pra voce?") vencendo o prompt da triagem quando
        # coladas como instrucao logo depois dele.
        partes.append(_TRIAGE_PRECEDENCIA)
        partes.append("<informacoes_da_clinica>")
        partes.append(business_prompt.strip())
        partes.append("</informacoes_da_clinica>")
        partes.append("")
    if catalogo:
        blocos = "\n\n".join(
            f"[{chave}] {titulo}\n{conteudo}" for chave, titulo, conteudo in catalogo
        )
        partes.append(
            "CATALOGO COMPLETO (unica fonte de dado da empresa):\n" + blocos
        )
        partes.append("")
    partes.append(_TRIAGE_SAIDA)
    partes.append(_TRIAGE_LEMBRETE)
    system = "\n".join(p for p in partes if p)

    user = json.dumps(
        {
            "pergunta": pergunta,
            "historico": [
                {"role": papel, "text": texto} for papel, texto in historico
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return system, user


def triage_route(
    *,
    tenant_id: str,
    business_prompt: str,
    historico: tuple[tuple[str, str], ...] = (),
    pergunta: str,
    catalogo: tuple[tuple[str, str, str], ...] | None = None,
    conta_chamada: bool = True,
) -> str | None:
    """Chamada densa de triagem pedindo JSON. ``None`` quando nao deu.

    Mesma rota densa de ``reasoning_route`` (mesmo modelo forte, mesmo teto
    diario em ``genius_next.manager_usage``, mesmo catalogo), so que com o
    prompt de triagem, os tetos PROPRIOS da triagem
    (``MANAGER_TRIAGE_MAX_TOKENS``/``MANAGER_TRIAGE_TIMEOUT_SECONDS``) e a
    saida em JSON — o texto volta cru, quem valida e o contrato
    ``TriageResolution`` em :mod:`app.contracts`.

    ``conta_chamada=False`` quando quem chama ja checou e vai registrar o teto
    no corpo do workflow: o ``triagem_decisao_step`` chama esta funcao DENTRO
    de um step, e ``@transaction`` dentro de step levanta AssertionError
    (TRAPS: transacao so no corpo do workflow).
    """
    from app.reply.prompt_profiles import active_prompt_for

    settings = get_settings()
    pergunta = (pergunta or "").strip()
    if not pergunta:
        return None
    if conta_chamada:
        try:
            hoje = uso_raciocinio_hoje_txn(tenant_id)
        except Exception:
            logger.warning("teto diario indisponivel; seguindo sem conta", exc_info=True)
            hoje = 0
        if hoje >= settings.MANAGER_REASONING_DAILY_MAX:
            logger.info("triagem recusada: teto diario do tenant (%s)", tenant_id)
            return None

    if catalogo is None:
        try:
            catalogo = catalogo_denso_step(tenant_id)
        except Exception:
            logger.warning("catalogo denso indisponivel", exc_info=True)
            catalogo = ()

    triage_prompt = (
        active_prompt_for(tenant_id, "manager_triage") or DEFAULT_TRIAGE_PROMPT
    )
    system, user = monta_prompt_triage(
        business_prompt=business_prompt,
        catalogo=catalogo or (),
        historico=historico,
        triage_prompt=triage_prompt,
        pergunta=pergunta,
    )
    texto = chama_modelo_denso(
        system,
        user,
        model=settings.MANAGER_REASONING_MODEL,
        timeout=settings.MANAGER_TRIAGE_TIMEOUT_SECONDS,
        max_tokens=settings.MANAGER_TRIAGE_MAX_TOKENS,
        extra_body=(
            None
            if settings.MANAGER_TRIAGE_THINKING
            else {"thinking": {"type": "disabled"}}
        ),
    )
    if texto is None:
        return None
    if conta_chamada:
        try:
            registra_uso_raciocinio_txn(tenant_id)
        except Exception:
            logger.warning("falha ao contar uso da triagem", exc_info=True)
    return texto


@reply_agent.tool(prepare=_so_para_manager)
def raciocinar(ctx: RunContext[ReplyDeps], pergunta: str) -> str:
    """Pergunta avançada que o catalogo nao responde (lista de exames,
    negociacao, indicacao, dado externo). Devolve material para VOCE redigir.

    Uma por mensagem (MANAGER_REASONING_MAX_PER_TURN). Falha ou limite
    devolve texto neutro — nunca quebra a conversa.
    """
    deps = ctx.deps
    deps.raciocinios.append(pergunta)
    if len(deps.raciocinios) > get_settings().MANAGER_REASONING_MAX_PER_TURN:
        return LIMITE_TURNO

    texto = reasoning_route(
        tenant_id=deps.tenant_id,
        pergunta=pergunta,
        business_prompt=deps.business_prompt,
        historico=deps.historico,
        web_search=deps.web_search,
        catalogo=deps.catalogo_inline or None,
    )
    return texto or NEUTRO
