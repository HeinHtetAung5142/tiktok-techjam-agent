"""Dense (LSA) embeddings over the catalog corpus.

Pure vectorization -- no file I/O, no network. Fits TF-IDF + Truncated SVD once from
whatever text `CatalogIndex` hands it, entirely offline and deterministic given a fixed
`random_state`. This is Latent Semantic Analysis: a low-rank compression of the same
term-frequency statistics `ranking.py`'s coverage score already uses, so it is not an
independent signal so much as a *smoothed* one -- its real value is the co-occurrence and
synonymy structure SVD captures (e.g. "trainers" sits near "sneakers"), which exact term
overlap cannot see at all. Treat it accordingly: useful for recall and for nudging a
lexically-dissimilar-but-relevant candidate up, not a replacement for coverage/phrase.

Every public method degrades to an empty/None result on a degenerate query rather than
raising, so a caller can always treat "no dense signal" as a valid, harmless answer.
"""

from __future__ import annotations

import numpy as np
from sklearn.decomposition import TruncatedSVD
from sklearn.feature_extraction.text import TfidfVectorizer

DENSE_COMPONENTS = 75
MAX_FEATURES = 15_000
SVD_RANDOM_STATE = 0

# Below this row/query norm, treat the vector as empty rather than dividing by it. A
# zero-norm *document* row that slips through becomes NaN and corrupts every future dot
# product against it for the process lifetime, since argsort does not reliably push NaN
# to the tail -- so this guard applies at both construction and query time.
_NORM_EPS = 1e-9


class DenseIndex:
    """Cosine-similarity search over LSA vectors for the whole catalog."""

    def __init__(self, texts: list[str], parent_asins: list[str]) -> None:
        self.parent_asins = list(parent_asins)
        self._row_by_asin = {asin: row for row, asin in enumerate(self.parent_asins)}

        self._vectorizer = TfidfVectorizer(max_features=MAX_FEATURES, dtype=np.float32)
        tfidf = self._vectorizer.fit_transform(texts)

        self._svd = TruncatedSVD(
            n_components=DENSE_COMPONENTS,
            algorithm="randomized",
            random_state=SVD_RANDOM_STATE,
        )
        matrix = self._svd.fit_transform(tfidf).astype(np.float32)
        self._matrix = self._l2_normalize(matrix)

    @staticmethod
    def _l2_normalize(matrix: np.ndarray) -> np.ndarray:
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        safe_norms = np.where(norms > _NORM_EPS, norms, 1.0)
        normalized = matrix / safe_norms
        # Rows that were ~zero to begin with must stay zero, not become an arbitrary unit
        # vector from dividing by the substituted 1.0 above.
        normalized[norms.ravel() <= _NORM_EPS] = 0.0
        return normalized

    def similarity_vector(self, text: str) -> np.ndarray | None:
        """Cosine similarity of `text` against every catalog row, in construction order.

        None if `text` is empty or has no vocabulary overlap with the fitted corpus --
        both are "no basis for a dense query", not zero-everywhere noise.
        """
        if not text or not text.strip():
            return None
        query = self._svd.transform(self._vectorizer.transform([text]))[0]
        norm = float(np.linalg.norm(query))
        if norm <= _NORM_EPS:
            return None
        return self._matrix @ (query / norm).astype(np.float32)

    def top_k(self, text: str, k: int) -> list[str]:
        """The `k` catalog products most similar to `text`, best-first."""
        if k <= 0:
            return []
        scores = self.similarity_vector(text)
        if scores is None:
            return []
        k = min(k, len(scores))
        top = np.argpartition(-scores, k - 1)[:k]
        top = top[np.argsort(-scores[top])]
        return [self.parent_asins[index] for index in top]

    def similarity_scores(self, text: str, candidates: list[str]) -> dict[str, float]:
        """Cosine similarity of `text` against just `candidates`, clipped to [0, inf).

        Clipped so the scale matches ranking.py's other terms, which are both in [0, 1]:
        a negative cosine is "unrelated", not "evidence against", for this purpose.
        """
        scores = self.similarity_vector(text)
        if scores is None:
            return {}
        result: dict[str, float] = {}
        for asin in candidates:
            row = self._row_by_asin.get(asin)
            if row is not None:
                result[asin] = max(0.0, float(scores[row]))
        return result
