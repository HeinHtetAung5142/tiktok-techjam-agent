# 17 — Coverage precision (rejected) + robustness hardening (shipped)

**Status:** one measured negative result, one score-neutral hardening — **score of record unchanged at 0.912205**
**Commit:** (this one)
**Owner:** Coordination + Evaluation
**Tier:** 2 (MRR) for the rejected half, 3 (feasibility) for the shipped half

Two things happened here, and they should be read separately.

1. **The coverage precision term — the last open lever in the project — was implemented, swept in
   both directions under two definitions of document length, and rejected.** It is not flat; it is
   sharply and monotonically negative from its smallest nonzero value. Gap 1 is now closed by
   measurement.
2. **The two pieces of unbanked private-set insurance were taken**, both proven byte-identical.
   Known gap 4 is closed, and `verify_features.py` has no XFAILs left.

---

## Part 1 — The coverage precision term (REJECTED)

### What & why

`Reranker._coverage` (`starter/ranking.py`) sums `idf * field_factor` over the evidence terms a
product contains and divides by `total_mass`, which is **constant across candidates**. So it scores
pure recall: *how much of the customer's evidence is in this product*, never the converse *how much
of this product is the customer's evidence*. A sprawling listing carrying "100% Cotton" among forty
other bullet points scored identically to a focused listing where that was the whole product.

That is exactly the failure shape of the 34 sessions at ranks 2–8, which feature 09 measured as
worth **+0.0348** — more than twice the entire miss pool — and named as *"the one idea with a real
mechanism behind it that has not been tried"*. It was the last item on the Tier 2 list.

The catalog said there was room. Document lengths over all 50,000 products, summed across
`TEXT_COLUMNS` and counted with `tokens()`:

| | mean | median | p10 | p90 | max |
|---|---|---|---|---|---|
| tokens per product | **124.1** | 99 | **39** | **244** | 1418 |

A >6x spread between p10 and p90, invisible to the scoring function. The hypothesis was that
normalizing it away would promote focused listings over verbose ones.

### Approach

BM25's own length normalization, added to `_coverage` behind a single tunable:

```python
norm = 1.0 - COVERAGE_LENGTH_B + COVERAGE_LENGTH_B * (length / AVG_DOC_TOKENS)
found /= norm
```

`COVERAGE_LENGTH_B = 0.0` was the shipped control and introduced no float operation at all, so the
control arm stayed byte-identical and `sweep_constants.py`'s "control must reproduce 0.912205
exactly" guard still applied. Length was read off the space-padded token string
`document_profile` already returns, so no second per-product cache had to be invalidated in
lockstep with `_profile_cache`.

### Results — sweep axis I, penalise length

| Variant | Score | Delta | HitRate | MRR | MTTC |
|---|---|---|---|---|---|
| **control (shipped, b=0)** | **0.912205** | — | **0.9800** | **0.864018** | **2.850** |
| b=0.15 | 0.876468 | **−0.0357** | 0.9400 | 0.831226 | 3.145 |
| b=0.30 | 0.863670 | **−0.0485** | 0.9300 | 0.816567 | 3.315 |
| b=0.45 | 0.838939 | **−0.0733** | 0.9100 | 0.781131 | 3.520 |
| b=0.60 | 0.799620 | **−0.1126** | 0.8750 | 0.728399 | 3.820 |
| b=0.75 | 0.741927 | **−0.1703** | 0.8250 | 0.653089 | 4.325 |
| b=1.00 | 0.587338 | **−0.3249** | 0.6750 | 0.474460 | 5.625 |

Monotonically negative, on **all three metrics at once**, from the smallest value tested — nothing
here is inside the noise floor. A one-sided negative would have been a weak result ("we picked the
wrong parameterization"), so the other side was measured too.

### Results — sweep axis J, the other side and the other length definition

| Variant | Score | Delta | HitRate | MRR | MTTC |
|---|---|---|---|---|---|
| b=−0.10 (reward length) | 0.892045 | **−0.0202** | 0.9650 | 0.832151 | 3.005 |
| b=−0.20 | 0.826477 | **−0.0857** | 0.9300 | 0.698256 | 3.400 |
| b=−0.35 | 0.732693 | **−0.1795** | 0.9050 | 0.462976 | 3.935 |
| b=−0.50 | 0.667704 | **−0.2445** | 0.8700 | 0.339681 | 4.460 |
| distinct-vocabulary length, b=0.15 | 0.886116 | **−0.0261** | 0.9550 | 0.831387 | 3.040 |
| distinct-vocabulary length, b=0.30 | 0.874624 | **−0.0376** | 0.9450 | 0.818748 | 3.175 |
| distinct-vocabulary length, b=−0.15 | 0.901654 | **−0.0106** | 0.9750 | 0.841181 | 2.910 |

**`b = 0` is a strict local maximum in both directions, under both definitions of length.** The
shipped absence of length normalization is not an oversight — it is the optimum.

### Why the mechanism was wrong

The reasoning in feature 09 was sound about the *symptom* and wrong about the *cause*. Two things
the length hypothesis did not account for:

- **Length is positively correlated with being a target.** The simulator builds every disclosure
  from the target's own `features`/`details` (`evaluator/local_evaluator.py:52-71`). A product with
  rich `features`/`details` is both the kind of product that generates a disclosable intent card
  *and* a long document. Penalising length penalises the target.
- **Precision is already priced in, twice.** `_phrase_score` rewards intact contiguous phrasing,
  which a boilerplate-padded listing cannot fake, and `FIELD_FACTORS` already discounts the fields
  that do the padding (`description` 0.65, `store` 0.7). Adding length normalization on top
  double-charges for verbosity that has already been charged for.

The negative side (`b < 0`, rewarding verbosity) failing too is what makes this conclusive rather
than a sign error: the coverage term is already sitting where those two forces balance.

### What this closes

**Known gap 1 is now closed by measurement, not left open with "expect it to move a handful".**
Combined with feature 09's structural proof that the customer's evidence is complete by turn 3, and
feature 06's proof that the 4 misses are information-theoretically unreachable, there is no
remaining untried mechanism in the ranking stack. Anyone revisiting this needs a *new* idea, not
another parameterization of this one — log damping is a strictly gentler member of a family that is
already negative at its gentlest tested point, and is not worth running.

All scaffolding was reverted; `starter/ranking.py`, `starter/retrieval.py` and
`tools/sweep_constants.py` are byte-identical to their pre-experiment state, and the control arm was
re-confirmed at 0.912205 afterwards.

---

## Part 2 — Robustness hardening (SHIPPED, byte-identical)

Both items were recorded in Known gaps as free insurance that nobody had banked. Neither moves the
public score; both remove a private-set failure mode. The private set is 4x larger and the spec
permits added paraphrasing, so "never happens here" is not "never happens".

### 2a. The query-term cap now keeps both ends

`agent.py` sliced `terms(state.evidence_text())[:MAX_QUERY_TERMS]`. `evidence_text()` is oldest-first
and `terms()` preserves first-seen order, so a *binding* cap would discard the newest disclosures —
the specific ones just elicited on turns 3–4, which is exactly where every non-rank-1 hit lives —
while keeping generic turn-1 chatter.

Feature 09 proposed taking the tail instead. That inverts the bug rather than fixing it: it would
drop the turn-1 product category, which is the entire reason evidence accumulates across turns. So
`_capped_terms` reserves a head budget and drops the *middle*:

```python
QUERY_TERM_HEAD = 16
return all_terms[:QUERY_TERM_HEAD] + all_terms[QUERY_TERM_HEAD - MAX_QUERY_TERMS:]
```

Measured dead on the public set — 0 of 574 `respond()` calls exceed the cap — so it is a no-op here
by construction, which `verify_features.py` now asserts from both sides (binding and non-binding).

### 2b. `respond()` no longer lets anything escape

A raised exception is scored as a **miss** (`evaluator/local_evaluator.py:239-244`). The `try` in
`respond()` was `try/finally` for latency timing only and re-raised. Three confirmed escape paths:
`observe(None, 1)` → `TypeError`, a non-`int` `turn`, and `respond()` before `reset()` →
`RuntimeError` by design.

Now: inputs are coerced (`user_message` to `str`, falsy to `""` so `None` cannot become the query
term "none"; `turn` to `int`, defaulting to 1), an unknown `session_id` auto-creates its session
instead of raising, and a broad `except Exception` returns `_fallback_response()` — a contract-valid
payload with `ask_attribute="other"` and an empty recommendation list.

`"other"` rather than `None` is deliberate: `null` wastes the turn outright
(`evaluator/local_evaluator.py:171`), while `other` is the one attribute that cannot whiff and may
still extract two constraints for the next turn. An empty list scores nothing that turn but keeps
the session alive, where an exception ends it as a miss.

This is the **outermost** layer only. The existing inner fault isolation (reranker, dense route,
phrase routes) still catches what it can and keeps the turn *working*; this catches only what
escapes that, and degrades to a valid shape instead of a miss.

### Results

Byte-identical, which is the acceptance test for this half — not score equality.

| Metric | Before | After | Delta |
|---|---|---|---|
| HitRate@10 | 0.98 | 0.98 | **0** |
| MRR | 0.864018 | 0.864018 | **0** |
| MTTC | 2.85 | 2.85 | **0** |
| Efficiency | 0.815 | 0.815 | **0** |
| **TechnicalScore** | **0.912205** | **0.912205** | **0** |

| Scenario | n | HitRate@10 | MRR | MTTC |
|---|---|---|---|---|
| boundary | 10 | 1.0 → 1.0 | 1.0 → 1.0 | 3.2 → 3.2 |
| browsing | 80 | 0.9875 → 0.9875 | 0.853001 → 0.853001 | 2.725 → 2.725 |
| buying | 80 | 0.975 → 0.975 | 0.852133 → 0.852133 | 2.525 → 2.525 |
| intent_override | 30 | 0.966667 → 0.966667 | 0.879762 → 0.879762 | 3.933 → 3.933 |

`py tools/score_ratchet.py` reports **`PASS: byte-identical`**, and the SHA-256 of the sessions
array is unchanged (`0974e5f1c5c9…`). Snapshot: `results/results_after_robustness.json`.

### Verification changes

`tools/verify_features.py` grew from 90 checks to **95, with no XFAILs** — the two robustness checks
that were reported as expected failures under known gap 4 are now real passes, and the
"raises `RuntimeError` by design" check was **inverted rather than deleted**, so the contract
softening is recorded as a deliberate decision.

New checks assert the full `turn_response` shape from `docs/agent_api_contract.json` (including that
recommendations are objects carrying a `parent_asin`, not bare strings), across six malformed
inputs, plus one that the guard does **not** swallow a working turn into the fallback, plus both
sides of the query-term cap.

## Limitations & follow-ups

- **The negative result is fitted to 200 public sessions**, like every constant here. The mechanism
  behind it — disclosures are generated from the target's own `features`/`details`, so length
  correlates with targethood — is structural and generated by the same function on the private set,
  so it should transfer. The exact shape of the curve will not.
- **The fallback response has never fired on a scored run**, by construction. It is insurance whose
  value is unmeasurable from here; the only claim made for it is that it converts a would-be miss
  into a legal shape, which is verified directly rather than inferred.
- **`_capped_terms` is likewise never exercised while scoring.** Its head budget of 16 is a
  judgment call, not a fitted number — there is no public-set session on which it could be fitted.
- **Not attempted:** log damping of the length term (same monotone family, negative at its gentlest
  point) and a true document-mass precision denominator (`sum(idf)` over every distinct token in the
  product). The latter is a different formula but the same hypothesis, which both sides of axis J
  have now falsified.
