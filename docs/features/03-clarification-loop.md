# 03 — Clarification loop and cross-turn evidence

**Status:** merged
**Owner:** Dialog + Ranking
**Tier:** 1

## What & why

Two bugs, one root cause: the agent was not treating the conversation as a source of information.

1. **It never asked anything.** `ask_attribute` was hardcoded `None`. In the simulator, a null
   attribute makes the customer reply *"Those options are not quite right yet. Ask me about one
   specific attribute."* and disclose nothing (`evaluator/local_evaluator.py:171`).
2. **It threw away what it was told.** Retrieval ran on `user_message` — *this turn's* text only. Even
   if the customer had disclosed something, turn 5's query no longer contained the product category
   from turn 1.

Together these meant all ten turns re-ran an identical query on the opening message. The agent spent
its entire turn budget learning nothing, which is why browsing sat at 0.0375 and boundary at 0.0 —
those are exactly the scenarios that begin with no disclosed constraint.

## Approach

Landed alongside the agreed module split: `starter/retrieval.py` (index, routes, fusion) and
`starter/dialog_state.py` (slots, evidence, question policy), with `agent.py` reduced to
orchestration and the official contract. The split was verified behaviour-neutral first — it scored
`0.124334`, identical to the previous commit — so everything below is attributable to the feature,
not the refactor.

**Ask every turn.** Recommendations are scored on every turn, so a question costs nothing. There is
no ask-versus-recommend tradeoff to balance; we do both, always.

**Attribute policy** (`ASK_ORDER` in `dialog_state.py`). `other` leads, then `feature`, then the
specific attributes. The reason is in `local_evaluator.py:178-181`: the disclosure filter is
`attribute == "other" or classify_constraint(value) == attribute`, so `other` is the only attribute
that cannot whiff — it matches any undisclosed constraint rather than a single bucket, and returns up
to two per turn. Asking `color` on a target with no colour constraint burns the turn. Specific
attributes follow so the dialogue still reads naturally once the broad ask is spent.

**Retire spent attributes, but not deflections.** *"I don't have an **additional** preference for X"*
means X is genuinely empty — retire it. *"I don't have a preference for X; please use your
judgment"* is the boundary customer deferring once; that must **not** retire the attribute, or we
would throw away every remaining question in exactly the scenario that was scoring 0.0.

**Accumulate evidence.** Every disclosure is appended to `DialogState.evidence` and the query is
built from all of it, oldest first (the opener carries the category). Retirement notices and the
no-attribute nudge carry no target information and are deliberately not accumulated. `MAX_QUERY_TERMS`
went 40 → 64 to fit a whole session's disclosures.

The override message (*"Actually, ignore my earlier preference. What I need is: X"*) is parsed for
its payload too, since the evaluator marks that value disclosed and it can never be harvested by
asking.

**Why this works so well:** the intent card is built from the target product's own `features` and
`details` (`local_evaluator.py:52-71`), so a disclosure is near-verbatim text from the target's
catalog entry. Getting the customer to speak is effectively getting them to quote the answer.

## Measured impact

_results_after_multiroute.json → results_after_clarification.json_

| Metric | Before | After | Delta |
|---|---|---|---|
| HitRate@10 | 0.15 | 0.825 | +0.675 ✅ |
| MRR | 0.068446 | 0.420141 | +0.351695 ✅ |
| MTTC | 9.56 | 3.85 | -5.71 ✅ |
| Efficiency | 0.144 | 0.715 | +0.571 ✅ |
| **TechnicalScore** | **0.124334** | **0.681542** | **+0.557208 ✅** |

### By scenario

| Scenario | n | Metric | Before | After | Delta |
|---|---|---|---|---|---|
| boundary | 10 | HitRate@10 | 0 | 0.9 | +0.9 ✅ |
|  |  | MRR | 0 | 0.613333 | +0.613333 ✅ |
|  |  | MTTC | 11 | 4.1 | -6.9 ✅ |
| browsing | 80 | HitRate@10 | 0.0375 | 0.8625 | +0.825 ✅ |
|  |  | MRR | 0.005035 | 0.38496 | +0.379925 ✅ |
|  |  | MTTC | 10.625 | 3.4625 | -7.1625 ✅ |
| buying | 80 | HitRate@10 | 0.2875 | 0.7875 | +0.5 ✅ |
|  |  | MRR | 0.125456 | 0.366384 | +0.240928 ✅ |
|  |  | MTTC | 8.125 | 3.75 | -4.375 ✅ |
| intent_override | 30 | HitRate@10 | 0.133333 | 0.8 | +0.666667 ✅ |
|  |  | MRR | 0.108333 | 0.59291 | +0.484577 ✅ |
|  |  | MTTC | 10.0667 | 5.06667 | -5 ✅ |

**Verdict: 5.5x the previous TechnicalScore, and 6.4x the organizer's baseline.** Every scenario
improved. The two scenarios that were near-zero — browsing and boundary — are now the two strongest,
which is the expected shape: they had the most to gain from a conversation because they start with
nothing disclosed.

### Ablation: which half did the work?

| Configuration | TechnicalScore | HitRate@10 | MTTC |
|---|---|---|---|
| Neither (previous commit) | 0.124334 | 0.15 | 9.56 |
| Asking only, no accumulation | 0.526557 | 0.62 | 5.655 |
| Asking + accumulation | **0.681542** | **0.825** | **3.85** |

Asking is the larger half (`+0.402`), but accumulation is not a rounding error (`+0.155` on top, and
it alone cuts MTTC from 5.655 to 3.85). Neither is worth dropping. Note that accumulation is
worthless without asking — with nothing disclosed there is nothing to accumulate — which is why
these were built and measured as one feature.

## Limitations & follow-ups

- **Turns are wasted once evidence runs dry.** Traced `public_0002`: all evidence is spent by turn 4,
  then turns 5–10 walk down `ASK_ORDER` collecting *"I don't have an additional preference"* while
  returning an unchanged list. Harmless to the score today because a miss costs turn 11 regardless,
  but it is the obvious Tier-3 turn-budget target and it reads badly in a demo. A strategy switch —
  stop asking, start re-ranking — is the fix.
- **`other` is a simulator-specific advantage.** The catch-all behaviour comes from the released
  evaluator. The spec warns the organizer may paraphrase customer messages; the private simulator
  could plausibly treat `other` more strictly. The `ASK_ORDER` fallback means we degrade to targeted
  asking rather than breaking, but the private score may not match the public one as closely as it
  usually would. **This is the single biggest risk to the number above** and should be stated plainly
  in the final report.
- **Intent override is still unhandled.** Slots remain first-write-wins, so a contradicting colour or
  material never overwrites and can keep a wrong hard `AND` filter in place. The scenario improved to
  0.8 on evidence accumulation alone; erase-and-rewrite should take it further.
- **Generic constraints still fail.** `public_0002` misses because *"leather; 100% Leather; Imported;
  Buckle closure"* does not distinguish one leather belt from hundreds. No amount of asking fixes a
  target whose disclosed constraints are not discriminative — that needs reranking or dense retrieval.
- **MRR is now the weak term.** HitRate@10 is 0.825 but MRR is 0.420, so targets are being found and
  then buried mid-list. With HitRate this high, reranking (Tier 2) is now the highest-value remaining
  work: it is worth up to `0.30 × ~0.4` of headroom.
- `MAX_QUERY_TERMS = 64` and the `ASK_ORDER` sequence are both untuned first guesses.
