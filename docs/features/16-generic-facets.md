# 16 — Generic attribute facets, and a ratchet that enforces the guarantee

**Status:** merged
**Commit:** (this one)
**Owner:** Dialog + Ranking
**Tier:** 3 (product quality) — byte-identical on the scored set, by construction

## What & why

Reported from the WebUI: asking for **men's** clothing returned ten women's products, and
the agent replied *"Narrowed to items matching blue, cotton, under $50.00"* — apparently
ignoring "round neck" and "men tshirt" entirely. Three distinct defects, diagnosed before
any code changed.

**1. Filler words dominated the ranking.** `under 50 dollars` was parsed into
`price_max=50.0` *and left in the query as text*. Measured IDF over that exact query:

| term | df | IDF |
|---|---|---|
| `dollars` | 56 | **6.79** |
| `tshirt` | 441 | 4.73 |
| `leisure` | 647 | 4.35 |
| `under` | 1,743 | 3.36 |
| `50` | 2,327 | 3.07 |
| `men` | 14,908 | **1.21** |

`under 50 dollars` carried 13.2 of 40.5 total IDF — **33% of the entire ranking signal**
on words describing no product — and `dollars` alone outweighed `tshirt`. With the
conversational filler from the leisure answer, roughly 62% of the mass was noise.

**2. Gender was an ordinary keyword.** `men` was the *weakest* term in the query at 3% of
its mass, in a catalog where 32,347 of 50,000 products mention "women", and nothing
penalised a product for asserting the **opposite** value.

A hard `AND "men"` does **not** fix this, which is the non-obvious part: 5,900 products
contain "men" outside their title — keyword spam like *"gifts for men women teens"* — so
women's listings satisfy the filter. Measured: requiring `men` still returned women's
items at ranks 1, 2 and 5.

What works is scoping to the **title** and demoting titles asserting a sibling value —
9,039 products have "men" in the title against 21,008 for "women", with only 1,350 both.

**3. `message()` could only ever name three things.** `SLOTS` is
`("price_max", "color", "material")`, so the agent had no way to *say* "men tshirt" even
though it was in `evidence` and did reach retrieval. A reporting bug that made a real bug
look worse.

## Approach

`starter/facets.py` generalises the gender fix into a mechanism: a **facet** is a group of
mutually exclusive values, and stating one implies rejecting its siblings. Ten groups
ship — gender, neckline, sleeve, fit, rise, length, closure, pattern, occasion, season.
**Adding a parameter is a dictionary entry, not new code.**

Two retrieval effects, both title-scoped:

- **Route 6** (`FACET_ROUTE_WEIGHT = 0.3`) gets facet-matching products into the pool.
- **`demote_title_forms`** pushes titles asserting a sibling value to the back, after
  reranking. Demotion, not deletion — 1,350 titles legitimately carry both.

Budget phrasing is stripped from the query text once parsed (`_strip_price_phrasing`); the
ceiling survives as the numeric filter, which is the only place it belongs.

### How this is guaranteed not to move the score

The scored path and the human path are disjoint, and this is now **measured, not
asserted**: a full run makes **566 `observe()` calls and 0 `_observe_freeform` calls.**
Every simulator reply is claimed by an earlier regex.

So every new capability enters as an **optional parameter that is empty on every scored
turn**, guarded by `if x:` — the pattern `avoid_terms` already established. `facets` is
`{}` during scoring, so route 6 and the demotion never execute.

Turn 1 needed a second application of the same idea. The opener takes an early return
before the free-form branch, so it got no facet detection — which is exactly the message a
person puts the most into. `Agent(..., freeform=True)` is set by the WebUI, which *knows*
its user is human; the evaluator never sets it, so the scored opener still appends the raw
message and returns as before.

Two guards keep this true over time, in `tools/verify_features.py`:

- **The isolation invariant** — asserts `_observe_freeform` is called **0** times across a
  full scored run. If a future edit widens a regex so a simulator reply falls through,
  this goes red *before* it can silently change the score.
- **`tools/score_ratchet.py`** — runs the full 200 sessions and **exits non-zero if the
  score fell**, distinguishing *byte-identical* from merely *score-equal* (offsetting
  session movements can hide a regression the 800-session private set would not forgive).

## Measured impact

| Metric | Before | After | Delta |
|---|---|---|---|
| HitRate@10 | 0.98 | 0.98 | 0 |
| MRR | 0.864018 | 0.864018 | 0 |
| MTTC | 2.85 | 2.85 | 0 |
| **TechnicalScore** | **0.912205** | **0.912205** | **0** |

**Byte-identical** — the whole sessions array, not just the aggregate. All four scenarios
zero on every metric.

Free-form behaviour, which is the point:

| Query | Before | After |
|---|---|---|
| `round neck, blue, cotton, under 50 dollars, men tshirt` | 0/10 men's | **8/10 men's** |
| agent's reply | "blue, cotton, under $50.00" | "men, crew neck, blue, cotton, under $50.00" |
| `long sleeve plaid flannel shirt for men` | — | sleeve + pattern + gender all detected |
| `womens high waisted skinny jeans` | — | rise + gender + fit all detected |

## Two bugs found on the way

The structural test comparing `Agent._respond`'s `retrieve()` kwargs against
`webui/agent_bridge.py::_deep_list`'s caught two **pre-existing** divergences:

- `avoid_terms` was never passed to the deep list. So since feature 15, typing
  *"not polyester"* made the display list disagree with the agent's answer, the
  consistency guard fired, and the page silently collapsed from 50 rows to 1. Confirmed
  against the running server before fixing.
- `extra_terms` had the same problem, latent because `expand` mode is off by default.

Both fixed. The kwarg comparison is now asserted structurally so a future parameter cannot
be forgotten.

## Limitations & follow-ups

- **Demotion only fires on an explicit opposite assertion.** A women's dress whose title
  never says "women" is not demoted — one such item still reaches rank 9 of 10 on the
  original query. Catching those needs a category signal, not a title token.
- **Facet vocabulary is curated, not mined.** Ten groups cover the common apparel axes;
  a value nobody listed is simply not a facet, and falls back to ordinary keyword
  evidence — no worse than before.
- **First statement wins per group.** Free-form corrections overwrite slots but not
  facets, so "actually make it long sleeve" after "short sleeve" keeps the first. Worth
  fixing if it comes up in a demo.
- **None of this can raise the competition score**, and it was never meant to — the code
  is unreachable while scoring. The filler-word fix is the one piece with plausible
  *scored* upside (33% of the query's IDF mass was noise), but moving it onto the scored
  path is a separate, measured experiment: it would change what the simulator's own
  disclosures retrieve, and must go through the ratchet.
