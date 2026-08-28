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

COLOR_RE = re.compile(
    r"\b(black|white|blue|red|pink|green|brown|gray|grey|purple|yellow|orange)\b", re.IGNORECASE
)
MATERIAL_RE = re.compile(
    r"\b(cotton|polyester|nylon|leather|wool|spandex|silk|rayon|fabric)\b", re.IGNORECASE
)
PRICE_DOLLAR_RE = re.compile(r"\$\s*(\d+(?:\.\d{1,2})?)")
PRICE_PHRASE_RE = re.compile(
    r"(?:under|below|less than|no more than|up to|at most)\s+\$?\s*(\d+(?:\.\d{1,2})?)", re.IGNORECASE
)
OVERFETCH_MULT = 5
CATEGORY_ROUTE_WEIGHT = 0.3


def _detect_constraints(message: str) -> dict[str, float | str | None]:
    color_match = COLOR_RE.search(message)
    material_match = MATERIAL_RE.search(message)
    price_match = PRICE_DOLLAR_RE.search(message) or PRICE_PHRASE_RE.search(message)
    return {
        "color": color_match.group(1).lower() if color_match else None,
        "material": material_match.group(1).lower() if material_match else None,
        "price_max": float(price_match.group(1)) if price_match else None,
    }


def _text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        return " ".join(f"{key} {item}" for key, item in value.items())
    if isinstance(value, list):
        return " ".join(str(item) for item in value)
    return str(value)


def _terms(text: str) -> list[str]:
    return [
        token.lower()
        for token in TOKEN_RE.findall(text)
        if len(token) > 1 and token.lower() not in STOPWORDS
    ]


def _with_and_terms(expression: str, and_terms: list[str]) -> str:
    result = f"({expression})"
    for term in and_terms:
        result += f' AND "{term}"'
    return result


class Agent:
    """Editable weak baseline: stateless BM25 retrieval with no LLM dependency."""

    def __init__(self, catalog_path: str | Path = "data/catalog.jsonl") -> None:
        self.catalog_path = Path(catalog_path)
        self.connection = sqlite3.connect(":memory:")
        self._sessions: dict[str, dict[str, float | str | None]] = {}
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
                        _text(product.get("title")),
                        _text(product.get("categories")),
                        _text(product.get("features")),
                        _text(product.get("details")),
                        _text(product.get("store")),
                        _text(product.get("description")),
                        product.get("price"),
                    )
                )
                if len(batch) >= 1000:
                    cursor.executemany("INSERT INTO products VALUES (?, ?, ?, ?, ?, ?, ?, ?)", batch)
                    batch.clear()
        if batch:
            cursor.executemany("INSERT INTO products VALUES (?, ?, ?, ?, ?, ?, ?, ?)", batch)
        self.connection.commit()

    def reset(self, session_id: str, user_profile: dict) -> None:
        # The profile is anonymized and may be used for personalization.
        self._sessions[session_id] = {"price_max": None, "color": None, "material": None}

    def _run_ranked_query(self, match_expression: str, price_max: float | None, limit: int) -> list[str]:
        sql = "SELECT parent_asin FROM products WHERE products MATCH ? "
        params: list[object] = [match_expression]
        if price_max is not None:
            sql += "AND (price IS NULL OR price <= ?) "
            params.append(price_max)
        sql += "ORDER BY bm25(products, 0.0, 6.0, 4.0, 2.5, 2.5, 1.5, 1.0, 0.0) LIMIT ?"
        params.append(limit)
        rows = self.connection.execute(sql, params).fetchall()
        return [str(row[0]) for row in rows]

    @staticmethod
    def _fuse_rankings(
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

    def _retrieve(
        self,
        base_expression: str,
        unique_terms: list[str],
        and_terms: list[str],
        price_max: float | None,
        top_k: int,
    ) -> list[dict]:
        limit = top_k * OVERFETCH_MULT

        # Route 1: keyword route — the original whole-catalog BM25 search.
        keyword_ids = self._run_ranked_query(_with_and_terms(base_expression, and_terms), price_max, limit)

        # Route 2: category route — restricts the match to just the categories column,
        # so a strong category match isn't diluted by noisy scores from title/description.
        category_ids: list[str] = []
        if unique_terms:
            category_base = "categories:(" + " OR ".join(f'"{term}"' for term in unique_terms) + ")"
            category_ids = self._run_ranked_query(_with_and_terms(category_base, and_terms), price_max, limit)

        merged = self._fuse_rankings([(keyword_ids, 1.0), (category_ids, CATEGORY_ROUTE_WEIGHT)], top_k)

        # Safety net: if hard filters (buying track) narrowed things too far, backfill
        # from an unfiltered wide search rather than returning too few recommendations.
        if len(merged) < top_k:
            seen = set(merged)
            for parent_asin in self._run_ranked_query(base_expression, None, limit):
                if parent_asin not in seen:
                    seen.add(parent_asin)
                    merged.append(parent_asin)
                if len(merged) >= top_k:
                    break

        return [{"parent_asin": parent_asin} for parent_asin in merged[:top_k]]

    def _compose_message(self, is_buying: bool, state: dict) -> str:
        if not is_buying:
            return "Here are the closest matches I found."
        parts = [str(value) for value in (state["color"], state["material"]) if value]
        if state["price_max"] is not None:
            parts.append(f"under ${state['price_max']:.2f}")
        detail = ", ".join(parts) if parts else "your requirement"
        return f"Narrowed to items matching {detail}."

    def respond(
        self,
        session_id: str,
        user_message: str,
        turn: int,
        top_k: int,
    ) -> dict:
        if session_id not in self._sessions:
            raise RuntimeError("reset must be called before respond")
        state = self._sessions[session_id]
        detected = _detect_constraints(user_message)
        for key, value in detected.items():
            if value is not None and state[key] is None:
                state[key] = value

        unique_terms = list(dict.fromkeys(_terms(user_message)))[:40]
        base_expression = " OR ".join(f'"{term}"' for term in unique_terms)
        is_buying = any(state[key] is not None for key in ("price_max", "color", "material"))
        and_terms = [str(state[key]) for key in ("color", "material") if state[key]]

        if not base_expression:
            recommendations: list[dict] = []
        else:
            recommendations = self._retrieve(
                base_expression,
                unique_terms,
                and_terms if is_buying else [],
                state["price_max"] if is_buying else None,
                top_k,
            )

        return {
            "message": self._compose_message(is_buying, state),
            "ask_attribute": None,
            "recommendations": recommendations,
            "usage": {"prompt_tokens": 0, "completion_tokens": 0},
        }
