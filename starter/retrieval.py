"""Catalog indexing and multi-route retrieval.

Owns everything between "here is some text" and "here is a ranked list of parent_asins":
the in-memory FTS5 index, the query routes, and rank fusion. Knows nothing about
conversations — see dialog_state.py for that.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path


TOKEN_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)
STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "for", "from",
    "i", "in", "is", "it", "me", "my", "of", "on", "or", "please", "some",
    "that", "the", "this", "to", "want", "with", "would", "you", "looking",
}

OVERFETCH_MULT = 5
CATEGORY_ROUTE_WEIGHT = 0.3

# BM25 column weights, in table order: parent_asin, title, categories, features,
# details, store, description, price. A product's title is far more diagnostic of
# what it is than its marketing copy, so it dominates.
BM25_WEIGHTS = (0.0, 6.0, 4.0, 2.5, 2.5, 1.5, 1.0, 0.0)


def flatten(value: object) -> str:
    """Render a catalog field (str, list, or dict) as one indexable string."""
    if value is None:
        return ""
    if isinstance(value, dict):
        return " ".join(f"{key} {item}" for key, item in value.items())
    if isinstance(value, list):
        return " ".join(str(item) for item in value)
    return str(value)


def terms(text: str) -> list[str]:
    """Tokenize, drop stopwords and single characters, preserving first-seen order."""
    return list(
        dict.fromkeys(
            token.lower()
            for token in TOKEN_RE.findall(text)
            if len(token) > 1 and token.lower() not in STOPWORDS
        )
    )


def with_and_terms(expression: str, and_terms: list[str]) -> str:
    """Wrap an FTS5 expression so every term in `and_terms` is also required."""
    result = f"({expression})"
    for term in and_terms:
        result += f' AND "{term}"'
    return result


def or_expression(query_terms: list[str], column: str | None = None) -> str:
    """Build an FTS5 OR expression, optionally scoped to a single column."""
    if not query_terms:
        return ""
    joined = " OR ".join(f'"{term}"' for term in query_terms)
    return f"{column}:({joined})" if column else joined


class CatalogIndex:
    """In-memory SQLite FTS5 index over the frozen 50k-product catalog."""

    def __init__(self, catalog_path: str | Path = "data/catalog.jsonl") -> None:
        self.catalog_path = Path(catalog_path)
        self.connection = sqlite3.connect(":memory:")
        self._build_index()

    def _build_index(self) -> None:
        cursor = self.connection.cursor()
        cursor.execute(
            "CREATE VIRTUAL TABLE products USING fts5("
            "parent_asin UNINDEXED, title, categories, features, details, store, description, "
            "price UNINDEXED, "
            "tokenize='unicode61 remove_diacritics 2')"
        )
        batch: list[tuple[str, str, str, str, str, str, str, float | None]] = []
        with self.catalog_path.open(encoding="utf-8") as handle:
            for line in handle:
                product = json.loads(line)
                batch.append(
                    (
                        str(product["parent_asin"]),
                        flatten(product.get("title")),
                        flatten(product.get("categories")),
                        flatten(product.get("features")),
                        flatten(product.get("details")),
                        flatten(product.get("store")),
                        flatten(product.get("description")),
                        product.get("price"),
                    )
                )
                if len(batch) >= 1000:
                    cursor.executemany("INSERT INTO products VALUES (?, ?, ?, ?, ?, ?, ?, ?)", batch)
                    batch.clear()
        if batch:
            cursor.executemany("INSERT INTO products VALUES (?, ?, ?, ?, ?, ?, ?, ?)", batch)
        self.connection.commit()

    def run_ranked_query(
        self, match_expression: str, price_max: float | None, limit: int
    ) -> list[str]:
        sql = "SELECT parent_asin FROM products WHERE products MATCH ? "
        params: list[object] = [match_expression]
        if price_max is not None:
            # Keep null-priced products: a missing price is not evidence of being expensive.
            sql += "AND (price IS NULL OR price <= ?) "
            params.append(price_max)
        weights = ", ".join(str(weight) for weight in BM25_WEIGHTS)
        sql += f"ORDER BY bm25(products, {weights}) LIMIT ?"
        params.append(limit)
        rows = self.connection.execute(sql, params).fetchall()
        return [str(row[0]) for row in rows]

    @staticmethod
    def fuse_rankings(
        weighted_lists: list[tuple[list[str], float]], top_k: int, rrf_k: int = 60
    ) -> list[str]:
        # Reciprocal Rank Fusion: an item's score is the weighted sum of 1/(rrf_k + rank)
        # across every route that surfaced it. The keyword route is weighted highest since
        # it's the proven primary signal; the category route can still rescue an item the
        # keyword route buried or missed, but can't casually outrank keyword's own top picks.
        scores: dict[str, float] = {}
        for ranked, weight in weighted_lists:
            for index, parent_asin in enumerate(ranked):
                scores[parent_asin] = scores.get(parent_asin, 0.0) + weight / (rrf_k + index + 1)
        ordered = sorted(scores.items(), key=lambda pair: pair[1], reverse=True)
        return [parent_asin for parent_asin, _ in ordered[:top_k]]

    def retrieve(
        self,
        query_terms: list[str],
        and_terms: list[str],
        price_max: float | None,
        top_k: int,
    ) -> list[dict]:
        base_expression = or_expression(query_terms)
        if not base_expression:
            return []

        limit = top_k * OVERFETCH_MULT

        # Route 1: keyword route — whole-catalog BM25 search.
        keyword_ids = self.run_ranked_query(
            with_and_terms(base_expression, and_terms), price_max, limit
        )

        # Route 2: category route — restricts the match to just the categories column,
        # so a strong category match isn't diluted by noisy scores from title/description.
        category_base = or_expression(query_terms, column="categories")
        category_ids = self.run_ranked_query(
            with_and_terms(category_base, and_terms), price_max, limit
        )

        merged = self.fuse_rankings(
            [(keyword_ids, 1.0), (category_ids, CATEGORY_ROUTE_WEIGHT)], top_k
        )

        # Safety net: if hard filters (buying track) narrowed things too far, backfill
        # from an unfiltered wide search rather than returning too few recommendations.
        if len(merged) < top_k:
            seen = set(merged)
            for parent_asin in self.run_ranked_query(base_expression, None, limit):
                if parent_asin not in seen:
                    seen.add(parent_asin)
                    merged.append(parent_asin)
                if len(merged) >= top_k:
                    break

        return [{"parent_asin": parent_asin} for parent_asin in merged[:top_k]]
