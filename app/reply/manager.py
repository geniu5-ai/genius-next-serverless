"""Manager por tenant — só detecção determinística (sem LLM, sem rede).

Quatro comportamentos, todos atrás da flag ``Settings.MANAGER_AGENT_TENANTS``
(vírgula de tenant_id; **padrão vazio = desligado**, então fora da lista nada
muda no turno de hoje):

* **(A) Promessa** — a resposta da IA promete verificar algo (preço, horário,
  disponibilidade, orçamento). ``detect_promise`` devolve o assunto; quem
  registra de forma durável é :mod:`app.durable.manager` (migration 076).
* **(C) Assistência** — a conversa foi para a equipe e ninguém respondeu. O
  registro é feito pelo fluxo quando ``draft.action == "handoff"``; este módulo
  só expõe o rótulo do atraso (``ASSISTENCIA_KIND``).
* **(D) Indicação** — cartão de contato ou menção a amigo/parente interessado.
  ``manager_contact_draft`` responde o turno **sem modelo** (hoje esse turno
  derruba com ``RuntimeError: turno sem conteudo para redigir``) e
  ``detect_indication`` sinaliza a indicação dita em texto.

Normalização igual à de ``handoff_promotion`` e ``safety_routes`` (armadilha
#17 de ``docs/TRAPS.md``): NFKD, sem acento, só letras — nenhum padrão
depende de símbolo. Regex nenhuma decide status de cliente nem fluxo: aqui só
se decide *se vale a pena* registrar uma pendência.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Iterable

from app.contracts import ReplyDraft

PROMESSA_KIND = "promessa"
ASSISTENCIA_KIND = "assistencia"
INDICACAO_KIND = "indicacao"
TRIAGEM_KIND = "triagem"

# messageTypes do Evolution sem texto proprio: cartao de contato.
_CONTACT_MESSAGE_TYPES = frozenset({"contactMessage", "contactsArrayMessage"})

# Respostas fixas (nada inventado: sem valor, sem horario, sem prazo).
CONTATO_TEXTO = (
    "Recebi o cartão de contato, obrigada! Me conta o que essa pessoa precisa "
    "que eu já deixo tudo preparado, ou, se preferir, aviso a nossa equipe "
    "para entrarem em contato com esse número."
)
SEM_TEXTO_TEXTO = (
    "Recebi sua mensagem, mas não consegui entender o conteúdo. Pode escrever "
    "para mim? Assim já te ajudo na hora."
)


def _normalize(text: str) -> str:
    """Casefold + remove acento + só letras (critério da casa)."""
    nfkd = unicodedata.normalize("NFKD", text)
    sem_acento = "".join(c for c in nfkd if not unicodedata.combining(c))
    return re.sub(r"[^a-z]+", " ", sem_acento.casefold()).strip()


def manager_tenants() -> frozenset[str]:
    """Tenants com o Manager ativo. Padrão vazio = desligado."""
    from app.settings import get_settings

    raw = getattr(get_settings(), "MANAGER_AGENT_TENANTS", "") or ""
    return frozenset(t.strip() for t in raw.split(",") if t.strip())


def manager_enabled(tenant_id: str) -> bool:
    return tenant_id in manager_tenants()


# --------------------------------------------------------------------------- #
# (A) Promessa — a IA disse que vai verificar/confirmar algo
# --------------------------------------------------------------------------- #

# Assuntos que a promessa pode ter. Chave = rotulo gravado em
# manager_items.question (curto, sem dado de paciente).
_ASSUNTOS: tuple[tuple[str, str], ...] = (
    ("preco", r"\b(?:precos?|valores?|custa|custo|quanto|orcamento|taxa|taxas)\b"),
    (
        "horario",
        r"\b(?:horarios?|agenda|disponibilidade|vagas?|espacos?|espaco|"
        r"marcacao|marcar|marque|dia e hora|calendario|peridioco)\b",
    ),
    (
        "exame",
        r"\b(?:exames?|laudos?|resultados?|resultados|pedidos?|pedido|"
        r"radiografias?|tomografias?|ressonancias?|ultrassons?|mamografias?|"
        r"exames de sangue|sangue)\b",
    ),
    ("convenio", r"\b(?:convenios?|plano de saude|particular|reembolso)\b"),
    ("preparo", r"\b(?:preparos?|jejum|orientacoes?|como (?:fazer|preparar))\b"),
    ("estoque", r"\b(?:estoque|em estoque|disponivel|tem (?:em )?estoque)\b"),
)

# Compromisso em primeira pessoa (futuro, gerundio ou "deixa eu ver").
_PROMESSA_VERBOS = (
    r"(?:vou|vamos|irei|vao|deixa eu|deixe eu|preciso|estou indo|to indo|"
    r"ja vou|quero so|vou so|vou ja|verificarei|confirmarei|checarei|"
    r"consultarei|retornarei|passarei)"
)
_PROMESSA_ACAO = (
    r"(?:ver|verificar|confirmar|conferir|checar|consultar|olhar|checar|"
    r"passar|repassar|encaminhar|perguntar|falar|conversar|dar uma olhada|"
    r"verificar|confirmar|reunir|alinhar|ver o|ver a|checar)"
)

_PROMESSA_RE: tuple[re.Pattern[str], ...] = (
    # "vou verificar o preço", "vou ver os horários", "deixa eu checar a agenda"
    re.compile(rf"\b{_PROMESSA_VERBOS}\b(?: \w+){{0,3}}? ?{_PROMESSA_ACAO}\b"),
    # "confiro com a recepção e te retorno" / "passo o valor já já"
    re.compile(
        r"\b(?:confiro|confira|verifico|verifica|checo|checa|consulto|consulta|"
        r"passo|passa|mando|envio|falo|retorno|retornamos|respondo|responderemos)\b"
        r"(?: \w+){0,4}?\b(?:e|,|;)?\s*(?:te |lhe |voce |vc )?"
        r"(?:retorno|retornamos|falo|passo|mando|envio|respondo|aviso|aviso|"
        r"confirmo|verifico|checo)\b"
    ),
    # "isso eu confirmo com a equipe e ja te falo" (assunto antes do verbo)
    re.compile(
        r"(?:\w+ ){0,6}?(?:vou|vamos|irei|deixa eu|deixe eu|preciso|ja vou)"
        r"(?: \w+){0,3} ?" + _PROMESSA_ACAO + r"\b"
    ),
)

# Oferta/pedida de permissao ainda nao e promessa: "posso verificar o preco
# se voce quiser".
_OFERTA_RE = re.compile(
    r"\b(?:posso|podemos|poderia|poderiamos|quer que|se quiser|se precisar|"
    r"se preferir|gostaria|gostariamos|caso queira|caso precise)\b"
)
# Negação: "nao vou conseguir verificar hoje".
_NEGACAO_RE = re.compile(
    r"\b(?:nao (?:vou|vai|vao|consegue|consigo|podemos|irei)|sem verificar|"
    r"nao (?:temos|ha) (?:horarios?|vagas?|estoque))\b"
)

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+|\n+")

# Conjugacoes de "ver" que o padrao nao pega sozinho depois de preposicao.
_ACoes_ESPEC = ("verificarei", "confirmarei", "checarei", "consultarei", "retornarei")


def detect_promise(text: str) -> str | None:
    """Rotulo do assunto prometido (``preco``, ``horario``...) ou ``None``.

    Só a RESPOSTA da IA e' examinada, nunca a fala do paciente: e' a IA quem
    promete. Uma frase que ja ENTREGA o dado ("o valor e' R$ 150") nao tem
    verbo de compromisso e nao casa.
    """
    if not text or not text.strip():
        return None
    normalized = _normalize(text)
    if not normalized:
        return None
    for sentence in _SENTENCE_SPLIT_RE.split(text):
        sent = _normalize(sentence)
        if not sent:
            continue
        if _OFERTA_RE.search(sent) or _NEGACAO_RE.search(sent):
            continue
        tem_promessa = any(p.search(sent) for p in _PROMESSA_RE) or any(
            verb in sent for verb in _ACoes_ESPEC
        )
        if not tem_promessa:
            continue
        for assunto, padrao in _ASSUNTOS:
            if re.search(padrao, sent):
                return assunto
    return None


# --------------------------------------------------------------------------- #
# (D) Indicação — amigo/parente interessado citado em texto
# --------------------------------------------------------------------------- #

_INDICACAO_RE: tuple[re.Pattern[str], ...] = (
    # "vou indicar minha irmã", "quero te indicar uma amiga"
    re.compile(
        r"\b(?:vou|posso|quero|pode|pode ser|quer|gostaria) (?:te |lhe )?"
        r"(?:indicar|recomendar|passar|mandar|enviar|convidar)\b"
        r"(?:\s+\w+){0,4}\s+(?:amiga|amigo|irma|irmao|mae|pai|esposa|esposo|"
        r"filho|filha|prima|primo|conhecido|conhecida|parente|familia|alguem)\b"
    ),
    # "minha mãe quer agendar", "meu marido precisa de orçamento"
    re.compile(
        r"\b(?:minha|minho|meu|minha) (?:mae|pai|irma|irmao|esposa|esposo|"
        r"filho|filha|amiga|amigo|prima|primo|tia|tio|sobrinha|sobrinho|"
        r"cunhada|cunhado|avo|marido|mulher|namorada|namorado)\b"
        r"(?:\s+\w+){0,4}\s+(?:quer|precisa|gostaria|vai querer|quer agendar|"
        r"quer marcar|vou mandar|vou passar|esta querendo|vai fazer)\b"
    ),
    # "vou mandar ela aqui falar com voces" / "passo o contato dele pra voces"
    re.compile(
        r"\b(?:mando|manda|mandar|passo|passa|passar|envio|envia|enviar|"
        r"encaminho|encaminha|encaminhar)\s+"
        r"(?:ela|ele|o contato|a contato|o numero|o telefone|o zap|"
        r"ele pra|ela pra)\b"
    ),
)


def detect_indication(text: str) -> str | None:
    """Resumo curto da indicação citada em texto, ou ``None``.

    Conservador de proposito: detecta mal antes de detectar errado — um falso
    positivo vira uma linha de lead no banco e nada mais; a resposta do
    paciente continua sendo a do modelo.
    """
    if not text or not text.strip():
        return None
    normalized = _normalize(text)
    for padrao in _INDICACAO_RE:
        m = padrao.search(normalized)
        if m:
            trecho = re.sub(r"\s+", " ", m.group(0)).strip()
            return trecho[:120]
    return None


# --------------------------------------------------------------------------- #
# (D) Turno sem texto — o que hoje derruba o turno
# --------------------------------------------------------------------------- #


def manager_contact_draft(turn: object) -> ReplyDraft | None:
    """Draft fixo (sem modelo) para turno inbound SEM nenhum texto.

    Devolve ``None`` quando ha texto no lote — nesse caso o caminho normal do
    modelo continua valendo. O RuntimeError "turno sem conteudo para redigir"
    so existe porque o lote sem texto chegava ate o agente; com a flag ligada
    o lote e' respondido aqui, deterministicamente, e o lead do cartao de
    contato e' registrado por :func:`app.durable.manager.register_turn_items`.
    """
    events = list(getattr(turn, "events", None) or [])
    inbound = [e for e in events if getattr(e, "direction", None) == "inbound"]
    if not inbound:
        return None
    if any((getattr(e, "text_content", None) or "").strip() for e in events):
        return None
    if any(getattr(e, "message_type", "") in _CONTACT_MESSAGE_TYPES for e in inbound):
        return ReplyDraft(action="send_text", text=CONTATO_TEXTO)
    return ReplyDraft(action="send_text", text=SEM_TEXTO_TEXTO)


def turn_has_contact_card(turn: object) -> bool:
    """True quando o lote traz um cartao de contato (para gravar o lead)."""
    return any(
        getattr(e, "direction", None) == "inbound"
        and getattr(e, "message_type", "") in _CONTACT_MESSAGE_TYPES
        for e in (getattr(turn, "events", None) or [])
    )


# --------------------------------------------------------------------------- #
# Triagem proativa antes da equipe (branch feat/triagem-proativa)
# --------------------------------------------------------------------------- #

# Texto de espera curto e natural, sem prometer prazo e sem citar equipe/IA.
TRIAGEM_ESPERA_TEXT = "Deixa eu ver isso direitinho pra você, só um instante."

# Causas de handoff que NUNCA passam pela triagem: rota fixa de seguranca
# (emergencia, medicamento) e midia que nao abriu vao direto ao owner, como
# hoje. A triagem so vale para handoff do ramo normal (causa None) ou promocao.
_CAUSAS_FORA_DA_TRIAGEM: frozenset[str] = frozenset(
    {"emergencia", "medicamento", "midia"}
)

# Pedido explicito de uma pessoa: "falar com atendente/humano/pessoa/alguem
# da equipe". Escrito contra o texto normalizado (sem acento, so letras, espaco
# simples entre palavras) — mesma regra de safety_routes/handoff_promotion.
# Conservador: um falso positivo so manda o caso direto para a equipe (o default
# seguro), nunca inventa resposta. "tem alguem ai?" (disponibilidade) NAo pega:
# nao ha verbo de pedido antes.
_PEDIDO_PESSOA_RE = re.compile(
    r"\b(?:falar|conversar|quero|preciso|gostaria|queria|desejo|consigo|"
    r"tem como|coloca|poe|encaminha|transfere|conecta|chama|passa)\b"
    r"(?:\s+\w+){0,3}?\s+"
    r"(?:com\s+|pra\s+|para\s+|de\s+)?"
    r"(?:o\s+|a\s+|um\s+|uma\s+)?"
    r"(?:atendente|humano|pessoa|alguem)\b"
)


def _normalize_triagem(text: str) -> str:
    """Casefold + remove acento + so letras (criterio da casa)."""
    nfkd = unicodedata.normalize("NFKD", text)
    sem_acento = "".join(c for c in nfkd if not unicodedata.combining(c))
    return re.sub(r"[^a-z]+", " ", sem_acento.casefold()).strip()


def triage_tenants() -> frozenset[str]:
    """Tenants com a triagem proativa ativa. Padrao vazio = desligado."""
    from app.settings import get_settings

    raw = getattr(get_settings(), "MANAGER_TRIAGE_TENANTS", "") or ""
    return frozenset(t.strip() for t in raw.split(",") if t.strip())


def triage_enabled(tenant_id: str) -> bool:
    return tenant_id in triage_tenants()


def pediu_pessoa(texts: Iterable[str]) -> bool:
    """True quando o paciente pediu explicitamente uma pessoa (vai direto)."""
    return any(
        _PEDIDO_PESSOA_RE.search(_normalize_triagem(t or "")) for t in texts
    )


def mandou_cadastro(texts: Iterable[str]) -> bool:
    """True quando o paciente mandou dados de cadastro de agendamento.

    Reusa o regex de ``handoff_notice`` (``derive_reason`` == REASON_REGISTRATION),
    sem duplicar o padrao.
    """
    from app.durable.handoff_notice import REASON_REGISTRATION, derive_reason

    return derive_reason([t for t in texts if t], None) == REASON_REGISTRATION


def triagem_aplica(
    *,
    tenant_id: str,
    action: str,
    handoff_cause: str | None,
    patient_texts: Iterable[str],
) -> bool:
    """A triagem proativa vale para este handoff? So deteccao, sem efeito.

    Liga quando: tenant na flag, ``action == "handoff"`` vindo do ramo normal
    (causa None) ou da promocao (``"promovido"``), e o paciente NAO pediu
    explicitamente uma pessoa nem mandou dados de cadastro. Emergencia,
    medicamento e midia ficam de fora (vao direto ao owner).
    """
    if not triage_enabled(tenant_id):
        return False
    if action != "handoff":
        return False
    if handoff_cause in _CAUSAS_FORA_DA_TRIAGEM:
        return False
    if handoff_cause not in (None, "promovido"):
        return False
    texts = [t for t in patient_texts if t and t.strip()]
    if pediu_pessoa(texts):
        return False
    if mandou_cadastro(texts):
        return False
    return True
