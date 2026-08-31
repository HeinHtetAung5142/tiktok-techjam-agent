# 15 — Free-form negation and slot-aware questioning

**Status:** merged
**Commit:** <sha>
**Owner:** Dialog + Ranking
**Tier:** 3 (free-form input robustness — WebUI/demo path only)

## What & why

Two defects visible in a single WebUI transcript, both on the free-form (human) path added in
feature 11:

1. **Negation was inverted into a requirement.** A person typed *"i want it to be grey and not
   fully polyester"*. `detect_constraints` scrapes the first vocabulary match it finds, with no
   notion of a preceding "not", so `material` was set to `polyester` — and every subsequent turn
   answered **"Narrowed to items matching grey, polyester."** The agent stated the exact opposite of
   the requirement back to the customer, and then filtered and ranked on it.
2. **The agent re-asked what it had just been told.** Nothing retires an attribute except the
   customer answering the question we asked *that* turn (`last_asked`). Having been handed the
   colour unprompted, the agent still worked down `ASK_ORDER` into "Do you have a material
   preference?" and then "Any particular colour you're after?", to which the person replied *"yes as
   i mentioned polyester"* and *"as i mentioned grey"*. Every one of those turns was spent
   re-collecting information already in the slots.

Neither is a scoring bug. The simulated customer never negates — `intent_card`
(`evaluator/local_evaluator.py:52-71`) builds every disclosure out of the target's own positive
`features`/`details`, so there is no "not X" for it to emit — and it only ever volunteers a
constraint in reply to the attribute it belongs to. Both defects are therefore confined to the
speaker the score cannot see, which is exactly the speaker a judge sees in a live demo.

## Approach

Everything is reached only from `DialogState._observe_freeform`, which the simulator cannot enter
(feature 11 documents the four sentence shapes that each get claimed earlier).

**Negation** (`starter/dialog_state.py`). `NEGATION_CUE_RE` matches a negation cue that ends within
24 characters of the value and inside the same clause (`[^.,;!?]{0,24}$` against the text *before*
the match), so "anything but leather" negates and "no rush, I want a black leather belt" does not.
`detect_constraints` now routes both vocabularies through a new `_scan(pattern, message,
honor_negation)`, which returns `(first wanted value, every ruled-out value)`. **With
`honor_negation` false it is exactly `pattern.search()` — first match wins, nothing excluded —
which is what the scored path has always done.** `detect_exclusions` exposes the second half of that
pair to the free-form path only.

`_observe_freeform` records exclusions into `DialogState.avoided` *before* any slot write and
*before* the optional model is consulted, so neither the regex nor an LLM expansion can reintroduce
the refused value; a slot already holding a now-refused value is cleared. `_scrub` then takes the
word out of `evidence` and `phrases` — the gentler sibling of `_supersede`, which drops a phrase
whole. That distinction matters here: "grey but not polyester" is one phrase making two claims, and
`_supersede` would have discarded the colour they wanted along with the material they didn't.

**Exclusions reach the results.** `CatalogIndex.demote_terms` partitions an ordered list by whether
the product's cached `document_profile` token string contains a ruled-out term, and returns
`kept + demoted`. `retrieve` calls it *after* reranking (so the reranker cannot undo it) under
`if avoid_terms:`, and `agent.py` passes `state.avoid_terms()`, which is `[]` on every scored turn.
It **demotes rather than deletes**: catalog material text is noisy ("polyester lining"), a short
list is worse than a badly ordered one, and a wrong exclusion must not be able to hide the target.
`DialogState.message` says it out loud — *"Narrowed to items matching grey, avoiding polyester."* —
so a person can see the negation landed as an exclusion instead of silently inverting.

**Slot-aware questioning.** `_observe_freeform` retires the attribute for every filled slot via
`SLOT_ATTRIBUTES` (`color`, `material`, `price_max` → `budget`), in addition to the existing
`last_asked` retirement. The question rotates onward instead of asking for something already known.

Two assertions in the verification harnesses pinned the *old* behaviour and were updated, not
weakened: `verify_features.py` expected `exhausted == {"other"}` after a free-form colour
correction, and `verify_llm.py` the same for the dead-model degradation check. Both now expect
`{"other", "color"}` — the colour slot is filled, so its question is spent. Six new checks cover
negation-as-exclusion, the scrub, the spoken exclusion, the scored-vocabulary guard, filled-slot
retirement, and `demote_terms` losing nothing.

## Measured impact

_results/results_after_fieldfactors.json → results_negation_check.json_

| Metric | Before | After | Delta |
|---|---|---|---|
| HitRate@10 | 0.98 | 0.98 | 0 |
| MRR | 0.864018 | 0.864018 | 0 |
| MTTC | 2.85 | 2.85 | 0 |
| Efficiency | 0.815 | 0.815 | 0 |
| **TechnicalScore** | **0.912205** | **0.912205** | **0** |

### By scenario

| Scenario | n | Metric | Before | After | Delta |
|---|---|---|---|---|---|
| boundary | 10 | HitRate@10 | 1 | 1 | 0 |
|  |  | MRR | 1 | 1 | 0 |
|  |  | MTTC | 3.2 | 3.2 | 0 |
| browsing | 80 | HitRate@10 | 0.9875 | 0.9875 | 0 |
|  |  | MRR | 0.853001 | 0.853001 | 0 |
|  |  | MTTC | 2.725 | 2.725 | 0 |
| buying | 80 | HitRate@10 | 0.975 | 0.975 | 0 |
|  |  | MRR | 0.852133 | 0.852133 | 0 |
|  |  | MTTC | 2.525 | 2.525 | 0 |
| intent_override | 30 | HitRate@10 | 0.966667 | 0.966667 | 0 |
|  |  | MRR | 0.879762 | 0.879762 | 0 |
|  |  | MTTC | 3.93333 | 3.93333 | 0 |

**Not merely flat — byte-identical.** The output file is identical to
`results/results_after_fieldfactors.json` at 38,523 bytes, sessions array included, which is the same
standard features 11–13 were held to. `verify_features.py` 67/69 (the two XFAILs are known gap 4),
`verify_llm.py` 96/96.

## Limitations & follow-ups

- **Cue-based, not parsed.** Negation scope is approximated by proximity. "not the polyester one,
  the cotton" excludes polyester and keeps cotton, but a cue separated from its value by a comma or
  a clause boundary is deliberately not matched — false negatives were chosen over false
  exclusions, since wrongly excluding is the more damaging error.
- **Exclusions are permanent within a session.** There is no way to say "actually polyester is fine
  after all"; `avoided` only grows. A retraction cue could clear it, but nothing observed needs it
  yet.
- **Demotion is whole-word substring matching** over the product token string, so "polyester" in a
  care label demotes as strongly as "polyester" in the material field. Weighting it by the field
  factors already in `document_profile` would be more precise; it was not measurable here.
- **Slot retirement is free-form only.** Making `next_attribute` skip filled slots globally would
  change which questions the scored sessions ask, and with them the score. If that is ever wanted,
  it is a measured change, not a cleanup.
- The exclusion still does not enter the FTS5 query itself. A `NOT` term would only bind routes 1–2
  — phrase, dense and backfill routes are deliberately unfiltered — so post-rerank demotion covers
  strictly more of the pipeline.
