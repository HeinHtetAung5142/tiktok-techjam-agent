# 10 — Field-factor calibration

**Status:** merged
**Commit:** (this one)
**Owner:** Dialog + Ranking
**Tier:** 2 (MRR) — also moves HitRate

## What & why

`FIELD_FACTORS` (`starter/retrieval.py`) discounts a reranker term by which product field it matched
in: `title` 1.0, `categories` 0.9, `features`/`details` 0.85, `store` 0.7, `description` 0.65. Those
numbers were set by hand in feature 04 and **never swept** — feature 04's own limitations say so:
*"the pool size and field factors are fitted numbers."*

They were also wrong in a specific, checkable way. The customer's disclosures are generated
**verbatim from `features` and `details`** (`evaluator/local_evaluator.py:53`):

```python
candidates = [*_flatten_values(product.get("features")), *_flatten_values(product.get("details"))]
```

`intent_card` then takes `hard_constraints = cleaned[:2]` and `soft_preferences = cleaned[2:4]` off
the front of that list. So every word the customer will ever say originates in those two fields — and
the reranker was discounting matches there to 0.85 while rewarding `title` at 1.0. That is backwards:
a term matched in `features` is *more* likely to be quoting the disclosure than one matched in the
title.

Fix: raise both to parity with `title`.

## Approach

One constant changed. `features: 0.85 -> 1.0` and `details: 0.85 -> 1.0` in
`starter/retrieval.py`. No logic touched anywhere.

Found by the axis-F sweep in feature 09 and decomposed by axis H
(`py tools/sweep_constants.py --axis H`):

| Variant | Score | Delta | HitRate | MRR | MTTC |
|---|---|---|---|---|---|
| shipped before | 0.906791 | — | 0.9750 | 0.857304 | 2.895 |
| `features=1.0` only | 0.911124 | +0.0043 | 0.9800 | 0.860413 | 2.850 |
| `details=1.0` only | 0.908416 | +0.0016 | 0.9750 | 0.863054 | 2.900 |
| both = 0.9 | 0.907568 | +0.0008 | 0.9750 | 0.859893 | 2.895 |
| both = 0.95 | 0.912149 | +0.0054 | 0.9800 | 0.862831 | 2.835 |
| **both = 1.0 (shipped)** | **0.912205** | **+0.0054** | **0.9800** | **0.864018** | 2.850 |
| both = 1.0, `categories` = 1.0 | 0.899816 | **−0.0070** | 0.9700 | 0.846387 | 2.955 |
| both = 1.15 (past `title`) | 0.904549 | **−0.0022** | 0.9700 | 0.856163 | 2.865 |

Three things this establishes, and the reason the value is 1.0 rather than a fitted decimal:

- **`features` carries most of it** (+0.0043 of +0.0054), which is what the mechanism predicts:
  `intent_card` reads `features` first, so it supplies most of `cleaned[:2]`.
- **1.0 is a ceiling, not a trend.** Pushing to 1.15 *loses* 0.0022 and costs HitRate outright. The
  optimum sits exactly at parity with `title` — principle and measurement agree, rather than having
  to be traded off.
- **It is specific to these two fields.** Raising `categories` alongside them costs 0.0070, and
  feature 09's `flat 1.0 everywhere` arm costs 0.0110. Field discrimination still matters; it was
  miscalibrated on exactly the two fields that carry the evidence.

0.95 and 1.0 are indistinguishable (0.00006 apart). 1.0 was chosen because it is the principled
point, not because it is the argmax by a hair.

## Measured impact

_results/results_after_dense.json → results/results_after_fieldfactors.json_

| Metric | Before | After | Delta |
|---|---|---|---|
| HitRate@10 | 0.975 | 0.98 | +0.005 |
| MRR | 0.857304 | 0.864018 | +0.006714 |
| MTTC | 2.895 | 2.85 | -0.045 |
| Efficiency | 0.8105 | 0.815 | +0.0045 |
| **TechnicalScore** | **0.906791** | **0.912205** | **+0.005414** |

### By scenario

| Scenario | n | Metric | Before | After | Delta |
|---|---|---|---|---|---|
| boundary | 10 | HitRate@10 | 1 | 1 | 0 |
|  |  | MRR | 0.95 | 1 | +0.05 |
|  |  | MTTC | 3.2 | 3.2 | 0 |
| browsing | 80 | HitRate@10 | 0.9875 | 0.9875 | 0 |
|  |  | MRR | 0.853765 | 0.853001 | -0.000764 |
|  |  | MTTC | 2.7375 | 2.725 | -0.0125 |
| buying | 80 | HitRate@10 | 0.9625 | 0.975 | +0.0125 |
|  |  | MRR | 0.851771 | 0.852133 | +0.000362 |
|  |  | MTTC | 2.6375 | 2.525 | -0.1125 |
| intent_override | 30 | HitRate@10 | 0.966667 | 0.966667 | 0 |
|  |  | MRR | 0.850595 | 0.879762 | +0.029167 |
|  |  | MTTC | 3.9 | 3.93333 | +0.033333 |

**`boundary` MRR is now a perfect 1.0** — all ten boundary sessions land at rank 1.

### Read this before quoting the number

**+0.005414 is below this repo's ~0.01 noise floor, and `score_delta.py` correctly flags it as
flat.** The per-session decomposition is thin and is stated here rather than buried:

- **One rescued miss.** `public_0145` — previously one of the five "unreachable" misses — now lands
  at **rank 10 on turn 5**, scraping into the top ten. That single session supplies the entire
  HitRate gain (+0.0025 of score) and most of the MTTC gain, since a miss is charged at turn 11.
- **The MRR gain comes from four sessions.** +0.875 RR across 3 `intent_override` sessions and
  +0.500 RR from 1 `boundary` session, against −0.13 RR spread thinly over 19 `browsing`/`buying`
  sessions that got marginally worse. Rank churn is nearly symmetric: **12 sessions improved, 11
  worsened**, and rank-1 count moved only 161 → 162.

So this is **not** a broad ranking improvement. It is one rescue plus four sessions moving up, with
compensating drift elsewhere. **The justification for shipping it is the mechanism, not the
magnitude** — the reranker was demonstrably under-weighting the two fields the simulator draws every
disclosure from, and that property of the generator is identical in the private set. The public-set
delta is best read as weak confirmation that the correction is not harmful, not as a measured +0.005.

Nothing regressed at the aggregate level: every headline metric improved and no session that
previously hit now misses.

## Limitations & follow-ups

- **The gain rests on five sessions out of 200.** It could be sample-specific. The mechanism
  argument is what should carry the decision; if the private set disagrees, the mechanism is still
  the better prior and the fitted 0.85 was never defensible.
- **`public_0145` at rank 10 is fragile.** It is one position from being a miss again. Do not treat
  HitRate 0.98 as robust — it is 0.975 plus a session hanging on the boundary of the cut. Known gap
  2's list of unreachable misses is now four, not five, and `public_0145` should be read as
  "marginal", not "solved".
- **`browsing` MRR drifted −0.0008.** Inside noise, but it is the one metric that moved the wrong
  way; worth watching if field factors are touched again.
- **This change moved other axes, which is the argument for re-sweeping after any pipeline edit.**
  Re-running axis G against the new configuration gives different answers than it did before:
  `PARTIAL_PHRASE_CREDIT` at 0.75 and 1.0 were +0.0001 under the old field factors and are now
  −0.0029 and −0.0031. The shipped 0.5 went from "indistinguishable" to "clearly correct". Nothing
  needs changing, but it demonstrates the axes are not independent — treat feature 09's A–H tables
  as measured against the *old* constants, and re-run before trusting any of them again.
- **Still unswept:** `store` and `description` were only moved as a pair in feature 09's tail arms.
  A finer sweep of the low end is cheap (`tools/sweep_constants.py`) and has not been done.
- **The bigger idea remains untested:** `_coverage` measures recall with no length normalization
  (feature 09, "Still untested"). Field factors adjust *which field* a match counts from; they do
  nothing about a verbose listing matching more terms by chance.
