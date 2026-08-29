# 07 — Hybrid/dense retrieval

**Status:** merged (route only — reranker blend measured and disabled)
**Owner:** Retrieval & Routing
**Tier:** 2

## What & why

The last unchecked Tier-2 item in `CLAUDE.md`'s priorities table. Feature 05 named it directly as
the next lever: *"The plateau is a retrieval ceiling, not a dialog one... the remaining 33
non-rank-1 sessions and the 7 misses need dense retrieval, not more patience."*

Feature 06 already tried something *called* "dense retrieval" and backed it out — but that was a
lexical AND-conjunction of two FTS5 phrase queries, not an embedding model, and it targeted the 5
information-theoretically-unreachable misses specifically (evidence shared identically across
thousands of products, so no method separates them). This feature is a genuinely different
technique — real dense vectors via LSA (TF-IDF → Truncated SVD) — scoped at the ~33 sessions that
already hit but don't rank #1, plus paraphrase-robustness for the private/held-out set (4x larger,
may include natural-language paraphrasing per the spec) that the public 200-session set cannot
measure at all.

**The honest headline result: it doesn't move the public-set score, and the reason why is now
measured rather than assumed.** It ships anyway, at a configuration verified not to cost anything,
because it's still real infrastructure this project didn't have before — a second, semantically
distinct signal alongside the sparse routes, load-bearing for the paraphrase case even though this
200-session set can't show it.

## Approach

**New module `starter/dense_retrieval.py`** — `DenseIndex`, pure vectorization with no file I/O.
Fits `TfidfVectorizer(max_features=15_000)` + `TruncatedSVD(n_components=75, algorithm="randomized",
random_state=0)` once from the catalog's own text at construction time — fully offline, no
pretrained model, deterministic given the fixed seed. L2-normalizes every doc row so cosine
similarity is a dot product; zero-norm rows (empty product text) are guarded to become an explicit
all-zero vector rather than `NaN` — that guard has to exist at *construction* time, not just query
time, since an unguarded zero-norm row corrupts every future similarity check against it for the
process lifetime (`np.argsort` does not reliably push `NaN` to the tail). Exposes `top_k()` for the
retrieval route and `similarity_scores()` for the reranker, both returning empty on a degenerate
(empty or fully out-of-vocabulary) query rather than raising.

**`starter/retrieval.py`** — `_build_index()` now also accumulates each product's flattened text
and asin order while it streams the catalog once (no second file read), then
`_build_dense_index()` builds the `DenseIndex`, wrapped in try/except so any failure (missing dep,
`MemoryError`, anything) leaves `self.dense_index = None` and the agent falls back to sparse-only
retrieval rather than crashing. `retrieve()` gets a 4th route — `DENSE_ROUTE_WEIGHT`-weighted,
built from `" ".join(phrases)` (falling back to `query_terms` when no phrases exist yet),
deliberately unfiltered by `and_terms`/`price_max` like the phrase routes, with its own try/except
so a per-turn transform failure costs only that route, not the whole `retrieve()` call.

**`starter/ranking.py`** — `Reranker.order` optionally blends a third additive scoring term,
`DENSE_WEIGHT * dense_scores.get(candidate, 0.0)`, computed via its own try/except separate from
`retrieve()`'s outer one (so a dense-scoring bug degrades only this term, never the already-working
coverage/phrase scoring). **This term is shipped at `DENSE_WEIGHT = 0.0`** — see Measured impact.

**Dependencies.** `numpy==2.5.2`, `scipy==1.18.1`, `scikit-learn==1.9.0` pinned in
`requirements.txt` (previously empty — this is the project's first non-stdlib dependency).
Confirmed installable via prebuilt `cp314-win_amd64` wheels on this machine's actual Python
(3.14.7, not the 3.12.0 `CLAUDE.md` had documented — fixed there too). No compilation, no network
at runtime.

## Ablation — the reranker blend was measured, not guessed

The module docstring in `ranking.py` already records a monotonic-regression pattern from a
previously-removed signal (blending the fused BM25/RRF order back in cost 0.03–0.04 TechnicalScore
at *every* tested nonzero weight). Dense similarity is a different signal in principle, but LSA is
a smoothed compression of the same term-frequency statistics `_coverage` already scores — not the
independent, uncorrelated source of information the "it's semantic, not positional" framing might
suggest — so it was treated with equal suspicion and swept with a `DENSE_WEIGHT=0.0` control arm
before shipping anything nonzero.

| Configuration | HitRate@10 | MRR | MTTC | TechnicalScore |
|---|---|---|---|---|
| baseline (no dense) | 0.975 | 0.857935 | 2.880 | **0.907281** |
| route only, weight 0.3 (shipped) | 0.975 | 0.857304 | 2.895 | 0.906791 |
| route weight 0.5, no blend | 0.970 | 0.856579 | 2.930 | 0.903374 |
| route 0.3 + reranker blend 0.03 | 0.975 | 0.850427 | 2.875 | 0.905128 |
| route 0.3 + reranker blend 0.1 | 0.965 | 0.834117 | 2.950 | 0.893735 |

Two findings, both matching prior project experience rather than contradicting it:

- **The retrieval route alone is flat, not harmful.** HitRate is unchanged at 0.975 across every
  tested route weight up to 0.3; MRR moves by -0.0006, inside the noise floor. The route adds
  candidates without displacing anything the sparse routes already had right, at this weight.
- **The reranker blend regresses monotonically, the instant it's nonzero.** `0.03` already costs
  0.007 of MRR versus the route-only control; `0.1` costs a session outright (HitRate 0.975 →
  0.965) and 0.024 of MRR. This is the same signature `ranking.py`'s docstring already documented
  for the removed positional blend — confirms LSA is correlated enough with `_coverage` that adding
  it as a reranking term mostly just perturbs an already-settled lexical ranking rather than
  contributing new information there.
- **Route weight 0.5 regressed one session** (HitRate 0.97) via the same RRF-dilution mechanism the
  rejected conjunction route hit in feature 06 — a heavier route can now outrank a sparse route's
  correct pick, not just add candidates to the pool.

**Shipped configuration: `DENSE_ROUTE_WEIGHT = 0.3`, `DENSE_WEIGHT = 0.0`** — the only tested point
that doesn't cost anything measurable.

## Measured impact

_results_after_phrase.json → results_after_dense.json_

| Metric | Before | After | Delta |
|---|---|---|---|
| HitRate@10 | 0.975 | 0.975 | 0 |
| MRR | 0.857935 | 0.857304 | -0.000631 🔻 |
| MTTC | 2.88 | 2.895 | +0.015 🔻 |
| Efficiency | 0.812 | 0.8105 | -0.0015 🔻 |
| **TechnicalScore** | **0.907281** | **0.906791** | **-0.00049 🔻** |

Within the ~0.01 noise floor of a 200-session set — treat as flat, not a regression.

### By scenario

| Scenario | n | Metric | Before | After | Delta |
|---|---|---|---|---|---|
| boundary | 10 | HitRate@10 | 1 | 1 | 0 |
|  |  | MRR | 0.95 | 0.95 | 0 |
| browsing | 80 | HitRate@10 | 0.9875 | 0.9875 | 0 |
|  |  | MRR | 0.855432 | 0.853765 | -0.001667 🔻 |
| buying | 80 | HitRate@10 | 0.9625 | 0.9625 | 0 |
|  |  | MRR | 0.851682 | 0.851771 | +0.000089 ✅ |
| intent_override | 30 | HitRate@10 | 0.966667 | 0.966667 | 0 |
|  |  | MRR | 0.850595 | 0.850595 | 0 |

No scenario moved outside noise in either direction — no HitRate/MRR inversion to flag here.

## Construction-time cost

Real, measured, and worth disclosing plainly: fitting `DenseIndex` over the full 50k-product
corpus adds **~30 seconds** to `Agent()` construction (FTS5-only: 6.35s; with dense: ~35.8s total),
almost entirely `TfidfVectorizer.fit_transform` + `TruncatedSVD.fit_transform` plus first-import
overhead for numpy/scipy/scikit-learn. This happens once per `Agent()` construction, not per
session or per turn — per-turn cost is negligible (a `(50000, 75)` dense matmul, sub-millisecond).
There is no enforced timeout in `evaluator/local_evaluator.py`, so this does not risk a scored
failure, but it roughly doubles-to-triples the previous ~20s full local run. `max_features` (15,000,
down from an initial 30,000) and `n_components` (75, down from 100) are the tuning knobs if this
needs to shrink further; going lower trades embedding quality for speed and wasn't pursued here
since there was no scoring pressure to do so.

## Limitations & follow-ups

- **Does not, and was never expected to, rescue the 5 unreachable misses.** Their disclosed
  evidence has document frequency in the thousands, identical across thousands of products — no
  discriminating signal exists for *any* method to exploit, dense included. Unaffected here, as
  predicted.
- **The reranker blend is disabled (`DENSE_WEIGHT = 0.0`), not deleted.** The code path exists,
  is tested, and is one constant away from re-enabling — but every measured nonzero weight
  regressed MRR, so it ships off. Don't re-enable it on a hunch; if revisited, it needs a genuinely
  different feature (e.g. a real pretrained sentence embedding bringing outside world-knowledge)
  to avoid reproducing this same correlated-with-coverage failure mode.
- **First non-stdlib dependency in this project.** `requirements.txt` was empty; now pins
  `numpy`/`scipy`/`scikit-learn`. Verified installable on this machine's Python 3.14.7 via prebuilt
  wheels — worth a final re-check closer to submission in case the organizer's environment resolves
  to a different Python version with different wheel availability.
- **Construction time roughly triples the local full-run** (~20s → ~35-45s+, plus 200-session
  replay). Not a scored risk (no enforced timeout) but worth knowing before a live demo.
- **Reproducibility is slightly weaker than the project's existing "runs are deterministic" claim.**
  `random_state=0` fixes the SVD's internal randomness, but floating-point summation order can
  still differ across different numpy/scipy/BLAS builds — same-machine, same-run determinism is
  guaranteed; bit-identical reproduction on a different machine is not, unlike the rest of this
  codebase's pure-Python determinism.
- **Deliberately unfiltered by `and_terms`/`price_max`**, matching the phrase-route precedent. On a
  split with real price constraints this could surface an over-budget item into the pool — it still
  has to win fusion and reranking to be shown, and this split has zero real `$` prices to test
  against (per feature 06), so it's untested here.
- **Value is unmeasurable on this split by construction.** The paraphrase-robustness case this
  feature is meant to hedge against only exists on the private set. It cannot be validated locally;
  it can only be built correctly and left in place.
