# 11 — Free-form input robustness

**Status:** merged
**Commit:** (this one)
**Owner:** Dialog + Ranking
**Tier:** 3 (feasibility / demo) — deliberately score-neutral

## What & why

Reported from manual testing through the WebUI (`py -m webui.server`, feature added in `a7a652f`):
*"agent gets stuck and keeps repeating msg after a color is mentioned, changing the color doesn't
work either."*

Not a WebUI bug. `DialogState.observe` only understood the sentence shapes the **simulated**
customer emits. A person's free text matched none of them, hit the explicit fall-through
(*"Anything else ... is deliberately not accumulated"*), and was discarded whole. Three things then
froze at once, which is why it reads as the agent hanging rather than as one missing feature:

| Symptom | Mechanism |
|---|---|
| Same question every turn | `self.exhausted` only ever grew via `EXHAUSTED_RE`, which needs the literal *"I don't have an additional preference for X."* Nobody types that, so `next_attribute()` returned `ASK_ORDER[0]` = `"other"` forever. |
| Same prefix every turn | One colour word flips `is_buying`, and `message()` then emits `"Narrowed to items matching blue."` permanently. |
| Same ranking every turn | No evidence accumulated, so `evidence_text()` stayed at the turn-1 opener and retrieval — being deterministic — returned an identical list. |
| Changing the colour did nothing | First-write-wins: `if value is not None and self.slots[key] is None`. The new colour was detected correctly and then silently dropped, and the stale one stayed in the hard FTS5 `AND` filter via `and_terms()`. |

This targets **no scoring term**. It is a demo/manual-testing fix, plus insurance against the
paraphrasing the spec says the organizer may add to the private set. It is written so that it
*cannot* move the score, and that claim is verified rather than asserted.

## Approach

All changes are in `starter/dialog_state.py`.

### Part A — a free-form branch that the simulator can never reach

`observe()` keeps its four scripted branches byte-for-byte, each now returning explicitly. A new
final branch, `_observe_freeform`, handles anything that matched none of them.

**Why that branch is unreachable on the scored set.** `customer_reply`
(`evaluator/local_evaluator.py:166-185`) has exactly four return shapes, and each is claimed by an
existing regex before the fall-through:

| Evaluator line | Text | Claimed by |
|---|---|---|
| `:169` | `I don't have a preference for {attr}; please use your judgment.` | `DECLINE_RE` |
| `:183` | `I don't have an additional preference for {attr}.` | `EXHAUSTED_RE` |
| `:185` | `For that, what matters is: ...` | `DISCLOSURE_RE` |
| `:85` | `Actually, ignore my earlier preference. What I need is: ...` | `OVERRIDE_RE` |

The fifth shape, `:171` *"Those options are not quite right yet"*, is emitted only when
`ask_attribute` is null — and `next_attribute()` cannot return `None` on the scored set: retiring one
attribute costs one `EXHAUSTED_RE` reply, and even a full 10-turn miss session retires at most 9 of
the 10 entries in `ASK_ORDER`. So the branch is dead code during scoring, which is what makes every
decision inside it free.

Inside the branch:

1. **The most recent value wins.** Any newly stated colour/material/budget replaces what is in the
   slot.

   > **Corrected after a bug report.** The first version of this required an explicit cue
   > (*actually*, *make it*, *instead*) before a filled slot could be replaced, reasoning that a
   > passing mention should not overwrite. That is wrong in the case that matters most: asked
   > *"Any particular colour you're after?"*, a person answers **"red"** — no cue — so the slot kept
   > `blue` and every following turn still said *"Narrowed to items matching blue"*. To the person
   > typing, that is the colour simply not changing, which was the original complaint. There is no
   > reading of "red" that means "still blue", so the cue gate is gone and `CORRECTION_RE` with it.
2. **The superseded value is scrubbed from the evidence, not just the slot.** `_supersede()` strips
   the old word from `evidence` and drops any `phrases` entry containing it. This is the half that
   is easy to miss: `evidence_text()` feeds retrieval and `phrases` feeds the reranker, so replacing
   the slot alone changes the hard filter while the ranking keeps serving blue items — which to the
   user is still *"changing the colour doesn't work"*.
3. **The reply becomes evidence.** With correction scaffolding stripped, the message is appended to
   `evidence`/`phrases` through the existing `phrase_units()`. This is what finally makes the
   ranking move between turns.
4. **The answered attribute retires.** `next_attribute()` records `last_asked` (it is called exactly
   once per turn, `starter/agent.py:150`); the free-form branch retires it, so the question rotates
   `other → feature → material → color → style → ...` instead of repeating.
5. **The correction is spoken aloud.** `message()` prepends e.g. *"Switched colour from blue to
   red."* so a person can see the correction landed. Multiple slots corrected in one message are
   joined.
6. **Wider vocabularies, kept separate.** `COLOR_EXTENDED_RE` / `MATERIAL_EXTENDED_RE` add `navy`,
   `beige`, `charcoal`, `denim`, `linen`, `suede` and friends. `COLOR_RE` / `MATERIAL_RE` are
   **untouched**, because `detect_constraints` also runs on evaluator messages — widening them in
   place would change which slots fill on the public set, and with them the score. The wider set is
   reached only via `detect_constraints(..., extended=True)`.

### Part B — an override now clears the hard filter (measured separately)

CLAUDE.md carried this as a demoted gap with the hook already in place: `OVERRIDE_RE` was matched but
never used to clear slots, so after *"ignore my earlier preference"* the abandoned colour kept
filtering for the rest of the session. `observe()` now clears `HARD_FILTER_SLOTS` on an override and
lets that message refill them.

Unlike Part A this **does** run on the scored path, so it was measured and gated in advance.

## Measured impact

**Part A — required to be byte-identical, and is.**

A full 200-session run with Part A in place reproduces `results_after_fieldfactors.json`
**byte for byte** (`diff -q`, not merely an equal TechnicalScore). Runs are deterministic
(`materialize_hidden_fields` uses a seeded RNG), so this is a proof that the free-form branch never
executes during scoring, not a "small enough" delta.

**Part B — flat, on every metric and every scenario.**

```
_results_after_fieldfactors.json → results_after_override.json_

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
```

That run is also byte-identical to `results_after_fieldfactors.json` — a **zero** delta, not a small
one. It confirms the measurement already in CLAUDE.md's demoted note: the slot drop fires in 3 of 200
sessions and is benign in all three, and the phrase and dense routes deliberately bypass the `AND`
filter anyway, so clearing it changes no ordering the public set can see. Part B is therefore
**kept as private-set insurance, not claimed as a win** — the pre-registered gate (TechnicalScore
`>= 0.912205`, `intent_override >= 0.9667`) is met exactly, with both unchanged.

No new results snapshot is committed: both runs reproduce the existing
`results_after_fieldfactors.json` byte for byte, so a duplicate file would add 1,641 lines and no
information. Reproduce with:

```bash
py -m evaluator.local_evaluator --output results_check.json
diff results_check.json results_after_fieldfactors.json    # expect no output
```

**Behaviour actually delivered**, driven through the real `AgentBridge` exactly as the browser does:

| Turn | Typed | `ask_attribute` | Message |
|---|---|---|---|
| 1 | I'm looking for a winter jacket | `other` | Here are the closest matches I found. Is there anything else... |
| 2 | something warm for commuting | `feature` | ...Are there any specific features you need? |
| 3 | make it blue | `material` | Narrowed to items matching blue. Do you have a material preference? |
| 4 | actually make it red | `color` | **Switched colour from blue to red.** Narrowed to items matching red... |
| 5 | navy would be better | `style` | **Switched colour from red to navy.** Narrowed to items matching navy... |

Five turns, five different messages, and a different ranking each turn. Before this change all five
turns returned the same message and the same list.

**A note on how the first version of this table hid a bug.** The check asserted *"no two turns
produce the same message"*. Because the question rotates every turn, that assertion passes even when
the colour never changes — the strings differ in their second half. The bug reported against the
first version (bare colour answers not taking effect) was invisible to it. The check now asserts the
thing the user actually cares about: **the colour named in the reply must be the last colour typed,
and no other colour may appear.** When a test can pass while the reported symptom is still present,
the test is measuring the wrong thing.

## Limitations & follow-ups

- **The free-form path has no measured retrieval quality.** By construction the public set never
  exercises it, so there is no number for how well it ranks. It is a demo and robustness path; do
  not quote it as a score improvement.
- **A person's prose is noisier evidence than the simulator's near-verbatim product text**, which is
  lifted from the target's own `features`/`details`. Expect free-form ranking to be worse than the
  0.912205 path. That is inherent to the input, not a defect in the branch.
- **Retirement after one answer is a heuristic.** The free-form branch retires whatever was asked as
  soon as the person answers it, because nothing else can ever retire it. Someone who answers vaguely
  still burns that attribute. Rotating is strictly better than repeating, but a "did this answer
  actually tell us anything" test would be better still.
- **The correction cues are a word list**, and so are the extended colour/material vocabularies.
  They will miss phrasings. They are cheap to extend precisely because nothing there can affect the
  score — that is the point of keeping them in a separate regex from `COLOR_RE`/`MATERIAL_RE`.
- **Turns 1–2 still disclose a single recommendation** (`DISCLOSURE_SCHEDULE`, feature 05). That is
  a deliberate scored-path trade and was left alone; `webui/agent_bridge.py:_deep_list` already
  compensates visually by showing the full 50-deep ranking with the withheld rows marked.
- **Untested follow-up:** merging the extended vocabularies into the scored path. Disclosures are
  generated from product text that does say "Navy" and "Denim", so `COLOR_RE` missing them may be
  costing real slot fills on the private set. That is a separate measured experiment — it would move
  public-set slots, so it must be run and gated, not assumed.
