"""Auditoria pos-hoc da conversa -- ponto unico de entrada pro flow.

Fase 1 (22/set/2026): so observa e loga. Nunca bloqueia, nunca realimenta o
prompt de geracao, nunca muda estado (loss/automation_state/Rescue). Ver
docs/TASKS.md #13 para o historico completo da decisao.

Por que este modulo existe (e nao mais uma chamada solta dentro de
``conversation_turn_workflow``): duas integracoes inline seguidas (etapa,
depois ``bad_faith_signal``) tornaram claro que cada sinal novo virando uma
chamada a mais no meio do workflow principal nao escala -- pedido explicito
do chairman pra consolidar num lugar so. Qualquer achado novo (regex sobre
fala do cliente, regex sobre resposta do agente, ou a decisao de quando vale
chamar um juiz LLM) entra AQUI. ``flow.py`` so importa ``audit_turn`` e
chama uma vez, com o texto final que de fato vai pro cliente.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass

from app.status.rules import bad_faith_signal

logger = logging.getLogger(__name__)


def _normalize(text: str) -> str:
    """Como app.status.rules._normalize, mas preserva ``%`` e ``:`` --
    ambos carregam sinal pros checks abaixo (percentual, horario exato) e a
    normalizacao generica (so [0-9a-z]) os apagaria."""
    decomposed = unicodedata.normalize("NFKD", text)
    without_marks = "".join(char for char in decomposed if not unicodedata.combining(char))
    simplified = re.sub(r"[^0-9a-z%:]+", " ", without_marks.casefold())
    return " ".join(simplified.split())


# ── Findings deterministicos sobre a RESPOSTA do agente ─────────────────────
# Generalizado a partir do bad_faith_signal (que so olha a fala do CLIENTE):
# estes olham a resposta do AGENTE, e cobrem so violacao de invariante
# absoluta -- lista fechada, decidida na sessao de design do "contrato
# hibrido" prompt/flow (22/set/2026). So o que e sim/nao sem julgamento
# entra aqui; qualquer coisa que exija leitura de intencao (preco cedo
# demais, acolhimento pulado) fica de fora -- isso e trabalho do prompt, nao
# de regex (ver docs/core/03-PROMPT.md).
_DUAS_PERGUNTAS_RE = re.compile(r"\?.*\?")

_HORARIO_EXATO_RE = re.compile(
    r"\b\d{1,2}\s*h(?:oras?)?(?:\s*\d{2}\b)?|\b\d{1,2}:\d{2}\b"
)

# Percentual exige contexto de chance/sucesso/exito -- "desconto de 20%" e
# preco legitimo, nao promessa de resultado. So dispara junto com a palavra
# de resultado, nunca por percentual isolado.
_PROMESSA_RESULTADO_RE = re.compile(
    r"\bgarant(?:o|ido|ida|imos|ia)\b"
    r"|\bvai\s+ficar\s+perfeito\b"
    r"|\bresultado\s+(?:certo|garantido)\b"
    r"|\b\d{1,3}\s*(?:%|por\s*cento)\s*(?:de\s+)?(?:chance|sucesso|exito)\b"
)

_REVELOU_IA_RE = re.compile(
    r"\bsou\s+(?:uma?\s+)?(?:ia|rob[oô]|assistente\s+virtual|chat\s*bot|chatbot)\b"
    r"|\binteligencia\s+artificial\b"
)


@dataclass(frozen=True)
class AuditFindings:
    """Achados de um turno -- so observacao, nunca acao (fase 1)."""

    bad_faith: bool = False
    duas_perguntas: bool = False
    horario_exato: bool = False
    promessa_resultado: bool = False
    revelou_ia: bool = False

    def any(self) -> bool:
        return any(
            (
                self.bad_faith,
                self.duas_perguntas,
                self.horario_exato,
                self.promessa_resultado,
                self.revelou_ia,
            )
        )

    def names(self) -> tuple[str, ...]:
        pares = (
            ("bad_faith", self.bad_faith),
            ("duas_perguntas", self.duas_perguntas),
            ("horario_exato", self.horario_exato),
            ("promessa_resultado", self.promessa_resultado),
            ("revelou_ia", self.revelou_ia),
        )
        return tuple(nome for nome, achado in pares if achado)


def audit_reply(customer_texts: Sequence[str], agent_reply: str) -> AuditFindings:
    """Roda todos os checks deterministicos sobre um turno. So log, nunca bloqueia."""
    normalized_reply = _normalize(agent_reply)
    return AuditFindings(
        bad_faith=bad_faith_signal(customer_texts),
        duas_perguntas=bool(_DUAS_PERGUNTAS_RE.search(agent_reply)),
        horario_exato=bool(_HORARIO_EXATO_RE.search(normalized_reply)),
        promessa_resultado=bool(_PROMESSA_RESULTADO_RE.search(normalized_reply)),
        revelou_ia=bool(_REVELOU_IA_RE.search(normalized_reply)),
    )


def should_invoke_judge(findings: AuditFindings, *, customer_status: str) -> bool:
    """Gatilho leve e dinamico pro juiz LLM semantico (fase 2, ainda NAO implementada).

    Dispara se qualquer achado deterministico existir OU se o status deste
    turno e ``loss`` -- perder um lead e o momento de maior valor pra
    auditar com mais profundidade (pedido explicito, 22/set/2026). Sem
    nenhuma das duas condicoes, roda o juiz em todo turno custaria caro sem
    sinal correspondente -- fica de fora de proposito.
    """
    return findings.any() or customer_status == "loss"


def audit_turn(
    *,
    turn_id: str,
    tenant_id: str,
    customer_texts: Sequence[str],
    agent_reply: str,
    customer_status: str,
) -> None:
    """Ponto unico de entrada pro flow.py. So loga -- ver docstring do modulo.

    Fase 2 (nao implementada ainda): quando ``should_invoke_judge`` for
    True, enfileirar chamada assincrona a um juiz LLM barato/amostrado,
    fora do caminho critico (nao atrasa a resposta, que ja foi despachada
    antes deste ponto rodar). Escolha de modelo, custo e onde persistir o
    veredito ficam pra quando isso virar tarefa de verdade -- ver
    docs/TASKS.md #13.
    """
    findings = audit_reply(customer_texts, agent_reply)
    judge_triggered = should_invoke_judge(findings, customer_status=customer_status)
    if findings.any() or judge_triggered:
        logger.info(
            "conversation_audit turno=%s tenant=%s achados=%s judge_trigger=%s",
            turn_id,
            tenant_id,
            findings.names(),
            judge_triggered,
        )
