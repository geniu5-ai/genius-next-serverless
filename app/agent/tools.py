"""agent indexed tools — direct PG lookups for product catalog and policies.

These are the "indexed tool" pattern: 2ms SQL queries on structured data
that should NEVER go through RAG/embedding.  Prices, policies, schedules —
anything where a wrong answer costs money or trust.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text as _sql_text
from sqlalchemy.orm import Session

_INTERNAL = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ProductResult(BaseModel):
    model_config = _INTERNAL

    found: bool
    name: str
    category: str | None = None
    description: str | None = None
    price_info: dict[str, Any] = Field(default_factory=dict)


class PolicyResult(BaseModel):
    model_config = _INTERNAL

    found: bool
    topic: str
    content: str | None = None
    category: str | None = None


class ProductCatalog(BaseModel):
    model_config = _INTERNAL

    products: list[ProductResult]
    total: int


# ── SQL ──────────────────────────────────────────────────────────────────

_PRODUCT_LOOKUP_SQL = _sql_text(
    """
    /* agent.product_lookup */
    SELECT name, category, description, price_info
    FROM genius_next.products
    WHERE tenant_id = :tenant_id
      AND active = true
      AND (
          unaccent(name) ILIKE unaccent(:query)
          OR unaccent(category) ILIKE unaccent(:query)
      )
    LIMIT 5
    """
)

_POLICY_LOOKUP_SQL = _sql_text(
    """
    /* agent.policy_lookup */
    SELECT topic, content, category
    FROM genius_next.policies
    WHERE tenant_id = :tenant_id
      AND active = true
      AND unaccent(topic) ILIKE unaccent(:query)
    LIMIT 3
    """
)

_PRODUCT_CATALOG_SQL = _sql_text(
    """
    /* agent.product_catalog */
    SELECT name, category FROM genius_next.products
    WHERE tenant_id = :tenant_id AND active = true
    ORDER BY category, name
    """
)


# ── Tools ────────────────────────────────────────────────────────────────

def get_product(
    session: Session,
    *,
    tenant_id: str,
    name: str,
) -> ProductResult | None:
    """Look up a product by name (fuzzy match) in the tenant catalog.

    Returns price, description, and structured price_info (cash_price,
    installments, max_parcels, etc.).
    """
    query = f"%{name}%"
    row = (
        session.execute(
            _PRODUCT_LOOKUP_SQL,
            {"tenant_id": tenant_id, "query": query},
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    return ProductResult(
        found=True,
        name=row["name"],
        category=row["category"],
        description=row["description"],
        price_info=dict(row["price_info"] or {}),
    )
