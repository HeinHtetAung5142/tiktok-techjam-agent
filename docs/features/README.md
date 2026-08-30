# Feature log

One file per feature, numbered in build order: `NN-<kebab-case-name>.md`.

These notes are not ceremony — the competition requires a final report covering *architecture,
model choice, cost, and limitations*, and a submission whose results can be reproduced. If each
feature is written up as it lands, that report is assembly rather than a panicked rewrite on the
last morning.

## Definition of done

A feature is not done until the evaluator has been re-run and the score movement written down.

```bash
py -m evaluator.local_evaluator --output results_after_<milestone>.json
py tools/score_delta.py <previous>.json results_after_<milestone>.json
```

Paste the generated table into your feature doc, then commit the code and the results snapshot
together.

## Reading the numbers honestly

- The public set is **200 sessions**, so one session moves HitRate@10 by **0.005**. A
  TechnicalScore delta below **~0.01** is noise. `score_delta.py` flags this for you — don't
  overrule it because the arrow is green.
- **Always include the per-scenario table.** The aggregate hides inversions: a change can lift
  buying while flattening browsing, and the four scenarios are weighted 40/40/15/5 in both the
  public and private sets.
- **HitRate and MRR can move in opposite directions.** Widening the candidate pool finds more
  targets but often lands them at rank 8–10, which raises HitRate (50% of score) while lowering MRR
  (30%). This already happened once — see `02-multi-route-retrieval.md`. Say so when it does.
- **Runs are deterministic.** Identical code scores identically, so any change in the number is
  caused by your change, not by sampling.
- **Record flat and negative results.** A regression written up in two minutes stops a teammate
  re-attempting the same idea on the final day. There is no penalty for a documented dead end.

## Template

```markdown
# NN — Feature name

**Status:** merged | reverted | experimental
**Commit:** <sha>
**Owner:** <role>
**Tier:** <0–3>

## What & why
One paragraph: the problem, and which scoring term it targets.

## Approach
How it works. Name the functions/files touched. Note anything a reviewer would find surprising.

## Measured impact
<paste `py tools/score_delta.py` output here>

## Limitations & follow-ups
What it does not fix, what it made worse, and what should come next.
```

## Index

| # | Feature | Tier | TechnicalScore after |
|---|---|---|---|
| — | _starter baseline_ | — | 0.10671 |
| 01 | [Dual-track intent routing](01-dual-track-intent-routing.md) | 1 | 0.110829 |
| 02 | [Multi-route retrieval pipeline](02-multi-route-retrieval.md) | 1 | 0.124334 |
| 03 | [Clarification loop and cross-turn evidence](03-clarification-loop.md) | 1 | 0.681542 |
| 04 | [Semantic reranking](04-semantic-reranking.md) | 2 | 0.84752 |
| 05 | [Rank-vs-turn arbitrage](05-rank-vs-turn-arbitrage.md) | 2 | 0.898866 |
| 06 | [Phrase retrieval + constraint bugs](06-phrase-retrieval.md) | 2 | **0.907281** |
| 07 | [Hybrid/dense retrieval](07-hybrid-dense-retrieval.md) | 2 | 0.906791 (flat) |
| 08 | [Latency and token-usage disclosure](08-feasibility-disclosure.md) | 3 | 0.906791 (unchanged) |
| 09 | [Optimization headroom](09-optimization-headroom.md) | 2 | 0.906791 (investigation, no code change) |
| 10 | [Field-factor calibration](10-field-factor-calibration.md) | 2 | **0.912205** |
| 11 | [Free-form input robustness](11-freeform-input-robustness.md) | 3 | 0.912205 (byte-identical) |
| 12 | [Intent override, properly](12-intent-override.md) | 2 | 0.912205 (byte-identical) |
| 13 | [Optional language model](13-optional-llm.md) | 3 | 0.912205 (byte-identical; off by default) |
| 14 | _model circuit breaker_ — no doc file; written up in CLAUDE.md's Model policy | 3 | 0.912205 (byte-identical) |
| 15 | [Free-form negation and slot-aware questioning](15-freeform-negation.md) | 3 | 0.912205 (byte-identical) |
