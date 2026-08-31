# 09 — Optimization headroom: where the remaining points are

**Status:** investigation — **no agent code changed by this doc**
**Commit:** (this one)
**Owner:** Coordination + Evaluation
**Tier:** 2 (MRR)

> **Every number below is measured against the pre-feature-10 configuration (0.906791).** Feature 10
> then shipped the one improvement this survey found, moving the score of record to **0.912205** and
> the miss count from five to four. The tables here are kept exactly as recorded rather than
> restated, so they remain a faithful account of what was measured when.

## What & why

The question asked was simple: *is there anything left to optimize, or is the agent finished?*
Features 05–07 all assert a plateau, and CLAUDE.md repeats it, but the assertion had accumulated
into folklore — "every cheap term is spent" was true when written and nobody had re-checked it
against the pipeline as it now stands.

This is the re-check. It produces one **proof** that closes most of the search space, confirms the
constants are still at their optimum, kills one suspected bug, and leaves exactly one idea standing.

## The finding that matters: the customer knows at most 4 things

`intent_card` (`evaluator/local_evaluator.py:69-71`) builds the customer's entire knowledge as:

```python
"hard_constraints": cleaned[:2],
"soft_preferences": cleaned[2:4] or cleaned[:1],
```

At most **4 constraints**, total, for the whole session. `customer_reply` returns at most two
undisclosed ones per turn (`evaluator/local_evaluator.py:177`). Since `other` matches any
constraint, two `other` questions drain the customer completely:

| Turn | Evidence in hand |
|---|---|
| 1 | the opening message (category) |
| 2 | + 2 constraints |
| 3 | + the remaining 2 — **everything obtainable, ever** |
| 4+ | *"I don't have an additional preference"* — nothing new arrives |

**This converts feature 05's measured plateau into a structural proof.** MRR plateaus at ~0.858 not
because the sweep happened to stop improving, but because after turn 3 there is no more information
in the universe to condition on. It also explains why no session runs past turn 4.

### What this rules out permanently

- **Deferring further.** Buys zero new evidence, costs MTTC. Not "measured as flat" — provably
  worthless.
- **A better `ASK_ORDER` or question policy.** Cannot extract a 5th constraint that does not exist.
  The current `"other"`-first policy already reaches the ceiling in the minimum number of turns.
- **Personalization.** Already measured degenerate (see Known gaps); this closes it from the other
  side too — even a perfect profile adds nothing the 4 constraints don't already say.

**Given fixed, complete evidence by turn 3, the only remaining lever in the entire system is
ranking quality**: ordering the same candidates better.

## Where the points are

| Pool | Worth in TechnicalScore |
|---|---|
| 34 hits at rank 2–8 → rank 1 | **+0.0353** |
| 5 misses → found | +0.0125 (HitRate) + 0.0075 (MRR) |

Realistic ceiling with the current miss set: **~0.942**, against 0.906791 today. The rank-2-to-8
pool is worth nearly **3x** the entire miss pool, and the misses are separately established as
information-theoretically unreachable. Chase rank, not recall.

### All 34 lag at exactly the same place

Turn × rank for the 195 hits:

| turn | r1 | r2 | r3 | r4 | r5 | r6 | r7 | r8 |
|---|---|---|---|---|---|---|---|---|
| 1 | 9 | | | | | | | |
| 2 | 75 | | | | | | | |
| 3 | 60 | 10 | 2 | 7 | | | | |
| 4 | 17 | 2 | | | 4 | 2 | 2 | 5 |

**Every non-rank-1 hit occurs on turn 3 or 4 — precisely where `DISCLOSURE_SCHEDULE` widens
1 → 4 → 8.** Turns 1–2 disclose a single slot and are 84/84 at rank 1. The gate is what converts
"target sits at rank 4" into a scored rank-4 hit. That is the arbitrage working as designed — but
it means the residual is entirely *"the reranker put 1–7 wrong products first"*, not *"we ran out
of turns"*.

### The organizer's difficulty labels are uninformative

`difficulty_bucket` in `data/public_set.jsonl` has no relationship to our outcome:

| difficulty | n | rank-1 share | MRR |
|---|---|---|---|
| easy | 80 | 81% | 0.8518 |
| medium | 90 | 80% | 0.8645 |
| hard | 30 | 80% | 0.8506 |

Our failures are not the organizer's hard cases. They are distributed at random with respect to
difficulty, which points at **catalog ambiguity** — near-identical products sharing the same
boilerplate — rather than hard sessions. Do not build a difficulty-conditioned strategy; there is
nothing there.

## Killed: the query-term truncation hypothesis

`agent.py:131` does `terms(state.evidence_text())[:64]`. `evidence_text()` is oldest-first and
`terms()` preserves first-seen order, so a binding cap would discard the *newest* disclosures — the
specific ones just elicited — while keeping generic turn-1 category words. That would have been a
real bug, biting exactly at turns 3–4 where the 34 live.

**Measured: 0 of 574 `respond()` calls exceed the cap.** It is dead code on this dataset. Score
reproduced at 0.906791 with the probe attached.

Worth noting for the private set: it stays dead only while messages are short. The spec permits
added paraphrasing and the private set is 4x larger. Flipping the slice to keep the newest terms is
**provably free** on the public set and removes the failure mode entirely — cheap insurance, not a
fix for a live bug.

## Constants re-swept against the current pipeline

Every constant was fitted against an *earlier* stack: rerank weights and pool in feature 04, the
disclosure schedule in feature 05 — both **before** phrase routes (`26d4b21`) and dense
(`1d41dee`). Feature 06's nineteen variants covered phrase parameters only. Nothing had been
re-swept since, so the old argmax was not known to still be the argmax.

Re-run with `tools/sweep_constants.py` (new in this commit — builds one `Agent` and reuses it, so a
30-variant sweep is minutes not hours, and aborts if the control arm does not reproduce 0.906791).

**A. coverage / phrase split** — flat, every variant inside the noise floor. The shipped 50/50 is
as good as anything, and the even split is the honest hedge against paraphrasing.

| Variant | Score | Delta | HitRate | MRR |
|---|---|---|---|---|
| control 0.5 / 0.5 | 0.906791 | — | 0.9750 | 0.857304 |
| 0.3 / 0.7 | 0.906991 | +0.0002 | 0.9750 | 0.857304 |
| 0.4 / 0.6 | 0.906991 | +0.0002 | 0.9750 | 0.857304 |
| 0.6 / 0.4 | 0.907085 | +0.0003 | 0.9750 | 0.859615 |
| 0.7 / 0.3 | 0.906985 | +0.0002 | 0.9750 | 0.859615 |

**B. rerank pool** — `RERANK_POOL=120` is a genuine optimum, and the only axis where a variant left
the noise floor. Shrinking the pool **loses targets outright**: at 60 the HitRate falls 0.975 →
0.960. The pool is load-bearing, not an arbitrary round number.

| Variant | Score | Delta | HitRate | MRR |
|---|---|---|---|---|
| 60 | 0.896077 | **−0.0107** | 0.9600 | 0.853589 |
| 90 | 0.903666 | −0.0031 | 0.9700 | 0.856887 |
| **120 (shipped)** | **0.906791** | — | **0.9750** | **0.857304** |
| 160 | 0.906533 | −0.0003 | 0.9750 | 0.857109 |
| 200 | 0.903183 | −0.0036 | 0.9700 | 0.856609 |
| 300 | 0.903056 | −0.0037 | 0.9700 | 0.856520 |

**C. disclosure schedule** — the shipped schedule is a real local optimum **in both directions**, and
this axis produced the second of the sweep's only two outside-noise results.

| Variant | Score | Delta | MRR | MTTC |
|---|---|---|---|---|
| (1, 1, 5, 10) | 0.907191 | +0.0004 | 0.857304 | 2.875 |
| **(1, 1, 4, 8, 10) shipped** | **0.906791** | — | 0.857304 | 2.895 |
| (1, 1, 3, 6, 10) | 0.906516 | −0.0003 | 0.861054 | 2.965 |
| (1, 1, 6, 10) | 0.906241 | −0.0006 | 0.853137 | 2.860 |
| (1, 1, 2, 5, 10) | 0.906116 | −0.0007 | 0.861054 | 2.985 |
| (1, 1, 1, 4, 8, 10) | 0.905466 | −0.0013 | 0.863554 | 3.055 |
| (1, **2**, 4, 8, 10) | 0.895191 | **−0.0116** | 0.812304 | 2.800 |

Two things fall out of this:

- **Deferring more is the proof made visible.** Extra turns re-present the *same* four constraints,
  so MRR creeps up (0.8573 → 0.8636 as the gate tightens) while MTTC rises monotonically and
  overtakes it. Every tighter variant is net negative. This independently reproduces feature 05's
  conclusion against the *current* retrieval stack, which is what the re-sweep was for.
- **Deferring less is far worse, and that is the load-bearing half.** Opening turn 2 from one slot
  to two costs **−0.0116** and collapses MRR 0.857 → 0.812. 75 sessions hit on turn 2; letting a
  second slot through converts rank-1 hits into rank-2 hits wholesale. The tight `(1, 1, …)` opening
  is doing the real work in feature 05, not the widening that follows it.

`(1, 1, 5, 10)` nominally edges the shipped schedule by +0.0004 — forty times below the noise floor,
and it is the same MRR with a marginally better MTTC. Not a win; do not "upgrade" to it.

**D. route weights** — all flat, with one consistent shape: **over-weighting any secondary route
costs HitRate.** Pushing category to 0.6, dense to 0.5, or the phrase routes to 1.0 each drops
0.975 → 0.970 and loses ~0.0031. That is the same signature feature 06 found when it tested
`phrase weight=2.0` — a dominant secondary route displaces candidates fusion already had right.
The shipped weights sit below that cliff on every axis.

| Variant | Score | Delta | HitRate | MRR |
|---|---|---|---|---|
| dense=0.0 (route off) | 0.907281 | +0.0005 | 0.9750 | 0.857935 |
| category=0.45 | 0.907022 | +0.0002 | 0.9750 | 0.857740 |
| phrase_route=0.75 | 0.906966 | +0.0002 | 0.9750 | 0.857554 |
| **shipped (cat 0.3 / dense 0.3 / phrase 0.5)** | **0.906791** | — | **0.9750** | 0.857304 |
| category=0.15 | 0.906770 | −0.0000 | 0.9750 | 0.857234 |
| phrase_route=0.35 | 0.906670 | −0.0001 | 0.9750 | 0.857234 |
| dense=0.15 | 0.906366 | −0.0004 | 0.9750 | 0.855220 |
| category=0.6 | 0.903670 | −0.0031 | 0.9700 | 0.857234 |
| phrase_route=1.0 | 0.903679 | −0.0031 | 0.9700 | 0.856929 |
| dense=0.5 | 0.903374 | −0.0034 | 0.9700 | 0.856579 |

**Note the top row.** Turning the dense route off scores 0.907281 — which is exactly
`results/results_after_phrase.json`, the repo's high-water mark. This independently reproduces feature 07's
own measurement: the dense route costs **−0.00049** on the public set and was shipped anyway, as
paraphrase insurance for the private set that 200 public sessions cannot measure. That trade is
still the right call, but it is now confirmed from a second direction rather than resting on one
run. Do not "discover" this later and switch it off on public-set evidence alone — read feature 07
first.

**E. phrase route reach** — flat, and it exposes a **third dead constant**. `MAX_PHRASE_ROUTES` at 6
and at 20 both score *exactly* the control, so no session ever generates more than six phrase
routes: the shipped cap of 12 never binds. `PHRASE_DF_MAX` at 4000 and 8000 are likewise
bit-identical to the shipped 2000, confirming feature 06's "threshold, not a gradient" finding from
above the step rather than below it. Only 1000 differs (−0.0001), consistent with feature 06's
observed step at df=933.

| Variant | Score | Delta | HitRate |
|---|---|---|---|
| MAX_PHRASE_ROUTES=6 | 0.906791 | ±0 | 0.9750 |
| MAX_PHRASE_ROUTES=20 | 0.906791 | ±0 | 0.9750 |
| PHRASE_DF_MAX=1000 | 0.906670 | −0.0001 | 0.9750 |
| PHRASE_DF_MAX=4000 | 0.906791 | ±0 | 0.9750 |
| PHRASE_DF_MAX=8000 | 0.906791 | ±0 | 0.9750 |

**F. field factors — the one axis that found a real improvement. See feature 10.**

| Variant | Score | Delta | HitRate | MRR | MTTC |
|---|---|---|---|---|---|
| **features/details = 1.0** | **0.912205** | **+0.0054** | **0.9800** | **0.864018** | **2.850** |
| lighter tail (desc 0.5, store 0.6) | 0.907016 | +0.0002 | 0.9750 | 0.857720 | 2.890 |
| **shipped** | 0.906791 | — | 0.9750 | 0.857304 | 2.895 |
| steeper tail (desc 0.4, store 0.5) | 0.905870 | −0.0009 | 0.9750 | 0.853901 | 2.890 |
| flat 1.0 everywhere | 0.895829 | **−0.0110** | 0.9650 | 0.844095 | 2.995 |

Raising `features` and `details` from 0.85 to 1.0 improves **all three metrics simultaneously** —
one more session found, better rank, fewer turns. And `flat 1.0 everywhere` losing 0.011 shows this
is not "raise everything": the field *discrimination* still matters, it was just calibrated wrong on
the two fields that carry the evidence. Written up and shipped as feature 10.

**G. partial phrase credit** — flat. 0.25 costs −0.0005; 0.75 and 1.0 gain +0.0001. The shipped 0.5
is fine and nothing here is distinguishable from it.

## Still untested: the coverage precision term

The field-factor win above came out of the sweep. This one did not — it is a structural change to
the scoring function that nothing has measured yet, and it remains the best remaining idea after
feature 10 ships.

**`_coverage` measures recall with no length normalization** (`starter/ranking.py`):

```python
found += weight * factor
return found / total_mass       # total_mass is constant across candidates
```

It asks *how much of the customer's IDF mass is in this product* and never the converse — *how much
of this product is the customer's evidence*. So a sprawling listing containing "100% Cotton" and
"Button closure" among forty other features scores identically to a focused listing where those two
things are the whole product.

That is precisely the failure shape of the 34: the disclosures are near-verbatim target text, so
the target always matches — it just fails to *out*-match verbose competitors carrying the same
manufacturer boilerplate. It also fits the difficulty-independence above, which points at catalog
ambiguity rather than hard sessions.

Adding a precision term, or normalizing by document length, is principled and appears nowhere in
features 04–07. **Untested.** Honest expectation: moves a handful of the 34, not all of them —
+0.005 to +0.015 if it works, flat if it doesn't. It is the only remaining idea with a real
mechanism behind it rather than a hyperparameter nudge.

## Limitations & follow-ups

- **No code was changed in this doc and no score moved by it.** This is an investigation. The one
  improvement it found — field factors — is implemented and measured separately in
  `10-field-factor-calibration.md`, so that the change carries its own validation run and snapshot
  rather than hiding inside a survey.
- **32 variants across 7 axes, and exactly one beat the shipped configuration.** The other three
  outside-noise results (`RERANK_POOL=60`, the turn-2 gate, `flat 1.0` field factors) are all
  confirmations that a shipped value is load-bearing. That ratio is the real headline: the
  configuration was already good, and re-sweeping is cheap insurance rather than a rich seam.
- **The sweep is coordinate descent, not a joint search.** Interactions between axes are untested.
  Given every axis except pool size is flat, an interaction large enough to matter is unlikely, but
  it is not ruled out.
- **Everything here is fitted to 200 public sessions.** The evidence-ceiling proof is structural and
  transfers; the constants are fitted numbers and the private set is 4x larger.
- **Two pieces of free insurance are still unbanked**, both documented in Known gaps: flipping the
  query-term slice to keep the newest terms, and the broad exception guard on `respond()`. Neither
  moves the public score; both remove a private-set failure mode.
