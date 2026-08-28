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

    def _browsing_query(self, base_expression: str, top_k: int) -> list[dict]:
        rows = self.connection.execute(
            "SELECT parent_asin FROM products WHERE products MATCH ? "
            "ORDER BY bm25(products, 0.0, 6.0, 4.0, 2.5, 2.5, 1.5, 1.0, 0.0) LIMIT ?",
            (base_expression, top_k),
        ).fetchall()
        return [{"parent_asin": str(row[0])} for row in rows]

    def _buying_query(self, base_expression: str, state: dict, top_k: int) -> list[dict]:
        filtered_expr = f"({base_expression})"
        if state["color"]:
            filtered_expr += f' AND "{state["color"]}"'
        if state["material"]:
            filtered_expr += f' AND "{state["material"]}"'

        sql = (
            "SELECT parent_asin FROM products WHERE products MATCH ? "
        )
        params: list[object] = [filtered_expr]
        if state["price_max"] is not None:
            sql += "AND (price IS NULL OR price <= ?) "
            params.append(state["price_max"])
        sql += "ORDER BY bm25(products, 0.0, 6.0, 4.0, 2.5, 2.5, 1.5, 1.0, 0.0) LIMIT ?"
        params.append(top_k * OVERFETCH_MULT)

        rows = self.connection.execute(sql, params).fetchall()
        candidates = [str(row[0]) for row in rows]

        if len(candidates) < top_k:
            seen = set(candidates)
            for item in self._browsing_query(base_expression, top_k * OVERFETCH_MULT):
                parent_asin = item["parent_asin"]
                if parent_asin not in seen:
                    seen.add(parent_asin)
                    candidates.append(parent_asin)
                if len(candidates) >= top_k:
                    break

        return [{"parent_asin": parent_asin} for parent_asin in candidates[:top_k]]

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

        if not base_expression:
            recommendations: list[dict] = []
        elif is_buying:
            recommendations = self._buying_query(base_expression, state, top_k)
        else:
            recommendations = self._browsing_query(base_expression, top_k)

        return {
            "message": self._compose_message(is_buying, state),
            "ask_attribute": None,
            "recommendations": recommendations,
            "usage": {"prompt_tokens": 0, "completion_tokens": 0},
        }
