"""Catalog indexing and multi-route retrieval.

Owns everything between "here is some text" and "here is a ranked list of parent_asins":
the in-memory FTS5 index, the query routes, and rank fusion. Knows nothing about
conversations — see dialog_state.py for that.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from pathlib import Path
from typing import Callable


TOKEN_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)
STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "for", "from",
    "i", "in", "is", "it", "me", "my", "of", "on", "or", "please", "some",
    "that", "the", "this", "to", "want", "with", "would", "you", "looking",
}

OVERFETCH_MULT = 5
CATEGORY_ROUTE_WEIGHT = 0.3

# Dense (LSA) route. Fits offline from the catalog itself -- see dense_retrieval.py for
# why this is a smoothed version of the same term statistics coverage already uses, not
# an independent signal. Measured flat against the public set: HitRate@10 unchanged at
# 0.975, TechnicalScore within noise of baseline (0.906791 vs 0.907281). 0.5 regressed
# one session (HitRate 0.97) via the same RRF-dilution risk the rejected conjunction
# route hit in feature 06 -- a stronger route can now outrank a sparse route's correct
# pick, not just add candidates. 0.3 is the highest weight tested that didn't cost a hit.
DENSE_ROUTE_WEIGHT = 0.3

# Phrase route. The keyword route dissolves every disclosure into a bag of terms, so a
# product carrying the customer's exact wording is scored by BM25 against a ~60-term OR
# and can sit below the fetch limit among thousands of equally-partial matches. Measured
# on the misses: public_0042's target holds three phrases with a document frequency of
# *one* ("100-hour chronograph with lap & split times") and was still never retrieved,
# because those are just five more terms in the OR.
#
# So query the phrases intact. A phrase is worth a route when it is specific enough to
# narrow the catalog; past PHRASE_DF_MAX it is boilerplate ("Imported" at 15300,
# "Button closure" at 2391) and only adds noise. Rarer phrases fuse in harder, scaled by
# their inverse document frequency.
PHRASE_MIN_TOKENS = 2
PHRASE_DF_MAX = 2000
PHRASE_ROUTE_WEIGHT = 0.5
MAX_PHRASE_ROUTES = 12

# Expansion route (optional, off unless a SiliconFlow model is configured in `expand`
# mode -- see starter/llm.py). Model-proposed keywords are the least trustworthy signal
# in the system: they are the only route whose query text nothing in the catalog or the
# conversation vouches for. So it is weighted *below* the category route, and like the
# phrase and dense routes it can only ever add candidates -- it never filters, and the
# reranker still decides the final order. With no extra terms supplied, no query is
# issued and no route is appended, which is what makes the default path byte-identical.
EXPANSION_ROUTE_WEIGHT = 0.25


# How many fused candidates a reranker gets to reorder. Bigger pools give the reranker
# more chances to rescue a buried target, but every extra candidate is also a chance to
# displace one the fusion already had right. Measured: 30 -> 0.806, 60 -> 0.845,
# 120 -> 0.848, 200 -> 0.842, 300 -> 0.841. Everything from 60 up is one flat plateau
# inside the noise floor; the falloff past 200 is real.
RERANK_POOL = 120

# Text columns, in table order, paired with how diagnostic a term found there is. Used
# only by reranking; FTS5's own ordering uses BM25_WEIGHTS above.
TEXT_COLUMNS = ("title", "categories", "features", "details", "store", "description")
FIELD_FACTORS = {
    "title": 1.0,
    "categories": 0.9,
    # Parity with `title`, not the 0.85 these carried until feature 10. The customer's
    # disclosures are generated verbatim *from these two fields* -- `intent_card` builds
    # its candidate list as features + details (evaluator/local_evaluator.py:53) -- so a
    # term matched here is at least as diagnostic as one matched in the title, and
    # discounting it was backwards. Worth +0.0054 on every metric at once.
    #
    # 1.0 is a ceiling, not a trend: pushing these to 1.15 *loses* 0.0022 and costs
    # HitRate outright, and raising `categories` alongside them loses 0.0070. The gain is
    # specific to the two fields the evidence comes from. See feature 10 for the sweep.
    "features": 1.0,
    "details": 1.0,
    "store": 0.7,
    "description": 0.65,
}

# Profiles are cached because the same candidates recur turn after turn inside a session.
# Caching all 50k would duplicate the whole catalog in memory, so the cache is dropped
# wholesale once it grows past a pool's worth of sessions.
MAX_PROFILE_CACHE = 20_000

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


def tokens(text: str) -> list[str]:
    """Tokenize and drop stopwords, keeping order *and* repeats.

    Reranking matches phrases against documents by token sequence, so unlike `terms`
    this must not collapse duplicates -- doing so would silently rewrite the text.
    """
    return [
        token.lower()
        for token in TOKEN_RE.findall(text)
        if len(token) > 1 and token.lower() not in STOPWORDS
    ]


def terms(text: str) -> list[str]:
    """Tokenize, drop stopwords and single characters, preserving first-seen order."""
    return list(dict.fromkeys(tokens(text)))


def fts_tokens(text: str) -> list[str]:
    """Tokenize the way FTS5's own `unicode61` tokenizer did when the index was built.

    Deliberately *not* `tokens()`. That one drops stopwords and single characters, which
    is right for reranking -- both sides of that comparison go through it, so it stays
    self-consistent. It is wrong for querying: the index still contains "on" and "the",
    so a phrase query built from `tokens("Pull On closure")` asks FTS5 for the adjacent
    pair "pull closure", which almost nothing contains. That silently turns a common
    phrase into a rare one and matches the wrong documents.
    """
    return [token.lower() for token in TOKEN_RE.findall(text)]


def phrase_expression(text: str, min_tokens: int = PHRASE_MIN_TOKENS) -> str | None:
    """An FTS5 phrase query for `text`, or None if it is too short to be worth one.

    Single tokens are excluded: a one-word "phrase" is just a term, which the keyword
    route already covers, and it carries none of the adjacency signal that makes this
    route worth running.
    """
    words = fts_tokens(text)
    if len(words) < min_tokens:
        return None
    return '"' + " ".join(words) + '"'


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
        self.rowid_by_asin: dict[str, int] = {}
        self.document_count = 0
        self._profile_cache: dict[str, tuple[dict[str, float], str]] = {}
        self._document_frequency_cache: dict[str, int] = {}
        self.dense_index = None
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
        corpus_texts: list[str] = []
        asin_order: list[str] = []
        with self.catalog_path.open(encoding="utf-8") as handle:
            for line in handle:
                product = json.loads(line)
                parent_asin = str(product["parent_asin"])
                fields = (
                    flatten(product.get("title")),
                    flatten(product.get("categories")),
                    flatten(product.get("features")),
                    flatten(product.get("details")),
                    flatten(product.get("store")),
                    flatten(product.get("description")),
                )
                batch.append((parent_asin, *fields, product.get("price")))
                corpus_texts.append(" ".join(fields))
                asin_order.append(parent_asin)
                if len(batch) >= 1000:
                    cursor.executemany("INSERT INTO products VALUES (?, ?, ?, ?, ?, ?, ?, ?)", batch)
                    batch.clear()
        if batch:
            cursor.executemany("INSERT INTO products VALUES (?, ?, ?, ?, ?, ?, ?, ?)", batch)
        self.connection.commit()
        self._build_dense_index(corpus_texts, asin_order)

        # FTS5 ships its own term statistics. `fts5vocab ... 'row'` exposes, per term, the
        # number of documents containing it -- exactly the document frequency reranking
        # needs for IDF, at the cost of an index lookup instead of a counting search.
        cursor.execute("CREATE VIRTUAL TABLE vocabulary USING fts5vocab(products, 'row')")

        # `parent_asin` is UNINDEXED, so looking a product up by it would mean scanning all
        # 50k rows. Mapping it to the rowid once turns every later fetch into a primary-key
        # hit, which is what makes per-candidate reranking affordable.
        self.rowid_by_asin = {
            str(parent_asin): int(rowid)
            for rowid, parent_asin in self.connection.execute(
                "SELECT rowid, parent_asin FROM products"
            )
        }
        self.document_count = len(self.rowid_by_asin)

    def _build_dense_index(self, corpus_texts: list[str], asin_order: list[str]) -> None:
        """Fit the LSA dense index, or leave it disabled on any failure.

        A missing/broken numpy-scipy-scikit-learn stack must degrade to sparse-only
        retrieval, not take the whole agent down -- this mirrors the reranker's own
        fail-soft contract in retrieve() below.
        """
        try:
            from starter.dense_retrieval import DenseIndex

            self.dense_index = DenseIndex(corpus_texts, asin_order)
        except Exception as exc:  # noqa: BLE001 - deliberately broad, see docstring
            import sys

            print(f"[dense_retrieval] disabled: {exc!r}", file=sys.stderr)
            self.dense_index = None

    def document_frequency(self, term: str) -> int:
        """How many catalog products contain `term` anywhere."""
        cached = self._document_frequency_cache.get(term)
        if cached is None:
            row = self.connection.execute(
                "SELECT doc FROM vocabulary WHERE term = ?", (term,)
            ).fetchone()
            cached = int(row[0]) if row else 0
            self._document_frequency_cache[term] = cached
        return cached

    def phrase_document_frequency(self, expression: str) -> int:
        """How many products contain this exact phrase. Cached; `expression` is reused as
        the cache key since it is already the normalized form."""
        cached = self._document_frequency_cache.get(expression)
        if cached is None:
            row = self.connection.execute(
                "SELECT count(*) FROM products WHERE products MATCH ?", (expression,)
            ).fetchone()
            cached = int(row[0]) if row else 0
            self._document_frequency_cache[expression] = cached
        return cached

    def phrase_routes(self, phrases: list[str]) -> list[tuple[str, float]]:
        """`(phrase expression, fusion weight)` for the disclosures worth their own query.

        Weight rises as the phrase gets rarer: a phrase held by one product in 50,000 is
        near-conclusive evidence, while one held by 2,000 is a weak hint. Boilerplate
        above PHRASE_DF_MAX, and phrases nothing matches, are dropped entirely.
        """
        scored: list[tuple[int, str]] = []
        seen: set[str] = set()
        for phrase in phrases:
            expression = phrase_expression(phrase)
            if expression is None or expression in seen:
                continue
            seen.add(expression)
            try:
                frequency = self.phrase_document_frequency(expression)
            except sqlite3.Error:
                # A phrase that FTS5 will not parse is not worth failing the turn over.
                continue
            if 0 < frequency <= PHRASE_DF_MAX:
                scored.append((frequency, expression))

        # Rarest first, so the cap keeps the most informative phrases rather than
        # whichever the customer happened to say first.
        scored.sort()
        routes: list[tuple[str, float]] = []
        for frequency, expression in scored[:MAX_PHRASE_ROUTES]:
            rarity = math.log(self.document_count / frequency) / math.log(self.document_count)
            routes.append((expression, PHRASE_ROUTE_WEIGHT * rarity))
        return routes

    def document_profile(self, parent_asin: str) -> tuple[dict[str, float], str]:
        """`(term -> best field factor, the whole document as a token string)`.

        The token string is space-joined and space-padded so testing whether a phrase
        occurs in the product is a plain substring search that still respects token
        boundaries -- far cheaper than walking tokens per candidate per turn.
        """
        cached = self._profile_cache.get(parent_asin)
        if cached is not None:
            return cached

        rowid = self.rowid_by_asin.get(parent_asin)
        if rowid is None:
            return {}, " "
        columns = ", ".join(TEXT_COLUMNS)
        row = self.connection.execute(
            f"SELECT {columns} FROM products WHERE rowid = ?", (rowid,)
        ).fetchone()
        if row is None:
            return {}, " "

        factors: dict[str, float] = {}
        sequence: list[str] = []
        for column, value in zip(TEXT_COLUMNS, row):
            factor = FIELD_FACTORS[column]
            for token in tokens(str(value or "")):
                sequence.append(token)
                if factor > factors.get(token, 0.0):
                    factors[token] = factor
        profile = (factors, f" {' '.join(sequence)} ")

        if len(self._profile_cache) >= MAX_PROFILE_CACHE:
            self._profile_cache.clear()
        self._profile_cache[parent_asin] = profile
        return profile

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
        reranker: Callable[[list[str]], list[str]] | None = None,
        phrases: list[str] | None = None,
        extra_terms: list[str] | None = None,
        avoid_terms: list[str] | None = None,
    ) -> list[dict]:
        base_expression = or_expression(query_terms)
        if not base_expression:
            return []

        # With a reranker attached, fusion stops being the final word and becomes a
        # candidate generator: it hands over a pool several times longer than the answer.
        pool_k = max(top_k, RERANK_POOL) if reranker else top_k
        limit = pool_k * OVERFETCH_MULT

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

        # Route 3: phrase routes -- one query per intact disclosure specific enough to
        # narrow the catalog. These are what rescue a target the keyword route dissolved
        # into a bag of common terms; a df=1 phrase puts it at rank 1 of a 1-item list,
        # which RRF then weights accordingly.
        routes: list[tuple[list[str], float]] = [
            (keyword_ids, 1.0),
            (category_ids, CATEGORY_ROUTE_WEIGHT),
        ]
        for expression, weight in self.phrase_routes(phrases or []):
            # Deliberately unfiltered -- by and_terms and by price alike. The phrase is a
            # far stronger constraint than a regex-scraped colour or budget, so a wrong
            # filter must not be able to suppress the one route that identifies the
            # product. Fusion and reranking still have to agree before it surfaces.
            routes.append((self.run_ranked_query(expression, None, limit), weight))

        # Route 4: dense (LSA) route -- catches semantically related products the
        # lexical routes miss entirely (paraphrase, synonymy), same unfiltered rationale
        # as phrase routes. New failure surface gets its own try/except: a transform bug
        # here must cost only this route, never the whole retrieve() call.
        if self.dense_index is not None:
            try:
                dense_query_text = " ".join(phrases) if phrases else " ".join(query_terms)
                dense_ids = self.dense_index.top_k(dense_query_text, limit)
            except Exception:
                dense_ids = []
            if dense_ids:
                routes.append((dense_ids, DENSE_ROUTE_WEIGHT))

        # Route 5: expansion route -- keywords proposed by an optional language model.
        # Absent by default (`extra_terms` is None), in which case not one statement in
        # this block executes and `routes` is exactly what it was before this route
        # existed. Unfiltered for the same reason as routes 3 and 4, and wrapped in its
        # own try/except so a malformed expansion term costs only this route.
        if extra_terms:
            try:
                expansion_expression = or_expression(
                    [term for term in extra_terms if term not in set(query_terms)]
                )
                if expansion_expression:
                    expansion_ids = self.run_ranked_query(expansion_expression, None, limit)
                    if expansion_ids:
                        routes.append((expansion_ids, EXPANSION_ROUTE_WEIGHT))
            except sqlite3.Error:
                pass

        merged = self.fuse_rankings(routes, pool_k)

        # Safety net: if hard filters (buying track) narrowed things too far, backfill
        # from an unfiltered wide search rather than returning too few recommendations.
        if len(merged) < pool_k:
            seen = set(merged)
            for parent_asin in self.run_ranked_query(base_expression, None, limit):
                if parent_asin not in seen:
                    seen.add(parent_asin)
                    merged.append(parent_asin)
                if len(merged) >= pool_k:
                    break

        if reranker is not None:
            try:
                merged = reranker(merged)
            except Exception:
                # A reranker fault must cost us ordering, never the whole session: the
                # evaluator scores any raised exception as an outright miss.
                pass

        # Exclusions, applied after reranking so the reranker cannot undo them. Absent by
        # default (`avoid_terms` is None on every scored turn -- only a person can say
        # "not polyester"), in which case not one statement here executes.
        if avoid_terms:
            try:
                merged = self.demote_terms(merged, avoid_terms)
            except Exception:
                pass

        return [{"parent_asin": parent_asin} for parent_asin in merged[:top_k]]

    def demote_terms(self, ordered: list[str], avoid_terms: list[str]) -> list[str]:
        """Push candidates whose text contains a ruled-out term to the back of the list.

        Demotion, not deletion. "no polyester" is a preference, and catalog material text
        is noisy enough ("polyester lining") that dropping outright could hide the item
        they actually wanted -- and a short list is worse than a badly ordered one.
        Everything still comes back, just after the products that respect the exclusion.
        """
        needles = [f" {term.lower()} " for term in avoid_terms if term]
        if not needles:
            return ordered
        wanted, ruled_out = [], []
        for parent_asin in ordered:
            _, token_string = self.document_profile(parent_asin)
            bucket = ruled_out if any(n in token_string for n in needles) else wanted
            bucket.append(parent_asin)
        return wanted + ruled_out
