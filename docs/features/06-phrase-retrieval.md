# 06 — Phrase retrieval, and two constraint-extraction bugs

**Status:** merged
**Owner:** Retrieval & Routing
**Tier:** 2

## What & why

This was scoped as "dense retrieval" — CLAUDE.md had named it the only large pot left. It turned
out to be the wrong diagnosis, and finding that out cost an hour and saved building an embedding
stack that would not have helped.

Tracing the seven missed sessions turn by turn showed the same thing every time:

```text
T1 ask='other'  shown=1  target_in_pool200=None
T2 ask='other'  shown=1  target_in_pool200=None
T3 ask='other'  shown=4  target_in_pool200=None
```

`target_in_pool200=None` on *every* turn. The targets were never retrieved at all, not even 200
deep. **This was a recall failure, not a ranking failure** — there was nothing in the pool for a
better reranker, dense or otherwise, to reorder. Semantic similarity would also have placed the
3,000 near-identical shirts in the same place.

Three separate causes, found by instrumenting the pipeline rather than by reading it.

## Cause 1 — the price regex parsed measurements as budgets

`PRICE_PHRASE_RE` matched `up to|under|at most` followed by a bare number. So:

```text
"Gold-tone 18mm stainless steel expansion band fits up to 8-inch wrist circumference"
  -> price_max = 8.0
```

That became a hard `price <= 8` filter applied to every route, which excluded the very Timex watch
the customer was describing. `public_0042` could not be retrieved on any turn of any session.

Measured across the public set: **three such false positives, and zero genuine `$` prices.** The
price filter had never once fired correctly here — it was pure downside.

Fixed with a unit blocklist (`inch`, `hour`, `pair`, `mm`, `count`, …) plus a digit guard. The guard
matters and is not obvious: without it the engine backtracks, `\d+` gives up `"30"` and retries
`"3"`, the unit lookahead then sees `"0mm"` — which starts with no unit — and `"up to 30mm"` yields
a **$3** ceiling. `(?!\d)` after the capture stops a truncated number from ever satisfying it.

## Cause 2 — phrase queries were built with the wrong tokenizer

`tokens()` drops stopwords. That is correct for reranking, where both the phrase and the document
profile go through it and stay self-consistent. It is wrong for *querying*, because the FTS5 index
still contains the stopwords:

```text
tokens("Pull On closure")      -> ["pull", "closure"]
FTS5 phrase query              -> "pull closure"      1 document
what the index actually holds  -> "pull on closure"   7184 documents
```

A common phrase silently becomes a rare one and matches the wrong documents. Added `fts_tokens()`,
which reproduces what `unicode61` did at index time, and left `tokens()` alone. Both are documented
with why they differ, since the natural instinct is to unify them.

## Cause 3 — intact phrases were never queried at all

The keyword route dissolves every disclosure into a bag of terms and scores products by BM25 over a
~60-term OR, fetching the top 600. A product carrying the customer's exact wording has no way to
stand out: its distinctive phrase is just five more terms in the OR.

`public_0042`'s target holds three phrases with a document frequency of **one**:

```text
df=1   "Gold-tone 18mm stainless steel expansion band fits up to 8-inch wrist circumference"
df=1   "100-hour chronograph with lap & split times"
df=1   "month, day & date calendar"
```

Three effectively unique identifiers, and the target was still never retrieved.

### Approach

A third route in `CatalogIndex.retrieve`: one FTS5 phrase query per disclosure specific enough to
narrow the catalog. `phrase_routes` scores each candidate phrase by document frequency, drops
boilerplate above `PHRASE_DF_MAX` (`"Imported"` at 15300, `"Button closure"` at 2391) and anything
under two tokens, keeps the twelve rarest, and weights each by inverse document frequency. A df=1
phrase returns a one-item list, so RRF ranks it first and weights it accordingly.

Phrase routes are deliberately **unfiltered** — by `and_terms` and by price alike. An intact phrase
is far stronger evidence than a regex-scraped colour or budget, so a wrong filter must not be able
to suppress the one route that identifies the product. Fusion and reranking still both have to
agree before anything surfaces.

## Measured impact

_results/results_after_disclosure.json → results/results_after_phrase.json_

| Metric | Before | After | Delta |
|---|---|---|---|
| HitRate@10 | 0.965 | 0.975 | +0.01 ✅ |
| MRR | 0.85222 | 0.857935 | +0.005715 ✅ |
| MTTC | 2.965 | 2.88 | -0.085 ✅ |
| Efficiency | 0.8035 | 0.812 | +0.0085 ✅ |
| **TechnicalScore** | **0.898866** | **0.907281** | **+0.008415 ✅** |

### By scenario

| Scenario | n | Metric | Before | After | Delta |
|---|---|---|---|---|---|
| boundary | 10 | HitRate@10 | 1 | 1 | 0 |
|  |  | MRR | 0.95 | 0.95 | 0 |
| browsing | 80 | HitRate@10 | 0.9875 | 0.9875 | 0 |
|  |  | MRR | 0.855432 | 0.855432 | 0 |
| buying | 80 | HitRate@10 | 0.9375 | 0.9625 | +0.025 ✅ |
|  |  | MRR | 0.837396 | 0.851682 | +0.014286 ✅ |
|  |  | MTTC | 2.85 | 2.6375 | -0.2125 ✅ |
| intent_override | 30 | HitRate@10 | 0.966667 | 0.966667 | 0 |
|  |  | MRR | 0.850595 | 0.850595 | 0 |

**`score_delta.py` flags the aggregate as within the ~0.01 noise floor, and it is right to.** The
reason to keep it anyway is that the movement is not spread thinly across the set — it is entirely
inside `buying` (HitRate +0.025, MTTC -0.21, everything else exactly 0.000), which is precisely
where the mechanism predicts it. The price filter only applies on the buying track, and both
rescued sessions are buying sessions. Two bugs were also fixed that cost nothing on this split but
are live on any split containing a real `$` price or a stopword phrase.

Per-session: **4 improved, 1 regressed** (`public_0067`, rank 1 → 2). Misses went 7 → 5:

- `public_0042` **miss → rank 1** (price fix, then the df=1 phrase routes)
- `public_0179` **miss → rank 7** (phrase route on `"Made in the USA"`, df=933)

### Ablation and tuning

The two fixes are complementary — each rescues a different session:

| Variant | HitRate | MRR | Score |
|---|---|---|---|
| neither (feature 05) | 0.9650 | 0.85222 | 0.898866 |
| price fix only | 0.9700 | 0.85722 | 0.903766 |
| phrase route only | 0.9700 | 0.85064 | 0.901693 |
| **both** | **0.9750** | **0.857935** | **0.907281** |

Nineteen parameter variants were swept. Two findings:

- **`PHRASE_DF_MAX` is a threshold, not a gradient.** Everything below 1000 scores 0.9700;
  everything at or above scores 0.9750. The step is `public_0179`'s rescuing phrase at df=933.
- **Over-weighting phrases hurts.** `weight=2.0` drops HitRate back to 0.9700 at *every* df cap —
  a dominant phrase route displaces candidates the fusion already had right.

Settings at df_max >= 1000 and weight <= 1.0 all land within 0.0008 of each other, so the argmax
(`df_max=4000, weight=1.0`, 0.907343) is not meaningfully better than its neighbours. Shipped
`df_max=2000, weight=0.5` — the conservative point on both axes, keeping solo routes to genuinely
specific phrases and stopping any one phrase dominating fusion.

## Rejected: the conjunction route

Built and measured, then backed out. The idea was sound on paper: a phrase can be far too common to
lead a route and still be decisive in combination.

```text
"tie closure"      570 products
"100 polyester"   3113 products
intersection       100 products, target included
```

Same shape for `public_0087` (402) and `public_0144` (578). All three were still missed.

| | HitRate | MRR | MTTC | Score |
|---|---|---|---|---|
| without conjunction | 0.9750 | 0.857935 | 2.880 | **0.907281** |
| with conjunction | 0.9700 | 0.856456 | 2.930 | 0.903337 |

**It rescued none of the three sessions it was designed for, and lost `public_0161`.** The reason is
structural and worth remembering before anyone tries it again: **RRF scores an item by its rank
within a route, not by how small the route's result set is.** Narrowing to 100 near-identical robes
leaves the target at roughly rank 50 of 100, contributing `1/(60+50)` — negligible next to a
rank-1 hit on any other route. A small candidate set is only worth something if you can order it,
and these candidates are boilerplate-identical, so nothing can.

## Limitations & follow-ups

- **Five misses remain, and they are genuinely unreachable by retrieval.** Their disclosed
  constraints are shared with thousands of products: `public_0087` discloses only
  `"cotton"` / `"100% Cotton"` / `"Imported"` / `"Button closure"` (df 9775 / 3770 / 15300 / 2391).
  No lexical *or* dense method separates a target from 3,000 items when the evidence is identical
  across all of them. Two of the five (`public_0020`, `public_0145`) are additionally hurt by a
  scraped colour that the target's own text does not contain — that belongs to the first-write-wins
  gap, not here.
- **Phrase routes ignore a disclosed price ceiling.** Deliberate, and free on this split (zero real
  prices), but on a split with genuine budgets it can surface an over-budget product into the pool.
  It still has to win fusion and reranking to be shown.
- **Diagnosis used ground truth; the agent does not.** The misses were identified by inspecting the
  targets of failing sessions. That is analysis, and no shipped code reads `ground_truth` — but the
  parameters are tuned on 200 sessions, which is why the tuning was left coarse.
- **`fts_tokens` and `tokens` must stay separate.** They look like duplicates and are not. Merging
  them silently breaks either phrase querying or phrase reranking, depending on which survives.
