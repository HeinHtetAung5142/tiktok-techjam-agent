# 14 — Model circuit breaker

**Status:** merged
**Commit:** `f506810` (`fix llm implementation, added benchmarking and integration with webui`)
**Owner:** Integration
**Tier:** 3

> Written up retrospectively during submission packaging. Feature 13 landed the optional
> model and its per-call fail-soft; the breaker shipped alongside it and was documented in
> `CLAUDE.md`'s Model policy section rather than here, leaving a 13 → 15 gap in the log.
> Closing it, because "every feature carries a measured delta" is a claim we make in the
> report and it should be true of every number in the index.

## What & why

Feature 13 made every model call fail soft: a timeout, HTTP error, bad JSON, or dead
network returns `None` and the agent falls through to the offline pipeline that scores
0.912205 on its own. That is correct per call and **insufficient across a session.**

With the network down, per-call fail-soft still pays the full `TIMEOUT_SECONDS` (6 s) on
*every* turn before falling through. Ten turns of a dead endpoint is a minute of dead air
for an outcome already known after the first two. Nothing is lost from the score — official
judging runs with the model off — but the *feasibility* disclosure this competition asks for
covers latency, and "our optional route adds a minute per session when the network is out"
is a real answer to a question a judge can ask. It also matters in the demo UI, where the
operator is watching.

This targets no scoring term. It is a Tier 3 robustness and disclosure item.

## Approach

`starter/llm.py` latches the client off for the rest of the process once it is clearly
unusable. `complete()` then returns the same `None` a failure would have produced —
immediately, with no socket and no wait — so every caller's existing fallback handles it
unchanged. There is no new failure path to test, only a faster one.

"Unusable" has three shapes, and each gets its own threshold:

```python
BREAKER_FAILURES = 3          # consecutive failures of any kind (HTTP 500s, bad JSON)
BREAKER_NETWORK_FAILURES = 2  # consecutive *connection* failures -- no route, DNS dead
BREAKER_SLOW_CALLS = 3        # consecutive successes that were too slow to be worth it
BREAKER_SLOW_MS = 4500.0      # what "too slow" means, against a 6000 ms timeout
```

The distinction a reviewer should notice is **connection failure versus service failure**.
`_is_network_error` classifies them, and `urllib.error.HTTPError` is deliberately excluded
even though it subclasses `URLError`: an HTTP error means we *reached* the service and it
answered, so retrying is merely unlucky rather than hopeless. Connection errors trip at 2,
service errors at 3.

The third trip condition is the non-obvious one. A model that answers correctly but takes
5 s per turn has not failed by any per-call test, yet it costs more than it returns —
`expand` mode contributes one retrieval route at weight 0.25. Three consecutive slow
successes latch it off just as a failure would.

Two deliberate omissions:

- **No automatic half-open retry.** The standard breaker pattern probes periodically to see
  whether the service recovered. Here that would put the timeout back onto the very turn the
  breaker exists to protect, so recovery is manual only: `reenable()` exists for the WebUI's
  retry button, and nothing calls it automatically.
- **`stats()` reports `enabled: True, disabled: True`** for a tripped client, rather than
  collapsing to "off". "A client is configured" and "it is currently being called" are
  different facts, and the second one is what an operator is trying to find out.

## Measured impact

**Byte-identical to `results/results_after_fieldfactors.json`** — TechnicalScore 0.912205,
sessions array included. This is true by construction rather than by luck: the default
configuration builds no client at all (`Agent.__init__` requires both
`SHOPPING_COPILOT_API_KEY` and `SHOPPING_COPILOT_LLM`), so there is no object for the
breaker to live on during a scored run.

| Metric | Before (`results/results_after_fieldfactors.json`) | After |
|---|---|---|
| HitRate@10 | 0.98 | 0.98 |
| MRR | 0.864018 | 0.864018 |
| MTTC | 2.85 | 2.85 |
| **TechnicalScore** | **0.912205** | **0.912205** |

Per scenario, all four unchanged: boundary 1.0 · browsing 0.9875 · buying 0.975 ·
intent_override 0.9667.

The measurement that *is* interesting is the offline one. A full 200-session run with
`expand` configured **and every socket raising** is also byte-identical — the fallback was
measured, not asserted. Latency behaviour under that run is the point of the feature: the
breaker trips on the second turn and every later turn returns immediately.

`tools/verify_llm.py` covers the breaker directly (the `BREAKER` block, ~9 checks): that
connection failures trip it sooner than HTTP errors, that an open breaker makes **no**
further network call, that it still returns `None` so every caller's fallback runs, that
slow-but-successful calls trip it, that `reenable()` closes it, and that a healthy client is
never disabled and says so in `stats()`.

## Limitations & follow-ups

- **The thresholds are reasoned, not fitted.** 2/3/3 and 4.5 s against a 6 s timeout are
  defensible but no sweep was run over them, because the axis they would be fitted on
  (`expand` mode against a live endpoint) does not enter the score.
- **Trip state is per process, not per client-swap.** `Agent.configure_llm` builds a fresh
  client, so changing the key or model in the WebUI implicitly resets the breaker. That is
  the behaviour an operator wants; it is worth knowing it happens.
- **Never exercised against a genuinely slow live model in a scored context.** The slow-call
  path was verified by driving `BREAKER_SLOW_MS` to 0 in the test rather than by waiting on
  a real 5-second endpoint.
- Related: `13-optional-llm.md` for the client itself, the mode table, and the reasoning
  behind keeping it off by default.
