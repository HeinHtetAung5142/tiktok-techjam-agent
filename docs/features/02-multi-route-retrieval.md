# 02 — Multi-route retrieval pipeline

**Status:** merged
**Commit:** `9aff528`
**Owner:** Retrieval & Routing
**Tier:** 1

> Backfilled from git history after the fact. Measured against the restored
> `results_after_routing.json` snapshot, so this table isolates *this* feature rather than showing
> the cumulative delta from the starter baseline.

## What & why

After feature 01 there was still only **one** way into the catalog: a single BM25 query over every
indexed column at once. That query's score is dominated by long fields — a product with a
keyword-stuffed description outranks one whose `categories` is an exact match, because BM25 sees
more matching tokens.

The customer's opening message is built from the target's coarse category
(`coarse_category()` in the evaluator, e.g. *"Novelty Ugly Christmas Sweaters"*), so category is one
of the strongest signals available and it was being diluted. HitRate@10 is 50% of the score, so
recall of the candidate pool is the thing to attack.

## Approach

Two independent retrieval routes per turn, fused.

**Route 1 — keyword.** The original whole-catalog BM25 query, unchanged.

**Route 2 — category.** The same terms, restricted to the `categories` column
(`categories:("term" OR "term" …)`). A strong category match now competes on its own terms instead
of being averaged against noisy title/description scores.

**Fusion.** `_fuse_rankings` implements weighted **Reciprocal Rank Fusion**: each route contributes
`weight / (60 + rank)` to an item's score, summed across routes. Keyword is weighted `1.0` and
category `0.3` (`CATEGORY_ROUTE_WEIGHT`). RRF is used rather than score averaging because the two
routes' BM25 scores are not on a comparable scale — only their *rankings* are. The 0.3 weight lets
the category route rescue an item the keyword route buried, without letting it casually outrank
keyword's own top picks.

Both routes over-fetch `top_k * 5` (`OVERFETCH_MULT`) so fusion has depth to work with.

**Backfill safety net.** On the buying track, hard `AND` terms plus a price ceiling can narrow the
pool below 10 items. Rather than return a short list — wasted slots are wasted chances — an
unfiltered wide search tops it back up to `top_k`.

`_with_and_terms` was extracted in this commit to compose the required-term expression once and
share it across both routes.

## Measured impact

_results_after_routing.json → results_after_multiroute.json_

| Metric | Before | After | Delta |
|---|---|---|---|
| HitRate@10 | 0.13 | 0.15 | +0.02 ✅ |
| MRR | 0.070095 | 0.068446 | -0.001649 🔻 |
| MTTC | 9.76 | 9.56 | -0.2 ✅ |
| Efficiency | 0.124 | 0.144 | +0.02 ✅ |
| **TechnicalScore** | **0.110829** | **0.124334** | **+0.013505 ✅** |

### By scenario

| Scenario | n | Metric | Before | After | Delta |
|---|---|---|---|---|---|
| boundary | 10 | HitRate@10 | 0 | 0 | 0 |
|  |  | MRR | 0 | 0 | 0 |
|  |  | MTTC | 11 | 11 | 0 |
| browsing | 80 | HitRate@10 | 0.025 | 0.0375 | +0.0125 ✅ |
|  |  | MRR | 0.004514 | 0.005035 | +0.000521 ✅ |
|  |  | MTTC | 10.75 | 10.625 | -0.125 ✅ |
| buying | 80 | HitRate@10 | 0.25 | 0.2875 | +0.0375 ✅ |
|  |  | MRR | 0.126974 | 0.125456 | -0.001518 🔻 |
|  |  | MTTC | 8.5 | 8.125 | -0.375 ✅ |
| intent_override | 30 | HitRate@10 | 0.133333 | 0.133333 | 0 |
|  |  | MRR | 0.116667 | 0.108333 | -0.008334 🔻 |
|  |  | MTTC | 10.0667 | 10.0667 | 0 |

**Verdict: a real win, and above the noise floor** — `+0.013505`, driven by 4 extra sessions found.
Buying gained the most (+0.0375, three sessions), browsing gained one.

## Limitations & follow-ups

- **It trades MRR for HitRate, and we should be honest that this is a real cost.** MRR *fell*
  (-0.001649 overall, -0.008334 on intent_override) while HitRate rose. RRF is surfacing targets
  that the keyword route missed entirely, but landing them low in the Top 10, while simultaneously
  nudging previously-well-ranked targets down. The scoring weights make this a net positive
  (HitRate is 0.50, MRR is 0.30), so the trade is worth taking — but it is the strongest argument
  yet for **semantic reranking** (Tier 2): the targets are now *in* the list, just in the wrong
  order. Reranking is where the lost MRR gets recovered and then some.
- **`CATEGORY_ROUTE_WEIGHT = 0.3` and `rrf_k = 60` are untuned.** 60 is the value from the original
  RRF paper; 0.3 was a judgement call. Neither has been swept.
- **Both routes are lexical.** A customer saying *"something warm for hiking"* still matches nothing
  unless those exact tokens appear in the product text. This is the case for dense/hybrid retrieval
  (Tier 2).
- **boundary and intent_override are completely untouched** (0.0 and 0.133, unchanged). Neither is a
  retrieval problem: boundary needs a sensible fallback when the customer declines to answer, and
  intent_override needs slot rewriting. Both are Dialog + Ranking work.
- The backfill path can dilute a well-filtered pool with unfiltered results. It has not been
  measured in isolation — worth an ablation if buying-track precision becomes the bottleneck.
