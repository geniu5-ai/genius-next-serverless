"""Guardrails de saida: checagem local (flow) e guardrail do agente (pydantic-ai-harness)."""

from __future__ import annotations

import json

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai_harness.guardrails import OutputBlocked

from app.reply.agent import AgentReply, _run_agent
from app.reply.guardrails import VALOR_RETRIES, sem_segredos
from app.security.guard import GuardDisposition, assess_locally, contains_secret

CHAVE_FALSA = "sk-proj-" + "a1B2c3D4e5F6g7H8i9J0" * 3
CATALOGO = (
    ("produto:protese_flexivel", "Protese flexivel", "A partir de R$ 1.500, em ate 10x."),
)


# --------------------------------------------------------------------------- #
# Checagem local (substitui o sidecar llm-guard)
# --------------------------------------------------------------------------- #
def test_saida_com_segredo_bloqueia():
    avaliacao = assess_locally("output", [f"Segue a chave {CHAVE_FALSA}"])
    assert avaliacao.disposition == GuardDisposition.BLOCK.value
    assert avaliacao.findings[0].reason_code == "secret_in_output"
    # o achado nunca carrega o texto avaliado
    assert CHAVE_FALSA not in avaliacao.model_dump_json()


def test_saida_normal_passa_e_nunca_fica_indisponivel():
    avaliacao = assess_locally("output", ["A protese flexivel comeca em R$ 1.500."])
    assert avaliacao.disposition == GuardDisposition.ALLOW.value
    assert avaliacao.deterministic is True


def test_entrada_nao_procura_segredo():
    # Cliente colando uma chave nao e motivo de bloqueio na entrada.
    avaliacao = assess_locally("input", [f"minha chave e {CHAVE_FALSA}"])
    assert avaliacao.disposition == GuardDisposition.ALLOW.value


def test_texto_invisivel_bloqueia_nos_dois_estagios():
    texto = "oi​tudo bem"
    for estagio in ("input", "output"):
        avaliacao = assess_locally(estagio, [texto])
        assert avaliacao.disposition == GuardDisposition.BLOCK.value
        assert avaliacao.stage == estagio


def test_contains_secret():
    assert contains_secret(CHAVE_FALSA)
    assert not contains_secret("Atendemos de segunda a sexta, das 8h as 18h.")
    assert not contains_secret("")


# --------------------------------------------------------------------------- #
# Guardrail do agente
# --------------------------------------------------------------------------- #
def test_sem_segredos_reescreve_e_mantem_o_tipo():
    saida = AgentReply(action="send_text", text=f"a chave e {CHAVE_FALSA}")
    veredito = sem_segredos(saida)
    assert veredito.action == "replace"
    assert isinstance(veredito.replacement, AgentReply)
    assert CHAVE_FALSA not in veredito.replacement.text
    assert veredito.replacement.action == "send_text"


def _modelo(respostas: list[str]) -> tuple[FunctionModel, list[list[ModelMessage]]]:
    """Modelo falso que devolve as respostas em ordem e guarda o que recebeu."""
    chamadas: list[list[ModelMessage]] = []

    def responder(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        chamadas.append(list(messages))
        texto = respostas[min(len(chamadas), len(respostas)) - 1]
        return ModelResponse(parts=[TextPart(json.dumps({"action": "send_text", "text": texto}))])

    return FunctionModel(responder), chamadas


def _rodar(modelo: FunctionModel, *, catalogo_inline=CATALOGO, **extra) -> AgentReply:
    return _run_agent(
        tenant_id="teste",
        business_prompt="Voce e a Lia, recepcao do consultorio.",
        contract_rules=(),
        catalog=tuple((chave, titulo) for chave, titulo, _ in catalogo_inline),
        catalogo_inline=catalogo_inline,
        context={"turn": [{"role": "customer", "text": "quanto custa a protese?"}]},
        model=modelo,
        output_max_tokens=300,
        fontes_de_valor=tuple(f"{t} {c}" for _, t, c in catalogo_inline),
        **extra,
    )


def test_valor_do_catalogo_passa_de_primeira():
    modelo, chamadas = _modelo(["A protese flexivel comeca em R$ 1.500."])
    resposta = _rodar(modelo)
    assert resposta.text == "A protese flexivel comeca em R$ 1.500."
    assert len(chamadas) == 1


def test_valor_inventado_faz_o_modelo_refazer():
    modelo, chamadas = _modelo(
        ["A protese flexivel sai por R$ 1.200.", "A protese flexivel comeca em R$ 1.500."]
    )
    resposta = _rodar(modelo)
    assert resposta.text == "A protese flexivel comeca em R$ 1.500."
    assert len(chamadas) == 2
    # a instrucao de refazer chega ao modelo (como feedback de validacao)
    # citando o valor errado
    ultimo_pedido = " ".join(str(getattr(p, "content", "")) for p in chamadas[1][-1].parts)
    assert "R$ 1200,00" in ultimo_pedido
    assert "depende da avaliacao" in ultimo_pedido


def test_valor_inventado_persistente_bloqueia():
    modelo, chamadas = _modelo(["Fica R$ 999.", "Fica R$ 998."])
    with pytest.raises(OutputBlocked):
        _rodar(modelo)
    assert len(chamadas) == VALOR_RETRIES + 1


def test_sem_catalogo_inline_o_guardrail_nao_opina():
    modelo, chamadas = _modelo(["Fica R$ 999."])
    resposta = _rodar(modelo, catalogo_inline=())
    assert resposta.text == "Fica R$ 999."
    assert len(chamadas) == 1


def test_com_busca_na_web_o_guardrail_nao_opina():
    modelo, chamadas = _modelo(["Fica R$ 999."])
    resposta = _rodar(modelo, web_search=True)
    assert resposta.text == "Fica R$ 999."
    assert len(chamadas) == 1


# --------------------------------------------------------------------------- #
# Flow: o passo de guarda nao depende mais de rede
# --------------------------------------------------------------------------- #
def test_guard_assess_step_usa_checagem_local(monkeypatch):
    from app.durable import flow
    from app.settings import get_settings

    get_settings.cache_clear()
    assert flow.guard_assess_step("output", ["Ola, tudo bem?"], ()).disposition == "allow"
    bloqueio = flow.guard_assess_step("output", [f"chave {CHAVE_FALSA}"], ("e1",))
    assert bloqueio.disposition == "block"
    assert bloqueio.event_refs == ("e1",)

    monkeypatch.setenv("OUTPUT_GUARD_ENABLED", "false")
    get_settings.cache_clear()
    try:
        assert flow.guard_assess_step("output", [f"chave {CHAVE_FALSA}"], ()).disposition == "allow"
    finally:
        monkeypatch.delenv("OUTPUT_GUARD_ENABLED")
        get_settings.cache_clear()
