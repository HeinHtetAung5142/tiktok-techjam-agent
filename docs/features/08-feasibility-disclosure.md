# 08 — Latency and token-usage disclosure

**Status:** merged
**Commit:** `1cffd0a`
**Owner:** Integration
**Tier:** 3

## What & why

`docs/submission_rules.md` requires every team to submit *"a disclosure of latency, token usage, and
estimated model cost"*, and `docs/competition_specification.md` repeats cost among the required
report topics. These are **feasibility** disclosures — they are explicitly not part of
`TechnicalScore`, which weights only HitRate@10, MRR, and Efficiency.

So this feature buys no points. It exists because a submission without it is incomplete, and because
"we used no tokens" is a claim that should be *measured and enforced* rather than asserted once in a
README and left to rot.

## Approach

**Latency is measured in-process, and deliberately kept out of the response.**

- `Agent.__init__` (`starter/agent.py:63-74`) wraps `CatalogIndex` + `Reranker` construction in a
  `perf_counter` and stores `self.construction_seconds`. This is the dominant cost and it is paid
  once per process, not per turn — reporting only a per-turn mean would hide it.
- `Agent.respond` (`starter/agent.py:102-117`) became a thin timing wrapper around a new private
  `_respond`, which holds the logic that used to live in `respond` directly. The elapsed time is
  appended in a **`finally`**, so a turn that raises is still timed. That is the point: an
  unrecorded timeout is exactly the latency worth knowing about.
- `Agent.latency_stats()` (`starter/agent.py:76-96`) summarizes turns, construction seconds, mean,
  median, p95, and max. p95 is **nearest-rank**, not interpolated — with a few hundred samples,
  interpolating invents a number between two turns that were never observed.

**The non-obvious part, and the reason latency is not simply returned to the caller:**
`turn_response` and `usage` both set `"additionalProperties": false` in
`docs/agent_api_contract.json`. An extra `latency_ms` key would therefore be *malformed output*,
which `docs/competition_specification.md:65` says may be scored as a miss. Disclosing latency inside
the response would cost HitRate. It is read out of the process afterwards instead — the constraint
is documented inline at `starter/agent.py:68-72` so nobody "helpfully" adds the field later.

**Token usage is zero by construction, and that is now enforced.** `NO_MODEL_USAGE`
(`starter/agent.py:57`) is a module constant reported as literal zeros; `respond` emits
`dict(NO_MODEL_USAGE)` per turn (`starter/agent.py:162`) so a caller cannot mutate a shared object.
Zeros are reported rather than the key being omitted, so the disclosure reads *"we used no tokens"*
rather than *"they didn't say."*

**New tool `tools/feasibility_report.py`** replays the public set, reads `agent.latency_stats()` and
the evaluator's `reported_token_usage`, and prints the two markdown tables that go in the README.
It raises `SystemExit` if token usage is ever nonzero (`tools/feasibility_report.py:75-81`) — so if
anyone later adds a model call, the "$0.00" claim fails loudly instead of going quietly stale.

## Measured impact

**Score-neutral by construction.** No retrieval, ranking, or dialog path is touched; the only change
inside the scoring loop is the `try/finally` wrapper and swapping an inline dict for
`dict(NO_MODEL_USAGE)`.

_results_after_dense.json → results.json_

| Metric | Before | After | Delta |
|---|---|---|---|
| HitRate@10 | 0.975 | 0.975 | 0 |
| MRR | 0.857304 | 0.857304 | 0 |
| MTTC | 2.895 | 2.895 | 0 |
| Efficiency | 0.8105 | 0.8105 | 0 |
| **TechnicalScore** | **0.906791** | **0.906791** | **0** |

### By scenario

| Scenario | n | Metric | Before | After | Delta |
|---|---|---|---|---|---|
| boundary | 10 | HitRate@10 | 1 | 1 | 0 |
|  |  | MRR | 0.95 | 0.95 | 0 |
|  |  | MTTC | 3.2 | 3.2 | 0 |
| browsing | 80 | HitRate@10 | 0.9875 | 0.9875 | 0 |
|  |  | MRR | 0.853765 | 0.853765 | 0 |
|  |  | MTTC | 2.7375 | 2.7375 | 0 |
| buying | 80 | HitRate@10 | 0.9625 | 0.9625 | 0 |
|  |  | MRR | 0.851771 | 0.851771 | 0 |
|  |  | MTTC | 2.6375 | 2.6375 | 0 |
| intent_override | 30 | HitRate@10 | 0.966667 | 0.966667 | 0 |
|  |  | MRR | 0.850595 | 0.850595 | 0 |
|  |  | MTTC | 3.9 | 3.9 | 0 |

**Deliberate deviation from the definition of done: no `results_after_*.json` snapshot was committed
for this feature.** The post-change run is byte-identical to `results_after_dense.json`, so a
snapshot would be a duplicate file with a different name — misleading, since it would imply an
independent measurement. `results_after_dense.json` remains the snapshot of record. Naming this here
rather than leaving a reader to notice a missing file.

### Measured latency

From the 200-session run (574 `respond()` calls). Regenerate at any time with
`py tools/feasibility_report.py`.

| Stage | Time |
|---|---|
| `Agent()` construction (FTS5 index + LSA embeddings) | **~13.5 s**, one-time at startup |
| `respond()` — mean | **~55 ms** |
| `respond()` — median | **~44 ms** |
| `respond()` — p95 | **~130 ms** |
| `respond()` — max | 240–400 ms |
| Full 200-session run, end to end | **~35 s** |

### Token usage and cost

| Item | Value |
|---|---|
| LLM / external API | **None** |
| Network access required | **None** — runs fully offline |
| API keys / environment variables | **None** |
| Estimated model cost | **$0.00** |
| Reported token usage | `0` prompt, `0` completion |

## Limitations & follow-ups

- **These timings are not deterministic, unlike the score.** They move with machine load and cache
  state. The figures above were stable to within ~3 ms on the mean across three consecutive runs,
  but the single worst-case turn ranged 240–400 ms. The score, by contrast, reproduces bit-for-bit.
  Anyone quoting these numbers should say "typical", not "exact".
- **They are single-machine numbers.** Everything was measured on the development Windows box; the
  organizer's hardware will differ, and the ~13.5 s construction cost in particular is dominated by
  indexing 50k products and will scale with their I/O.
- **Runtime is offline, but installation is not.** The disclosure's "no network access" claim is
  about *runtime*, and it is correct — there are zero network imports in `starter/`. But
  `pip install -r requirements.txt` needs the network at **install** time to fetch numpy, scipy,
  and scikit-learn. If the organizer's environment is network-isolated end to end, dependencies must
  be pre-provisioned. This deserves one honest sentence in the final report.
  *(Corrected 2026-08-30: this bullet used to cite `README.md:47` for the claim that "the agent will
  not import without them". That was wrong, and so was the README line it quoted.
  `_build_dense_index` imports `DenseIndex` inside a broad `try/except`
  (`starter/retrieval.py:227-242`), so a missing stack silently degrades to sparse-only retrieval —
  verified by running the full public set with none of the three installed: it completes and scores
  0.909858 instead of 0.912205. The risk is a quiet wrong number, not a crash, which makes
  pre-provisioning **more** important, not less.)*
- **An unrelated change rode along in this commit:** the `scipy` pin was downgraded
  `1.18.1 → 1.18.0`. It has nothing to do with the feature and should not be read as one.
- **The wheel-availability claim is asserted, not verified.** `README.md:37` advertises "Python 3.10
  or later" while the pins were only validated on 3.14.7.
- **Follow-up:** `_turn_latencies_ms` grows unbounded for the life of the process
  (`starter/agent.py:74`). Irrelevant at 574 turns; worth a cap if the harness ever runs long.
