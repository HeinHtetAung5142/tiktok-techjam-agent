"""Read-only product lookup for the web UI.

`respond()` hands back bare `{"parent_asin": ...}` dicts, and the agent's FTS5 table
flattens products into text -- `average_rating` and `rating_number` are not stored there
at all. So the UI needs its own reader.

Rather than holding all 50k parsed rows (~60 MB) or re-parsing the file per request, this
keeps one byte offset per `parent_asin` and seeks to that line on demand. The scan reads
the file once and regexes the id straight out of the raw bytes, so it never pays for
`json.loads` on rows nobody asks for.
"""

from __future__ import annotations

import json
import random
import re
from pathlib import Path


# The id is the first key of every row, but search the whole line anyway -- cheap, and it
# survives a reordered field. Bytes, not str: the scan never decodes lines it skips.
_ASIN_RE = re.compile(rb'"parent_asin"\s*:\s*"([^"]+)"')


class CatalogReader:
    """Offset index over `data/catalog.jsonl`. Never mutates anything."""

    def __init__(self, path: str | Path = "data/catalog.jsonl") -> None:
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(
                f"catalog not found at {self.path} -- see README.md for the download steps"
            )
        self._offsets: dict[str, int] = {}
        self._asins: list[str] = []
        self._scan()

    def _scan(self) -> None:
        offset = 0
        with self.path.open("rb") as handle:
            for line in handle:
                match = _ASIN_RE.search(line)
                if match:
                    asin = match.group(1).decode("utf-8")
                    # First write wins, matching the agent's own index: a duplicate id
                    # would otherwise point the UI at a different row than the one
                    # retrieval actually scored.
                    if asin not in self._offsets:
                        self._offsets[asin] = offset
                        self._asins.append(asin)
                offset += len(line)

    def __len__(self) -> int:
        return len(self._asins)

    def get(self, parent_asin: str) -> dict | None:
        return self.get_many([parent_asin]).get(parent_asin)

    def get_many(self, parent_asins: list[str]) -> dict[str, dict]:
        """One file handle for the whole batch -- a turn hydrates ~50 rows at once."""
        products: dict[str, dict] = {}
        wanted = [asin for asin in parent_asins if asin in self._offsets]
        if not wanted:
            return products
        with self.path.open("rb") as handle:
            # Ascending offsets keep the seeks moving forward through the file.
            for asin in sorted(wanted, key=self._offsets.__getitem__):
                handle.seek(self._offsets[asin])
                line = handle.readline()
                try:
                    products[asin] = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
        return products

    def random_asin(self, rng: random.Random | None = None) -> str:
        return (rng or random).choice(self._asins)


def card(product: dict, feature_limit: int | None = 3) -> dict:
    """The display fields the UI renders. Everything else in the row is dropped."""
    features = [str(item) for item in (product.get("features") or []) if str(item).strip()]
    price = product.get("price")
    return {
        "parent_asin": product.get("parent_asin", ""),
        "title": str(product.get("title") or "(untitled product)"),
        "store": str(product.get("store") or ""),
        # Prices are frequently null in this catalog; let the page decide how to say so.
        "price": float(price) if isinstance(price, (int, float)) else None,
        "average_rating": product.get("average_rating"),
        "rating_number": product.get("rating_number"),
        "categories": [str(item) for item in (product.get("categories") or [])],
        "features": features[:feature_limit] if feature_limit else features,
    }
