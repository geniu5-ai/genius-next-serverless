"""Read-only catalog lookups for clinic knowledge documents."""

from __future__ import annotations

import logging

from sqlalchemy import text as _sql_text
from sqlalchemy.orm import Session

from app.security.guard import GuardDisposition, scan_invisible_text

logger = logging.getLogger(__name__)

# persuasao:* fica fora do catalogo/fatos de proposito: e' copy de venda
# (app/reply/prompt_profiles.py tenant_rules), nao fato verificavel, e a
# propria label "Informacoes verificadas da empresa" que envolve isto vira
# mentira se um texto de persuasao entrar no bloco. Achado real de 29/ago:
# persuasao:protese_flexivel_beneficios injetada aqui, densa em "dente",
# contribuia pra pergunta clinica indevida no preco frio -- nao por conteudo
# errado, por rotulo errado (fato vs venda misturados sob "verificado").
_LIST_CATALOG_SQL = _sql_text(
    """
    /* knowledge.list_catalog */
    SELECT chave, titulo
    FROM genius_next.knowledge
    WHERE tenant_id = :tenant_id
      AND chave NOT LIKE 'persuasao:%'
    ORDER BY chave
    LIMIT 40
    """
)

_FULL_CATALOG_SQL = _sql_text(
    """
    /* knowledge.load_full_catalog */
    SELECT chave, titulo, conteudo
    FROM genius_next.knowledge
    WHERE tenant_id = :tenant_id
      AND chave NOT LIKE 'persuasao:%'
    ORDER BY chave
    LIMIT 200
    """
)

_OPEN_KNOWLEDGE_SQL = _sql_text(
    """
    /* knowledge.open_knowledge */
    SELECT conteudo
    FROM genius_next.knowledge
    WHERE tenant_id = :tenant_id
      AND chave = :chave
    """
)


def list_catalog(session: Session, *, tenant_id: str) -> list[tuple[str, str]]:
    try:
        rows = (
            session.execute(_LIST_CATALOG_SQL, {"tenant_id": tenant_id})
            .mappings()
            .all()
        )
    except Exception:
        return []
    return [
        (str(row["chave"]), str(row["titulo"]))
        for row in rows
    ]


def open_knowledge(session: Session, *, tenant_id: str, chave: str) -> str | None:
    try:
        row = (
            session.execute(
                _OPEN_KNOWLEDGE_SQL,
                {"tenant_id": tenant_id, "chave": chave},
            )
            .mappings()
            .first()
        )
    except Exception:
        return None
    if row is None:
        return None
    content = row["conteudo"]
    if not isinstance(content, str) or not content:
        return None
    return content[:2000]


def load_full_catalog(
    session: Session,
    *,
    tenant_id: str,
    max_chars: int,
) -> list[tuple[str, str, str]]:
    """Todo o catalogo do tenant, para ir colado no prompt.

    Devolve lista vazia quando o catalogo passa de ``max_chars`` — quem chama
    volta para a lista de chaves + ferramenta.  Nao trunca: catalogo cortado no
    meio faz o modelo responder com meia verdade, que e pior que responder que
    nao sabe.

    05/set/2026: cada item passa por ``scan_invisible_text`` antes de sair
    daqui, mesmo scanner deterministico que a ferramenta ``abrir`` (em
    app/reply/tools/knowledge.py) ja aplica por documento. O caminho inline
    nao passava por nenhum scan -- so a ferramenta tinha essa defesa. Nao e' o
    scanner de LLM Guard completo: aquele e treinado em mensagens hostis e da
    falso positivo em texto comercial declarativo (medido 22/ago -- tabela de
    preco pontuou 1.0 para prompt_injection). scan_invisible_text so pega
    Unicode invisivel/formatacao, sem rede e sem falso positivo em texto
    comum -- o mesmo criterio, aplicado no caminho que faltava.
    """
    try:
        rows = (
            session.execute(_FULL_CATALOG_SQL, {"tenant_id": tenant_id})
            .mappings()
            .all()
        )
    except Exception:
        return []
    total = sum(len(str(row["conteudo"])) for row in rows)
    if not rows or total > max_chars:
        return []
    itens: list[tuple[str, str, str]] = []
    for row in rows:
        chave = str(row["chave"])
        conteudo = str(row["conteudo"])
        if scan_invisible_text(conteudo).disposition == GuardDisposition.BLOCK.value:
            logger.warning(
                "catalogo inline de %s: item %s com texto invisivel; retido",
                tenant_id,
                chave,
            )
            continue
        itens.append((chave, str(row["titulo"]), conteudo))
    return itens
