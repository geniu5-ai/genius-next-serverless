"""Campanhas e promocoes sazonais: dado com validade, injetado so enquanto vigente.

Por que existe (03/nov/2026): campanha vivia como entrada ``promo:*`` do catalogo
(``genius_next.knowledge``), com a validade escrita em prosa ("vale outubro de
2026"), e as regras sobre campanha estavam espalhadas pelos prompts de
personalidade. Nada retirava campanha vencida: a Julia cotou o preco da Semana da
Crianca em novembro e o follow-up, que nao recebia a data, citou "outubro".

Agora campanha e uma linha de ``genius_next.campaigns`` com ``vale_de`` e
``vale_ate`` (inclusivos). O codigo, e nao o modelo, decide o que vale hoje, no
fuso do negocio (America/Fortaleza), e entrega so as vigentes numa chave propria
do contexto JSON, ``campanhas_vigentes``. Sem campanha vigente a chave nao
aparece. Os prompts de personalidade nao falam de nenhuma campanha: tem uma regra
generica ("so existe se estiver em campanhas_vigentes").

Convencao: entradas ``promo:*`` de ``knowledge`` sao ignoradas pelo catalogo
(``app/context/knowledge.py``); campanha so entra por este modulo.

Contrato do projeto: o catalogo nunca derruba o turno. Tabela ausente, erro de SQL
ou linha mal formada devolvem lista vazia (ou pulam a linha) e registram warning.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy import Date, bindparam
from sqlalchemy import text as _sql_text

from app.security.guard import GuardDisposition, scan_invisible_text

logger = logging.getLogger(__name__)

# Chave do contexto JSON (atendimento e follow-up) e do guia de entrada.
CHAVE_CONTEXTO = "campanhas_vigentes"
GUIA_DE_ENTRADA = (
    "única fonte de campanha e promoção; só as que valem hoje; o que não está "
    "aqui não existe e vale a tabela normal; o preço de campanha vem escrito no item"
)

# Prefixo das entradas de ``knowledge`` que o catalogo ignora (campanha so entra
# por este modulo). O SQL de ``knowledge.py`` usa o mesmo literal.
PREFIXO_PROMO_NO_CATALOGO = "promo:"

# Teto de caracteres do bloco inteiro (titulo + texto de cada campanha). Passou
# disso, as campanhas que nao cabem inteiras ficam de fora: nunca se corta uma no
# meio, porque preco cortado e pior que preco ausente.
CAMPANHAS_MAX_CHARS = 6000
# Teto de linhas lidas; campanha vigente simultanea passa de poucas.
_MAX_LINHAS = 50

_VIGENTES_SQL = _sql_text(
    """
    /* campaigns.vigentes */
    SELECT slug, titulo, conteudo, vale_de, vale_ate, ativa
    FROM genius_next.campaigns
    WHERE tenant_id = :tenant_id
      AND ativa
      AND vale_de <= :hoje
      AND vale_ate >= :hoje
    ORDER BY vale_ate, vale_de, slug
    LIMIT 50
    """
).bindparams(bindparam("hoje", type_=Date))


@dataclass(frozen=True)
class Campanha:
    """Uma campanha vigente, ja validada."""

    slug: str
    titulo: str
    conteudo: str
    vale_de: date
    vale_ate: date

    @property
    def valida_ate(self) -> str:
        """Ultimo dia de validade (inclusivo), dd/mm/aaaa."""
        return self.vale_ate.strftime("%d/%m/%Y")

    @property
    def tamanho(self) -> int:
        return len(self.titulo) + len(self.conteudo)


def hoje_no_negocio() -> date:
    """Hoje no fuso do negocio (America/Fortaleza), o mesmo de ``data_atual``."""
    from app.reply.llm import _hoje_negocio

    return _hoje_negocio()


def _como_data(valor: Any) -> date:
    if isinstance(valor, datetime):
        return valor.date()
    if isinstance(valor, date):
        return valor
    if isinstance(valor, str):
        return date.fromisoformat(valor[:10])
    raise TypeError(f"data invalida: {type(valor).__name__}")


def _linha_para_campanha(linha: Any) -> Campanha | None:
    """Linha do banco -> Campanha; inativa ou mal formada vira None (a mal formada, com warning)."""
    try:
        if not linha.get("ativa", True):
            return None
        campanha = Campanha(
            slug=str(linha["slug"]),
            titulo=str(linha["titulo"]).strip(),
            conteudo=str(linha["conteudo"]).strip(),
            vale_de=_como_data(linha["vale_de"]),
            vale_ate=_como_data(linha["vale_ate"]),
        )
    except Exception:
        logger.warning("campanha com linha invalida ignorada", exc_info=True)
        return None
    if not campanha.titulo or not campanha.conteudo:
        logger.warning("campanha %s sem titulo ou texto; ignorada", campanha.slug)
        return None
    # Mesmo scanner do catalogo inline: texto invisivel escondendo instrucao.
    if any(
        scan_invisible_text(texto).disposition == GuardDisposition.BLOCK.value
        for texto in (campanha.titulo, campanha.conteudo)
    ):
        logger.warning("campanha %s com texto invisivel; retida", campanha.slug)
        return None
    return campanha


def _consultar(session: Any, tenant_id: str, hoje: date) -> list[Any]:
    """Linhas cruas do SQL. Roda num SAVEPOINT quando a sessao o oferece.

    Sem isto, tabela ausente deixaria a transacao do Postgres abortada e a
    PROXIMA consulta da mesma sessao (historico, catalogo) falharia com
    ``InFailedSqlTransaction``: o erro da campanha derrubaria o turno.
    """
    begin_nested = getattr(session, "begin_nested", None)
    if begin_nested is None:
        return list(
            session.execute(_VIGENTES_SQL, {"tenant_id": tenant_id, "hoje": hoje})
            .mappings()
            .all()
        )
    with begin_nested():
        return list(
            session.execute(_VIGENTES_SQL, {"tenant_id": tenant_id, "hoje": hoje})
            .mappings()
            .all()
        )


def campanhas_vigentes(
    session: Any,
    tenant_id: str,
    hoje: date | None = None,
    max_chars: int = CAMPANHAS_MAX_CHARS,
) -> list[Campanha]:
    """Campanhas ``ativa`` com ``vale_de <= hoje <= vale_ate`` do tenant.

    ``hoje`` e a data no fuso do negocio (padrao: agora em America/Fortaleza).
    Ordem: a que vence primeiro, depois a que comecou primeiro, depois ``slug``.
    O total de caracteres respeita ``max_chars``: a primeira campanha que nao cabe
    inteira (e todas depois dela) fica de fora. Qualquer falha devolve ``[]``.
    """
    try:
        dia = hoje or hoje_no_negocio()
        linhas = _consultar(session, tenant_id, dia)
    except Exception:
        logger.warning(
            "campanhas indisponiveis para %s; seguindo sem campanha", tenant_id, exc_info=True
        )
        return []
    candidatas = [c for c in (_linha_para_campanha(linha) for linha in linhas) if c is not None]
    # O SQL ja filtra; repetir aqui protege contra um SQL errado e deixa a regra
    # de datas testavel sem Postgres.
    vigentes = [c for c in candidatas if c.vale_de <= dia <= c.vale_ate]
    vigentes.sort(key=lambda c: (c.vale_ate, c.vale_de, c.slug))
    escolhidas: list[Campanha] = []
    total = 0
    for campanha in vigentes:
        if total + campanha.tamanho > max_chars:
            logger.warning(
                "campanhas de %s passam de %d caracteres; %d fora do contexto (a partir de %s)",
                tenant_id,
                max_chars,
                len(vigentes) - len(escolhidas),
                campanha.slug,
            )
            break
        escolhidas.append(campanha)
        total += campanha.tamanho
    return escolhidas


def formatar_para_contexto(campanhas: list[Campanha]) -> list[dict[str, str]]:
    """Itens do contexto: ``titulo``, ``texto`` e ``valida_ate`` (dd/mm/aaaa)."""
    return [
        {"titulo": c.titulo, "texto": c.conteudo, "valida_ate": c.valida_ate}
        for c in campanhas
    ]


def incluir_no_contexto(context: dict[str, Any], campanhas: list[Campanha] | None) -> None:
    """Poe ``campanhas_vigentes`` no contexto; sem campanha a chave nao aparece."""
    if campanhas:
        context[CHAVE_CONTEXTO] = formatar_para_contexto(campanhas)
