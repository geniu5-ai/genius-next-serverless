"""Rota fixa de segurança do vertical clínica — docs/TASKS.md item #3.

Detecção DETERMINÍSTICA (regex, sem LLM) sobre o texto recebido do paciente,
ANTES de o modelo responder. Duas classes:

* Classe ``emergencia``  — emergência médica em andamento (dor forte no peito,
  falta de ar intensa, desmaio, convulsão, sangramento intenso, sinais de AVC,
  criança muito mal / não responde, intenção de se machucar ou tirar a própria
  vida). Resposta fixa curta e acolhedora orientando urgência agora ou 192
  (SAMU) + handoff imediato para a equipe.
* Classe ``medicamento`` — pergunta de medicamento/dose (quanto tomar, posso
  tomar X junto com Y, dose, receita). Resposta fixa dizendo que isso só a
  avaliação com o profissional responde + handoff.

Nenhum dos dois gera texto por LLM. O texto fixo é constante deste módulo — a
resposta é sempre a mesma string.

Escopo por tenant: ``Settings.SAFETY_ROUTE_TENANTS`` (vírgula, padrão vazio =
desligado). Tenants fora da lista: comportamento atual intacto.

Armadilha do repo (docs/TRAPS.md #17): o ``_normalize`` aqui apaga tudo que
não é letra — nenhum regex deste módulo depende de símbolo (`%`, `:`, `-`),
então a regra é satisfeita por construção.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from typing import Iterable, Literal

from app.contracts import ReplyDraft
from app.settings import get_settings

logger = logging.getLogger(__name__)

SafetyClass = Literal["emergencia", "medicamento"]

EMERGENCY_TEXT = (
    "Pelo que você descreveu, pode ser uma urgência. "
    "Procure um atendimento de urgência agora (UPA ou pronto-socorro) "
    "ou ligue 192 (SAMU). Já estou encaminhando você para a nossa equipe — "
    "alguém vai falar com você."
)

MEDICATION_TEXT = (
    "Essa dúvida sobre medicamento e dose só pode ser respondida pelo "
    "profissional em uma avaliação — não posso orientar isso por aqui. "
    "Já estou encaminhando você para a nossa equipe para o atendimento certo."
)

EMERGENCY_DRAFT = ReplyDraft(action="handoff", text=EMERGENCY_TEXT)
MEDICATION_DRAFT = ReplyDraft(action="handoff", text=MEDICATION_TEXT)


def _normalize(text: str) -> str:
    """Casefold + remove acento + só letras (armadilha #17 não se aplica:
    nenhum regex abaixo depende de símbolo)."""
    nfkd = unicodedata.normalize("NFKD", text)
    sem_acento = "".join(c for c in nfkd if not unicodedata.combining(c))
    return re.sub(r"[^a-z]+", " ", sem_acento.casefold())


# --- Classe A: emergência médica em andamento ------------------------------
# Escritos contra o texto normalizado (sem acento, sem símbolo).
_EMERGENCY_RES: tuple[re.Pattern[str], ...] = (
    # Dor no peito (inclusive typo comum de WhatsApp: "peitu").
    re.compile(r"\bdor (forte |muito |mto )?(no|na) peit[oou]\b"),
    # Falta de ar: só com intensidade/agora (solto é ambíguo — o modelo trata).
    re.compile(r"\b(muita|forte|intensa|grave|subita|repentina) falta de ar\b"),
    re.compile(
        r"\bfalta de ar (forte|intensa|grave|subita|repentina|agora|que nao passa)\b"
    ),
    re.compile(r"\bsem conseguir respirar\b"),
    re.compile(r"\bta sem ar\b"),
    re.compile(r"\bnao (consigo|dou|dae|dah) respirar\b"),
    re.compile(r"\bnao respira\b"),
    re.compile(r"\bengasgada?\b"),
    # Desmaio / perda de consciência.
    re.compile(r"\bdesmai(o|ei|ou|ada|ado|ando)\b"),
    re.compile(r"\bapagou\b"),
    re.compile(r"\bperdeu a consciencia\b"),
    re.compile(r"\binconsciente\b"),
    # Convulsão em andamento (menção passada é ambígua — o modelo trata).
    re.compile(r"\b(esta|ta|entrou em|tendo|fazendo) (uma )?convuls\w*\b"),
    re.compile(r"\bconvulsionando\b"),
    re.compile(r"\bataque epileptico\b"),
    # Sangramento intenso.
    re.compile(r"\bsangrando (muito|demais|bastante)\b"),
    re.compile(r"\bsangramento (forte|intenso|muito|que nao para)\b"),
    re.compile(r"\bhemorragia\b"),
    # Sinais de AVC (boca torta, fala enrolada, fraqueza de um lado).
    re.compile(r"\bboca torta\b"),
    re.compile(r"\bfala enrolada\b"),
    re.compile(r"\bfalando enrolado\b"),
    re.compile(r"\bfraqueza de um lado\b"),
    re.compile(r"\bum lado (do corpo|paralisado)\b"),
    re.compile(r"\bparalisad(o|a) de um lado\b"),
    # AVC/derrame: só presente/suspeita (menção solta é ambígua).
    re.compile(r"\b(esta|ta|estou|tendo|com) (um |uma )?(avc|derrame)\b"),
    re.compile(r"\bsuspeita de (avc|derrame)\b"),
    re.compile(r"\b(avc|derrame) agora\b"),
    # Criança muito mal / não responde.
    re.compile(r"\b(bebe|nenem|crianca|filh[oa]|meu filho|minha filha) nao responde\b"),
    re.compile(r"\bnao responde (mais|nem)\b"),
    re.compile(r"\bcrianca (ta|esta|ficou) muito mal\b"),
    re.compile(r"\bcorpo molinho\b"),
    # Intenção de se machucar ou tirar a própria vida.
    re.compile(r"\b(quero|queria|vou|penso|pretendo|tenho vontade de) (em )?me matar\b"),
    re.compile(r"\bme matar\b"),
    re.compile(r"\bme cortar\b"),
    re.compile(r"\bme machucar\b"),
    re.compile(r"\bsuicid[aoi]\w*\b"),
    re.compile(r"\btirar (a minha|minha) vida\b"),
)

# --- Classe B: pergunta de medicamento/dose ---------------------------------
_MEDICATION_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(posso|pode|podia|devo|preciso) tomar\b"),
    re.compile(r"\bquanto (tomo|tomar|tenho que tomar|devo tomar)\b"),
    re.compile(r"\btom(o|ar) (antes|depois)\b"),
    re.compile(r"\b(remedio|medicamento) (junto )?com\b"),
    # Receita: só pedido explícito ligado a remédio/medicamento (receita de
    # exame, médico receitou exame etc. não disparam — o modelo trata).
    re.compile(r"\breceit[oa]s? (de|do|da|pra|para) (\w+ )?(remedio|medicamento)\b"),
    re.compile(r"\bme receita\w*\b"),
    re.compile(r"\breceitar\b"),
    re.compile(r"\bquant[oa]s? (comprimidos|pastilhas|gotas|capsulas)\b"),
    re.compile(r"\bquantas (vezes ao dia|horas)\b"),
    re.compile(r"\bquantos (mg|miligramas)\b"),
    re.compile(r"\b(dose|dosagem)\b"),
    re.compile(r"\bprescricao\b"),
    re.compile(r"\btomar (junto|com) \w+\b"),
    re.compile(r"\bjunto com (o |a )?(remedio|medicamento)\b"),
    re.compile(r"\bcom remedio\b"),
    re.compile(r"\bremedio que (tomo|estou tomando)\b"),
    re.compile(r"\binterferencia medicamentosa\b"),
)

# O vertical vende vacinas: "tomar/quantas doses da vacina" é comercial, não
# pergunta de medicamento — veto da classe B quando o texto fala de vacina.
_VACINA_RE = re.compile(r"\bvacin\w*\b")

def _matches_any(patterns: Iterable[re.Pattern[str]], normalized: str) -> bool:
    return any(pattern.search(normalized) for pattern in patterns)


def classify_text(text: str) -> SafetyClass | None:
    """Classe de risco de uma fala do paciente, ou None.

    Emergência (A) tem precedência sobre medicamento (B): "estou tomando
    remédio e agora estou com dor no peito" é emergência, não dúvida de dose.
    """
    normalized = _normalize(text)
    if not normalized:
        return None
    if _matches_any(_EMERGENCY_RES, normalized):
        return "emergencia"
    if _VACINA_RE.search(normalized):
        return None
    if _matches_any(_MEDICATION_RES, normalized):
        return "medicamento"
    return None


def _safety_tenants() -> frozenset[str]:
    """Tenants com a rota ativa. Padrão vazio = desligado para todos."""
    raw = get_settings().SAFETY_ROUTE_TENANTS
    return frozenset(
        tenant.strip() for tenant in raw.split(",") if tenant.strip()
    )


def safety_route_enabled(tenant_id: str) -> bool:
    return tenant_id in _safety_tenants()


def safety_handoff_draft(
    tenant_id: str, customer_texts: Iterable[str]
) -> ReplyDraft | None:
    """ReplyDraft fixo de handoff quando a rota dispara; None caso contrário.

    Chame NO workflow do turno, antes de qualquer chamada ao modelo. Log
    estruturado registra apenas tenant e classe — nunca o texto do paciente.
    """
    if not safety_route_enabled(tenant_id):
        return None
    safety_class: SafetyClass | None = None
    for text in customer_texts:
        detected = classify_text(text or "")
        if detected is None:
            continue
        if detected == "emergencia":
            safety_class = "emergencia"
            break
        safety_class = "medicamento"
    if safety_class is None:
        return None
    logger.info(
        "safety_route_triggered",
        extra={
            "event": "safety_route_triggered",
            "tenant_id": tenant_id,
            "safety_class": safety_class,
        },
    )
    return EMERGENCY_DRAFT if safety_class == "emergencia" else MEDICATION_DRAFT
