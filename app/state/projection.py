"""Projecao de ESTADO a partir de FATOS (docs/specs/vendedor-manager-prompts.md).

Regra do chairman e dos modelos (Mythos e Fable): estado e calculado por codigo a
partir de fatos; texto solto ("ok", "obrigada") nunca encerra nem vira loss. Esta
funcao e pura: recebe o que o banco ja sabe e devolve o estado, sem LLM.

Vocabulario: aguardando_paciente, aguardando_clinica, com_equipe, adiado,
agendado, pago, encerrado.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

Estado = Literal[
    "aguardando_paciente",
    "aguardando_clinica",
    "com_equipe",
    "adiado",
    "agendado",
    "pago",
    "encerrado",
]

# Handoff aberto ha mais que isso sem ato humano vira divida da clinica.
HANDOFF_ABERTO_SLA = timedelta(hours=4)


@dataclass(frozen=True)
class Entrada:
    """O que o banco sabe da conversa (ja resolvido pelo chamador)."""

    fatos: tuple[str, ...] = ()  # tipos dos fatos recentes, do mais antigo ao mais novo
    ultima_fala: Literal["paciente", "julia", "equipe", "nenhuma"] = "nenhuma"
    pagamento_confirmado: bool = False
    agendamento_registrado: bool = False
    automacao: Literal["open", "crew", "loss", "block"] = "open"
    handoff_aberto_desde: datetime | None = None
    midia_pendente: bool = False
    # 24/out: um humano da equipe escreveu DEPOIS do ultimo fato de divida; a divida esta paga.
    humano_depois_da_divida: bool = False
    agora: datetime | None = None


# Fatos que, sem resposta depois deles, sao divida da clinica.
_DIVIDA = {"pergunta_aberta", "promessa"}


def estado_da_conversa(e: Entrada) -> Estado:
    """Primeira regra que casar decide. Na duvida: aguardando_clinica."""
    if e.pagamento_confirmado:
        return "pago"
    if e.agendamento_registrado:
        return "agendado"
    if e.automacao in ("loss", "block"):
        return "encerrado"
    if e.automacao == "crew":
        if (
            e.handoff_aberto_desde is not None
            and e.agora is not None
            and e.agora - e.handoff_aberto_desde > HANDOFF_ABERTO_SLA
        ):
            return "aguardando_clinica"
        return "com_equipe"

    fatos = [f for f in e.fatos if not (e.humano_depois_da_divida and f in _DIVIDA)]
    if e.midia_pendente:
        return "aguardando_clinica"
    # Recusa explicita depois da insistencia unica e o unico caminho ao encerramento.
    if "recusa" in fatos and "insistencia_feita" in fatos:
        if fatos.index("insistencia_feita") < len(fatos) - 1 - fatos[::-1].index("recusa"):
            return "encerrado"
    # O ultimo fato de adiamento vale ate algo novo acontecer depois dele.
    ultimo_adiamento = _ultimo(fatos, {"adiamento", "pedido_retorno"})
    ultima_divida = _ultimo(fatos, _DIVIDA)
    if ultima_divida is not None and (ultimo_adiamento is None or ultima_divida > ultimo_adiamento):
        return "aguardando_clinica"
    if ultimo_adiamento is not None:
        return "adiado"
    # Paciente falou por ultimo e nada o explica: e divida (pergunta sem fato).
    if e.ultima_fala == "paciente":
        return "aguardando_clinica"
    return "aguardando_paciente"


def _ultimo(fatos: list[str], tipos: set[str]) -> int | None:
    idx = [i for i, t in enumerate(fatos) if t in tipos]
    return idx[-1] if idx else None
