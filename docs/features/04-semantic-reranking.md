# 04 — Semantic reranking

**Status:** merged
**Owner:** Dialog + Ranking
**Tier:** 2

## What & why

Whatever RRF emitted was the final order. That was fine while the problem was *finding* the target
at all, and stopped being fine once we were finding it: HitRate@10 sat at 0.825 while MRR sat at
0.420, which is the signature of targets being retrieved and then buried mid-list. MRR is 30% of
the score and it was the weakest term on the board.

The gap exists because BM25 over a ~60-term OR query is the wrong instrument for ordering. It
rewards a product for matching *many* query terms, so a generic listing that happens to contain a
dozen common words ("women", "fashion", "comfortable") outranks the one product carrying the single
rare phrase that actually identifies it. Widening retrieval cannot fix that — it is an ordering
problem, not a recall problem.

## Approach

New module `starter/ranking.py`, the home CLAUDE.md had already reserved for this. `CatalogIndex`
now generates a pool of 120 fused candidates instead of a final 10, and `Reranker.order` decides
which of them surface.

The signal is that a shopper describes what they want in the language of the thing they want, and
the catalog's metadata is where that language comes from — so the product echoing the customer's
exact phrasing, especially phrasing that is *rare across 50k products*, is disproportionately likely
to be the target. Two scores, evenly weighted:

- **coverage** — the share of the evidence's IDF mass the product contains anywhere, discounted by
  which field it was found in (`FIELD_FACTORS`: title 1.0 down to description 0.65).
- **phrase** — the share of that mass surviving as an intact token *sequence*. `"closure type
  buckle"` appearing verbatim is far stronger evidence than those three words scattered across a
  page of copy. Phrases that survive only in fragments get half credit per matching bigram.

Supporting changes:

- `retrieval.tokens()` — splits out of `terms()`, keeping order *and* duplicates. `terms()` dedupes,
  which would silently rewrite any phrase it touched.
- `CatalogIndex.document_frequency()` — IDF comes from FTS5's own `fts5vocab` table, so it is an
  index lookup rather than a counting search.
- `CatalogIndex.document_profile()` — candidate text fetched by rowid (`parent_asin` is UNINDEXED,
  so looking up by it would scan all 50k rows) and cached, since candidates recur across the turns
  of a session. Documents are cached as one space-padded token string, which turns "does this
  phrase occur" into a substring test.
- `DialogState.phrases` / `evidence_phrases()` — the same disclosures as `evidence`, but split into
  individual claims. Retrieval wants a bag of terms; ranking wants the phrases intact.
- `retrieve(..., reranker=...)` wraps the call in `try/except`. A reranker fault must cost ordering,
  never the session — the evaluator scores a raised exception as an outright miss. Instrumented over
  a full run: 500 calls, 0 raised, and the pool is never added to or dropped from.

### The prior we built and then deleted

The first version blended in the fused retrieval order as a third signal, on the reasoning that
reranking should be a correction rather than a replacement, and that evicting a candidate fusion
already had right costs HitRate — worth more than the MRR being chased. That reasoning was wrong,
and the sweep says so clearly:

| prior weight | TechnicalScore |
|---|---|
| 0.35 | 0.81049 |
| 0.10 | 0.82811 |
| 0.00 | 0.84493 |

Both gaps are well outside the ~0.01 noise floor. BM25-over-a-long-OR is simply a weaker ordering
signal than coverage and phrase, and mixing it in drags good candidates down. Fusion still earns its
keep by choosing *which* 120 candidates are considered, and still breaks ties — it just no longer
votes on the order.

Everything else in the sweep was flat. Coverage/phrase splits from 0.35/0.65 to 0.65/0.35 all landed
within 0.004 of each other, so the committed weights are an even split rather than the sweep's
nominal winner. Pool size: 30 → 0.806, 60 → 0.845, 120 → 0.848, 200 → 0.842, 300 → 0.841 — one flat
plateau from 60 up, with a real falloff past 200.

## Measured impact

_results/results_after_clarification.json → results/results_after_reranking.json_

| Metric | Before | After | Delta |
|---|---|---|---|
| HitRate@10 | 0.825 | 0.965 | +0.14 ✅ |
| MRR | 0.420141 | 0.652067 | +0.231926 ✅ |
| MTTC | 3.85 | 2.53 | -1.32 ✅ |
| Efficiency | 0.715 | 0.847 | +0.132 ✅ |
| **TechnicalScore** | **0.681542** | **0.84752** | **+0.165978 ✅** |

### By scenario

| Scenario | n | Metric | Before | After | Delta |
|---|---|---|---|---|---|
| boundary | 10 | HitRate@10 | 0.9 | 1 | +0.1 ✅ |
|  |  | MRR | 0.613333 | 0.8 | +0.186667 ✅ |
|  |  | MTTC | 4.1 | 2.8 | -1.3 ✅ |
| browsing | 80 | HitRate@10 | 0.8625 | 0.9875 | +0.125 ✅ |
|  |  | MRR | 0.38496 | 0.627034 | +0.242074 ✅ |
|  |  | MTTC | 3.4625 | 2.2375 | -1.225 ✅ |
| buying | 80 | HitRate@10 | 0.7875 | 0.9375 | +0.15 ✅ |
|  |  | MRR | 0.366384 | 0.588849 | +0.222465 ✅ |
|  |  | MTTC | 3.75 | 2.2875 | -1.4625 ✅ |
| intent_override | 30 | HitRate@10 | 0.8 | 0.966667 | +0.166667 ✅ |
|  |  | MRR | 0.59291 | 0.838095 | +0.245185 ✅ |
|  |  | MTTC | 5.06667 | 3.86667 | -1.2 ✅ |

**Verdict:** TechnicalScore improved by +0.165978.

Every metric moved in the right direction in every scenario, which is not the usual shape — 02
traded MRR for HitRate. Here HitRate rose *because* ordering improved: the evaluator stops a session
the first turn the target lands in the top 10, so a target promoted from rank 14 to rank 6 converts
a miss into a hit, and converts it earlier. That single mechanism is why MTTC also fell 1.32 turns
without any turn-budget work.

Runtime for the full 200-session replay is ~14s, down from ~60s, because sessions now end sooner.
Still standard library only; `requirements.txt` stays empty.

## Limitations & follow-ups

- **Six of the seven remaining misses are the generic-constraint case** (Known gap 5). Inspecting
  them, the customer's entire disclosure set is boilerplate that describes thousands of products —
  `['cotton', '100% Cotton']` + `['Imported', 'Button closure']` for a casual shirt,
  `['polyester', '100% Polyester']` + `['Imported', 'Zipper closure']` for a parka. There is no
  lexical signal there to find, at any weighting, so this is the ceiling of the approach rather
  than a tuning miss. Dense retrieval or a genuinely semantic model is the only way past it.
- **The seventh is a real ranking failure worth chasing.** A wrist watch whose disclosures were
  *"Gold-tone 18mm stainless steel expansion band…"* and *"100-hour chronograph with lap & split
  times…"* — highly distinctive text that should have pinned it exactly. Worth tracing as a single
  session before any broader ranking work; it likely indicates the phrase never reached the pool
  at all, which would make it a retrieval bug rather than a ranking one.
- **Phrase matching is lexical**, so it degrades if the organizer adds the natural-language
  paraphrasing the spec permits. Coverage is term-level and survives paraphrasing; phrase matching
  would quietly contribute less. Worth re-measuring against paraphrased input before the private
  run, and the even 50/50 split is partly a hedge on this.
- **Tuned on 200 sessions.** The plateau is broad, which is reassuring, but the pool size and field
  factors are fitted numbers and the private set is 4x larger.
- **Untouched:** `user_profile` is still discarded (Known gap 4), state is still first-write-wins
  (Known gap 3), and turns 5–10 still ask dead questions once evidence runs dry (Known gap 2).
