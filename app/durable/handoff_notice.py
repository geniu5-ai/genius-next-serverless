"""Aviso ao gestor (rota equipe): curto, acionável no celular, sem dado sensível.

Quem recebe é um gestor lendo no WhatsApp: precisa saber QUEM é o paciente
(link wa.me + nome do contato, se já existir), POR QUE a equipe foi
chamada e o que o paciente disse por último — sem abrir o sistema. Redigido
como uma pessoa da clínica escreveria (sem colchete, sem "rota", sem
"IA"/"agente"/"sistema"/"bot"), uma ideia por linha.

Nunca entram no aviso: CPF, e-mail, data de nascimento e nome da mãe do
paciente. Esses dados chegam nas falas do paciente (é o cadastro de
agendamento), então o texto é redigido antes de ir para o aviso.

O contrato público de ``ReplyDraft`` não muda: o motivo é derivado aqui, a
partir da causa que o fluxo informa (``handoff_cause``) e do texto do paciente.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable, Literal

HandoffCause = Literal["emergencia", "medicamento", "promovido", "midia"]

NOTICE_MAX_CHARS = 900
PATIENT_LINE_MAX_CHARS = 160
TRIAGE_SUMMARY_MAX_CHARS = 200
MAX_PATIENT_LINES = 4
_NAME_MAX_CHARS = 60

REASON_REGISTRATION = "dados de agendamento enviados"
REASON_UNANSWERED = "dúvida sem resposta"
REASON_EMERGENCY = "emergência"
REASON_MEDICATION = "dose de remédio"
REASON_DEFAULT = "pedido do paciente"
REASON_MEDIA = "arquivo que a atendente não conseguiu abrir"

# Motivo em frase natural, como uma pessoa da clinica escreveria no WhatsApp.
# Sem colchete, sem "rota", sem "IA"/"agente"/"sistema"/"bot". NEUTRO em gênero:
# há pacientes homens, então nada de "ela"/"a paciente".
_REASON_PHRASE: dict[str, str] = {
    REASON_EMERGENCY: "está com uma emergência, chama agora, por favor",
    REASON_MEDICATION: "quer saber a dose de um remédio",
    REASON_MEDIA: "mandou um arquivo que não abriu aqui",
    REASON_REGISTRATION: "mandou os dados para agendar",
    REASON_UNANSWERED: "ficou com uma dúvida que ainda não foi respondida",
    REASON_DEFAULT: "tem um pedido que é com vocês",
}

# Todo caractere de controle (\r, \t, \x00...) vira espaço; só o próprio
# aviso insere "\n" estrutural entre campos.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f  ]+")
_SPACES_RE = re.compile(r"\s+")

# --- Redação de dados sensíveis ----------------------------------------------
_CPF_RE = re.compile(r"(?<!\d)\d{3}[.\s]?\d{3}[.\s]?\d{3}[-\s]?\d{2}(?!\d)")
_EMAIL_RE = re.compile(r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+")
_DATE_NUM_RE = re.compile(r"(?<!\d)\d{1,2}\s*[/.\-]\s*\d{1,2}\s*[/.\-]\s*(?:\d{4}|\d{2})(?!\d)")
_DATE_TEXT_RE = re.compile(
    r"(?<!\d)\d{1,2}\s+de\s+[a-zA-Zçãé]+\s+de\s+\d{2,4}", re.IGNORECASE
)
_LONG_DIGITS_RE = re.compile(r"(?<!\d)\d{8,}(?!\d)")
_MOTHER_RE = re.compile(
    r"(nome\s+da\s+m[aã]e)\s*(?:[:\-é]|e)?\s*[^\n,;]*", re.IGNORECASE
)
_BIRTH_RE = re.compile(
    r"(data\s+(?:de\s+)?nasc\w*|nascimento|nasci\s+em)\s*[:\-]?\s*[^\n,;]*",
    re.IGNORECASE,
)

# --- Motivo --------------------------------------------------------------------
_REGISTRATION_RE = re.compile(
    r"\bcpf\b|\bdata (?:de )?nasc\w*|\bnome da mae\b|\bnascimento\b|\bnascid[oa]\b|\bnasci\b"
)
# Data com ano (12/03/1980, 12-03-80): o _normalize apaga dígitos, então vai à parte.
_DATE_RE = re.compile(r"\b\d{1,2}[/.-]\d{1,2}[/.-](?:\d{2}|\d{4})\b")


def _normalize(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text)
    sem_acento = "".join(c for c in nfkd if not unicodedata.combining(c))
    return re.sub(r"[^a-z]+", " ", sem_acento.casefold())


def redact_sensitive(text: str) -> str:
    """Troca CPF, e-mail, data de nascimento/datas e números longos por marcas."""
    out = _MOTHER_RE.sub(r"\1 [omitido]", text)
    out = _BIRTH_RE.sub(r"\1 [omitido]", out)
    out = _EMAIL_RE.sub("[e-mail omitido]", out)
    out = _CPF_RE.sub("[CPF omitido]", out)
    out = _DATE_NUM_RE.sub("[data omitida]", out)
    out = _DATE_TEXT_RE.sub("[data omitida]", out)
    out = _LONG_DIGITS_RE.sub("[número omitido]", out)
    return out


def clean_line(text: str) -> str:
    """Uma linha só: sem controle, sem quebra, espaços colapsados."""
    return _SPACES_RE.sub(" ", _CONTROL_RE.sub(" ", text)).strip()


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)].rstrip() + "…"


def format_dialable_phone(remote_jid: str) -> str | None:
    """``5511999998888@s.whatsapp.net`` -> ``+55 11 999998888``; None se não for número.

    JID de LID ou de grupo não carrega telefone: devolve None (nunca inventa).
    """
    local, _, domain = (remote_jid or "").partition("@")
    if domain != "s.whatsapp.net" or not local.isdigit():
        return None
    if local.startswith("55") and len(local) in (12, 13):
        return f"+55 {local[2:4]} {local[4:]}"
    return f"+{local}"


def format_wa_link(remote_jid: str) -> str | None:
    """``5588999998888@s.whatsapp.net`` -> ``wa.me/5588999998888``; None se não for número.

    Link wa.me (o toque abre a conversa), nunca o ``+55``. Normaliza o nono
    dígito: JID brasileiro com 12 dígitos e primeiro dígito do número local
    (posição 4) em 6,7,8,9 ganha o ``9`` depois do DDD (vira 13); 13 já vem
    certo e os demais ficam como estão. JID de LID ou de grupo não carrega
    telefone: devolve None (nunca inventa).
    """
    local, _, domain = (remote_jid or "").partition("@")
    if domain != "s.whatsapp.net" or not local.isdigit():
        return None
    if local.startswith("55") and len(local) == 12 and local[4] in "6789":
        local = f"{local[:4]}9{local[4:]}"
    return f"wa.me/{local}"


def contact_name(events: Iterable[Any]) -> str | None:
    """Último ``push_name`` não vazio de um evento INBOUND do turno."""
    name: str | None = None
    for event in events:
        if getattr(event, "direction", None) != "inbound":
            continue
        payload = getattr(event, "sanitized_payload", None)
        data = getattr(payload, "data", None)
        candidate = getattr(data, "push_name", None)
        if isinstance(candidate, str) and candidate.strip():
            name = candidate
    if name is None:
        return None
    cleaned = truncate(clean_line(redact_sensitive(name)), _NAME_MAX_CHARS)
    return cleaned or None


def derive_reason(patient_texts: Iterable[str], cause: HandoffCause | None) -> str:
    """Motivo em uma linha. Emergência e dose vêm do fluxo (rota fixa)."""
    if cause == "emergencia":
        return REASON_EMERGENCY
    if cause == "medicamento":
        return REASON_MEDICATION
    if cause == "midia":
        return REASON_MEDIA
    if any(
        _REGISTRATION_RE.search(_normalize(t or "")) or _DATE_RE.search(t or "")
        for t in patient_texts
    ):
        return REASON_REGISTRATION
    if cause == "promovido":
        return REASON_UNANSWERED
    return REASON_DEFAULT


def build_human_notice(
    *,
    remote_jid: str,
    patient_texts: Iterable[str],
    name: str | None = None,
    cause: HandoffCause | None = None,
    resumo: str | None = None,
    motivo: str | None = None,
    remetente: str | None = None,
) -> str:
    """Aviso ao owner como um recado de WhatsApp de quem manda na clínica.

    ``remetente`` (ou ``Settings.HANDOFF_NOTICE_SENDER``): o recado sai na voz dessa pessoa
    ("Oi, Eugenio aqui."): so o primeiro nome, por ordem do chairman de 16 e 17/out/2026.

    16/out/2026 (chairman): tom natural, como se fosse ele mandando o recado,
    sem cara de sistema: um parágrafo, sem lista, sem colchete, sem "IA",
    "agente", "sistema" ou "bot"; a fala do paciente entre aspas. ``motivo``
    sobrescreve o motivo (guarda de revisão); ``resumo`` entra como "Pelo que
    entendi". Dado sensível é redigido; <= 900 caracteres.
    """
    textos = [t for t in patient_texts if t and t.strip()]
    frase = (motivo or "").strip().rstrip(".") or _REASON_PHRASE.get(
        derive_reason(textos, cause), _REASON_PHRASE[REASON_DEFAULT]
    )
    link = format_wa_link(remote_jid)
    if remetente is None:
        try:
            from app.settings import get_settings

            remetente = getattr(get_settings(), "HANDOFF_NOTICE_SENDER", None)
        except Exception:  # noqa: BLE001 — aviso nunca falha por configuracao
            remetente = None
    # 17/out/2026 (chairman): so o primeiro nome, informal: "Oi, Eugenio aqui."
    primeiro_nome = (remetente or "").strip().split(" ")[0] if remetente else ""
    saudacao = f"Oi, {primeiro_nome} aqui." if primeiro_nome else "Oi!"
    if name and link:
        abertura = f"{saudacao} {name} ({link}) {frase}."
    elif link:
        abertura = f"{saudacao} Tem um contato ({link}) que {frase}."
    elif name:
        abertura = f"{saudacao} {name} {frase} (o contato veio sem número)."
    else:
        abertura = f"{saudacao} Um contato {frase} (veio sem número)."

    resumo_txt = ""
    if resumo and resumo.strip():
        resumo_txt = "Pelo que entendi: " + truncate(
            clean_line(redact_sensitive(resumo)), TRIAGE_SUMMARY_MAX_CHARS
        ).rstrip(".") + "."
    fecho = (
        "Já orientei a procurar atendimento de urgência."
        if derive_reason(textos, cause) == REASON_EMERGENCY and not motivo
        else "Já avisei que alguém da equipe vai continuar o atendimento, consegue chamar?"
    )

    def assemble(falas: list[str]) -> str:
        partes = [abertura]
        if falas:
            partes.append("Escreveu: " + " / ".join(f"“{f}”" for f in falas) + ".")
        if resumo_txt:
            partes.append(resumo_txt)
        partes.append(fecho)
        return " ".join(partes)

    falas = [
        truncate(clean_line(redact_sensitive(t)), PATIENT_LINE_MAX_CHARS)
        for t in textos[-2:]
    ]
    falas = [f for f in falas if f]
    while True:
        notice = assemble(falas)
        if len(notice) <= NOTICE_MAX_CHARS or len(falas) <= 1:
            break
        falas = falas[1:]
    return truncate(notice, NOTICE_MAX_CHARS) if len(notice) > NOTICE_MAX_CHARS else notice


def build_manager_notice(
    *,
    remote_jid: str,
    events: Iterable[Any],
    cause: HandoffCause | None = None,
    resumo: str | None = None,
) -> str:
    """Aviso do handoff do turno: extrai falas e nome dos eventos e delega a
    :func:`build_human_notice` (mesma redação natural, mesmo redigido)."""
    events = list(events)
    inbound_texts = [
        e.text_content.strip()
        for e in events
        if getattr(e, "direction", "inbound") == "inbound"
        and isinstance(getattr(e, "text_content", None), str)
        and e.text_content.strip()
    ]
    return build_human_notice(
        remote_jid=remote_jid,
        patient_texts=inbound_texts,
        name=contact_name(events),
        cause=cause,
        resumo=resumo,
    )
