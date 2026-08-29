"""Reranking: deciding what order a retrieved pool goes out in.

Retrieval answers *which* products are plausible. This module answers *which one goes
first*, because the two are scored separately and had come apart badly: HitRate@10 only
asks whether the target is in the list at all (0.825 of the time it was), while MRR asks
where in the list it landed (0.420). Targets were being found and then buried.

The signal this leans on is a property of the problem, not of the simulator's wording:
a shopper describing what they want reuses the language of the thing they want, and the
catalog's own metadata is where that language comes from. So the product that echoes the
customer's *exact* phrasing -- especially phrasing that is rare across 50k products -- is
disproportionately likely to be the target. BM25 over a 60-term OR query cannot say that.
It rewards matching many terms, so a product matching a dozen common words outranks the
one product carrying the single rare phrase that actually identifies it.

Two signals, equally weighted:

- **coverage** -- how much of the evidence's IDF mass the product contains anywhere,
  discounted by which field it turned up in (a title match means more than a
  marketing-copy one).
- **phrase** -- how much of that mass survives as an intact token *sequence*. "closure
  type buckle" appearing verbatim is far stronger evidence than those three words
  scattered across a page of description.

A third signal -- blending in the fused retrieval order as a prior, so that reranking
acted as a correction rather than a replacement -- was built and then removed, because it
measurably *cost* points: weighting it at 0.35 scored 0.8105 and at 0.10 scored 0.8281,
against 0.8475 for dropping it. Both gaps are well outside the ~0.01 noise floor of a
200-session set. The reading is that BM25 over a 60-term OR query is simply a weaker
ordering signal than the two above, so mixing it in drags good candidates down. Fusion
still earns its keep by choosing *which* candidates are considered, and still breaks
ties, but it no longer votes on the order.

A fourth signal -- dense (LSA) similarity, see dense_retrieval.py -- is blended in as a
third additive term, DENSE_WEIGHT below. Treat this with the same suspicion as the
removed positional blend, not less: LSA is a smoothed compression of the same term
statistics coverage already scores, so it is correlated rather than independent, and
"not literally the same signal as before" is not evidence that blending it is safe. It
must be measured with a DENSE_WEIGHT=0.0 control arm before shipping a nonzero weight --
see docs/features/07-hybrid-dense-retrieval.md for the sweep.
"""

from __future__ import annotations

import math

from starter import retrieval
from starter.retrieval import CatalogIndex


# Blend weights. The score surface is flat near the top -- every split between 0.35/0.65
# and 0.65/0.35 lands inside the noise floor -- so an even split is the honest choice
# rather than the sweep's nominal winner.
COVERAGE_WEIGHT = 0.5
PHRASE_WEIGHT = 0.5

# Dense similarity's weight, additive on top rather than rebalanced out of the pair
# above. Measured, not guessed: even DENSE_WEIGHT=0.03 cost MRR versus the route-only
# control arm (0.850427 vs 0.857304), and 0.1 cost HitRate outright (0.975 -> 0.965) --
# the same monotonic-regression signature as the removed positional blend at the top of
# this file. LSA is correlated with _coverage (same term stats, smoothed), so this isn't
# a surprise in hindsight, but it had to be measured rather than assumed either way.
# Left at 0.0 deliberately: the dense signal still earns its keep as a retrieval route
# (see DENSE_ROUTE_WEIGHT in retrieval.py), just not as a reranking term.
DENSE_WEIGHT = 0.0

# A one-token "phrase" says nothing that coverage has not already counted.
MIN_PHRASE_TOKENS = 2

# What a phrase earns when it survives only in fragments. Full credit would make a
# scattered bigram worth as much as the whole intact phrase.
PARTIAL_PHRASE_CREDIT = 0.5


class Reranker:
    """Reorders a candidate pool against everything the customer has said."""

    def __init__(self, index: CatalogIndex) -> None:
        self.index = index

    def inverse_document_frequency(self, term: str) -> float:
        """How much it means that a product contains `term`.

        BM25's IDF, floored at zero: a term carried by most of the catalog says nothing,
        and must not be allowed to go negative and penalise a product for containing it.
        """
        total = self.index.document_count or 1
        frequency = self.index.document_frequency(term)
        return max(0.0, math.log(1.0 + (total - frequency + 0.5) / (frequency + 0.5)))

    def order(self, candidates: list[str], phrases: list[str]) -> list[str]:
        """Return `candidates` reordered best-first. Never drops or adds a candidate."""
        units = [unit for unit in (tuple(retrieval.tokens(text)) for text in phrases) if unit]
        vocabulary = {term for unit in units for term in unit}
        if not candidates or not vocabulary:
            return candidates

        idf = {term: self.inverse_document_frequency(term) for term in vocabulary}
        total_mass = sum(idf.values())
        if total_mass <= 0.0:
            return candidates

        phrase_units = [unit for unit in units if len(unit) >= MIN_PHRASE_TOKENS]
        # Constant across candidates, so it only rescales; it never reorders. That is why
        # noise units like "but I'm still exploring" need no special-casing -- they
        # dilute every candidate identically.
        phrase_mass = sum(sum(idf[term] for term in unit) for unit in phrase_units)

        # Own try/except, separate from retrieve()'s outer one: a dense-scoring bug must
        # cost only this term (falls back to an empty dict, i.e. 0.0 for every candidate),
        # never the already-working coverage/phrase scoring below it.
        dense_scores: dict[str, float] = {}
        dense_index = getattr(self.index, "dense_index", None)
        if dense_index is not None:
            try:
                dense_text = " ".join(phrases)
                dense_scores = dense_index.similarity_scores(dense_text, candidates)
            except Exception:
                dense_scores = {}

        scored: list[tuple[float, int, str]] = []
        for rank, parent_asin in enumerate(candidates):
            factors, document = self.index.document_profile(parent_asin)
            score = (
                COVERAGE_WEIGHT * self._coverage(idf, total_mass, factors)
                + PHRASE_WEIGHT * self._phrase_score(idf, phrase_mass, phrase_units, document)
                + DENSE_WEIGHT * dense_scores.get(parent_asin, 0.0)
            )
            # -rank keeps the fused order as the tie-break, so equal scores change nothing.
            # This is the only place retrieval's own ordering still has a say.
            scored.append((score, -rank, parent_asin))

        scored.sort(reverse=True)
        return [parent_asin for _, _, parent_asin in scored]

    @staticmethod
    def _coverage(idf: dict[str, float], total_mass: float, factors: dict[str, float]) -> float:
        found = 0.0
        for term, weight in idf.items():
            factor = factors.get(term)
            if factor:
                found += weight * factor
        return found / total_mass

    @staticmethod
    def _phrase_score(
        idf: dict[str, float],
        phrase_mass: float,
        phrase_units: list[tuple[str, ...]],
        document: str,
    ) -> float:
        if phrase_mass <= 0.0:
            return 0.0
        matched = 0.0
        for unit in phrase_units:
            mass = sum(idf[term] for term in unit)
            if f" {' '.join(unit)} " in document:
                matched += mass
                continue
            pairs = len(unit) - 1
            hits = sum(
                1 for index in range(pairs)
                if f" {unit[index]} {unit[index + 1]} " in document
            )
            if hits:
                matched += mass * PARTIAL_PHRASE_CREDIT * hits / pairs
        return matched / phrase_mass
