"""Local business data — the bot's structured "database".

This is the ground truth the tools (see ``tools.py``) read from, so answers about
products, stock, and orders are looked up rather than invented. It is deliberately
a plain local layer — JSON files under ``data/<business-key>/`` loaded into memory
— with no external database, embeddings, or network calls:

  * a small store catalog fits comfortably in memory and is searched with plain
    substring matching, which is *deterministic* (no fuzzy-retrieval surprises)
    and instant (no embedding step in the response path), and
  * it keeps the demo fully local and dependency-free.

Answering from *unstructured* prose (multi-paragraph policies, warranty text, FAQs)
is a different job, handled separately by the RAG layer in ``rag.py`` (the
``search_docs`` tool, backed by a local embedding store). The structured catalog
here is the wrong tool for prose and the right tool for a product list, so it stays
lookup-based; ``rag.py`` is the wrong tool for a product list and the right tool for
prose. Its ``DocStore`` is attached onto ``BusinessData.docs`` at boot so both reach
the tool handlers through the same ``app_resources``.

Data is per-business (keyed by ``BUSINESS`` / the profile key), so swapping the
use case swaps the data folder with it. Missing files degrade gracefully to an
empty catalog rather than crashing the bot.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from rag import DocStore

# Data lives next to this file, partitioned by business key: data/<key>/*.json.
DATA_ROOT = Path(__file__).parent / "data"


def _read_json(path: Path, default: Any) -> Any:
    """Read a JSON file, returning ``default`` (and warning) if it's absent/bad."""
    if not path.exists():
        logger.warning(f"data | {path} not found; using empty {type(default).__name__}")
        return default
    try:
        with path.open() as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"data | could not read {path}: {e}; using empty default")
        return default


@dataclass
class BusinessData:
    """In-memory view of one business's products and orders.

    Attributes:
        key: The business/profile key this data belongs to (e.g. ``"store"``).
        products: List of product records (see ``data/store/products.json``).
        orders: Order records keyed by order number (string).
        docs: Optional semantic index over the business's FAQ/policy prose
            (see ``rag.py``), attached after load and read by the ``search_docs``
            tool. ``None`` for a profile with no ``docs/`` folder.
    """

    key: str
    products: list[dict] = field(default_factory=list)
    orders: dict[str, dict] = field(default_factory=dict)
    docs: DocStore | None = None

    @classmethod
    def load(cls, key: str) -> BusinessData:
        """Load the data folder for ``key`` (``data/<key>/``) into memory."""
        base = DATA_ROOT / key
        products = _read_json(base / "products.json", default=[])
        orders = _read_json(base / "orders.json", default={})
        logger.info(f"data | {key}: {len(products)} products, {len(orders)} orders loaded")
        return cls(key=key, products=products, orders=orders)

    def categories(self) -> list[str]:
        """Return the distinct product categories, in first-seen order."""
        seen: list[str] = []
        for p in self.products:
            category = p.get("category")
            if category and category not in seen:
                seen.append(category)
        return seen

    def search_products(
        self, query: str | None = None, category: str | None = None
    ) -> list[dict]:
        """Return products matching an optional free-text query and/or category.

        Matching is case-insensitive substring across name, category, tags, and
        description. With neither argument, returns the whole catalog (callers
        can then summarize by category).
        """
        results = self.products
        if category:
            wanted = category.strip().lower()
            results = [p for p in results if wanted in p.get("category", "").lower()]
        if query:
            needle = query.strip().lower()
            results = [p for p in results if needle in self._haystack(p)]
        return results

    def find_product(self, name: str) -> dict | None:
        """Return the single best product match for ``name`` (or ``None``)."""
        matches = self.search_products(query=name)
        return matches[0] if matches else None

    def get_order(self, order_number: str) -> dict | None:
        """Return the order record for ``order_number`` (or ``None`` if unknown)."""
        return self.orders.get(str(order_number).strip())

    @staticmethod
    def _haystack(product: dict) -> str:
        """Lowercased searchable text for a product record."""
        tags = " ".join(product.get("tags", []))
        return " ".join(
            [
                product.get("name", ""),
                product.get("category", ""),
                product.get("description", ""),
                tags,
            ]
        ).lower()
