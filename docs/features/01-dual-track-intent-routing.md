# 01 — Dual-track intent routing

**Status:** merged
**Commit:** `0b95776`
**Owner:** Retrieval & Routing
**Tier:** 1

> Backfilled from git history after the fact. The measured impact below is real — recovered by
> restoring the `results/results_after_routing.json` snapshot the commit originally shipped.

## What & why

The starter agent was fully stateless: every turn it tokenised the incoming message, ran one BM25
query over the whole catalog, and forgot everything. A customer who opened with *"a key requirement
is: cotton"* got no more precision on turn 5 than on turn 1.

Problem-statement Pillar I asks for Buying vs. Browsing routing, and it maps directly onto
HitRate@10 (50% of the score): a disclosed hard constraint should **shrink** the candidate pool,
while a vague opener should **widen** it. Treating both the same wastes the constraint.

## Approach

Two pieces, both in `starter/agent.py`.

**Per-session constraint state.** `reset()` now seeds `self._sessions[session_id]` with
`{price_max, color, material}`. `_detect_constraints` regex-scrapes those three from each incoming
message — a colour/material word list, plus `$N` and phrasings like *under $30* / *no more than $30*.
`respond()` merges newly detected values into the session state, so a constraint disclosed on turn 2
still applies on turn 7.

**Track selection.** If any slot is filled, the session is on the **buying** track: colour and
material become required `AND` terms in the FTS5 expression, and `price_max` becomes a SQL predicate
(`price IS NULL OR price <= ?` — nulls are kept rather than dropped, since a missing price is not
evidence of being expensive). With no slot filled the session stays on the **browsing** track: the
original wide `OR` query, unfiltered.

BM25 column weights were also tuned in the same commit — title 6.0, categories 4.0, features and
details 2.5, store 1.5, description 1.0 — on the reasoning that a target's title is far more
diagnostic than its marketing copy.

## Measured impact

_baseline_results.json → results/results_after_routing.json_

| Metric | Before | After | Delta |
|---|---|---|---|
| HitRate@10 | 0.125 | 0.13 | +0.005 ✅ |
| MRR | 0.068034 | 0.070095 | +0.002061 ✅ |
| MTTC | 9.81 | 9.76 | -0.05 ✅ |
| Efficiency | 0.119 | 0.124 | +0.005 ✅ |
| **TechnicalScore** | **0.10671** | **0.110829** | **+0.004119 ✅** |

Scenario values after this change (the organizer's baseline file has no per-scenario breakdown, so
there is nothing to diff against):

| Scenario | n | HitRate@10 | MRR | MTTC |
|---|---|---|---|---|
| buying | 80 | 0.25 | 0.126974 | 8.5 |
| browsing | 80 | 0.025 | 0.004514 | 10.75 |
| intent_override | 30 | 0.133333 | 0.116667 | 10.066667 |
| boundary | 10 | 0.0 | 0.0 | 11.0 |

**Verdict: within noise.** `+0.005` HitRate@10 is exactly **one session** out of 200, and the
TechnicalScore delta of `+0.004119` sits below the ~0.01 noise floor. This should be read as *"laid
the state plumbing, did not itself move the number"* — not as a win. Its real value is that it made
feature 02 possible.

## Limitations & follow-ups

- **Constraint merge is first-write-wins** (`if value is not None and state[key] is None`), so a
  customer who changes their mind is ignored. This is precisely why intent_override sat flat at
  0.133 — the override message on turn 3/4 lands in an already-filled slot and is dropped. Fixing it
  needs erase-and-rewrite semantics (Tier 2).
- **Only three slots** — colour, material, price — against ten allowed `ask_attribute` values. Size,
  style, brand, and use_case are extracted from nothing.
- **Regex word lists are closed.** `MATERIAL_RE` knows nine fabrics and `COLOR_RE` twelve colours;
  anything else in the catalog is invisible.
- **Browsing barely moved** (0.025), because the browsing track is still just the unmodified wide
  query. Nothing here helps a customer who starts vague — that needs the clarification loop.
