# Feature 11 — Offline-safe fallback

## What and why

Tier 3 of the feature plan asks us to "confirm the agent degrades gracefully, or works fully,
without a live external API call", because official judging may run with network access disabled
(`docs/competition_specification.md`). Until now this was ticked off as **vacuous**: the agent makes
no network call, therefore it must be offline-safe.

That reasoning is correct and worth nothing. It is an argument, not a measurement, and it covers
only one of the two failure modes the requirement actually names:

1. **Works fully without network** — never tested. Never even *enforced*: a dependency added later
   could dial out at import time and nothing in the repo would notice.
2. **Degrades gracefully** — outright false. `respond()` had no exception guard at all
   (recorded as known gap 4 in `CLAUDE.md`), so a fault anywhere outside the three inner
   fail-soft layers escaped it, and a raised exception is scored as a miss
   (`evaluator/local_evaluator.py:239-244`). `respond("s", None, 1, 10)` raised `TypeError`.
   `respond()` before `reset()` raised `RuntimeError` by design. An `Agent()` built on a Python
   without FTS5 raised in the **constructor**, which the evaluator calls once outside its per-turn
   `try` — taking all 200 sessions down rather than one turn.

So this feature turns an assertion into a guarantee with a test behind it. It buys no points and is
not meant to: it is insurance against a judging environment stricter or stranger than ours.

## Approach

### 1. `starter/offline.py` — the response contract, enforced

One module owns the shape of a `turn_response`, used by both the happy path and the fallback path so
the two cannot drift. Input coercions (`coerce_turn`, `coerce_top_k`, `coerce_user_message`,
`coerce_session_id`) absorb hostile arguments before they reach `DialogState`; output coercions
(`coerce_attribute`, `coerce_recommendations`, `coerce_usage`) guarantee every field satisfies
`docs/agent_api_contract.json` — enum-valid `ask_attribute`, deduplicated non-empty `parent_asin`
strings, no keys the schema's `additionalProperties: false` would reject.

On well-formed input every one of these is the identity function. That is the property the test
suite pins down, and the reason the score cannot move.

### 2. `Agent.respond` never raises

A broad `except Exception` around `_respond`, plus a `_fallback_response` that degrades in three
steps rather than one:

| Step | Source | Reached when |
|---|---|---|
| 1 | this session's last good recommendations | any turn after the first has succeeded |
| 2 | the catalog-wide fallback slate (10 most-reviewed products) | the first turn already failed |
| 3 | an empty list | retrieval *and* the catalog file are both unreadable |

Only step 3 is unscoreable, and reaching it takes two independent failures. The slate is collected
during the existing index build pass (`heapq.nlargest` over `rating_number`, measured at **129 ms**
over all 50k rows) and read straight from the catalog file when the index itself could not be built
— deliberately not through sqlite, since sqlite is what just failed.

`Agent.__init__` is guarded for the same reason, and `respond()` now auto-creates a session instead
of raising when `reset()` was skipped. An empty recommendation list also routes to the fallback:
retrieval legitimately returns nothing for an empty query (`retrieval.py:376`), but an empty page is
an unscoreable turn either way, so it is spent on the slate instead.

### 3. `tools/offline_guard.py` — enforcement, not assertion

Installing the guard makes the process incapable of network access, via two independent layers:

- a **`sys.addaudithook`** hook rejecting `socket.connect`, `socket.getaddrinfo`,
  `socket.gethostbyname`, `socket.sendto`, `urllib.Request`, and the ftplib/smtplib connect events.
  This fires at the CPython level, so it catches a C extension dialling out and cannot be dodged by
  aliasing the import — verified against a `socket` reference captured *before* the guard was
  installed;
- **monkeypatched entry points**, so socket *construction* is refused too and the error names what
  was attempted.

One trap worth recording: blocking `socket.socket` with a plain function **breaks `ssl.py`**, which
does `class SSLSocket(socket)` at import time. That takes down asyncio → joblib → scikit-learn, and
so the entire dense route, with an unrelated `TypeError`. The block has to be a *subclass* whose
`__init__` refuses. The first offline run was made with the naive version and scored **0.909858**
with 12 sessions differing — the dense index had silently failed to build. The check caught it,
which is the best evidence available that it is a real check and not a rubber stamp.

The agent never imports any of this. Enforcement belongs to the harness; shipping a process-wide
audit hook inside a module the organizer imports would be hostile.

### 4. Two ways to run it

```bash
py -m unittest discover -s tests -t . -v   # 26 tests, ~5 s
py tools/offline_check.py                  # full 200-session replay, network blocked
```

The check takes about two minutes, so it reports each stage and a session counter with an ETA
(rewritten in place on a terminal, printed every 20 sessions when piped). The counter is driven by
`reset()`, which the evaluator calls once per session — the only per-session hook available without
touching `evaluator/`, which the rules forbid. `ProgressAgent` overrides nothing else: it is the
object whose score the tool exists to verify.

`offline_check.py` installs the guard *before* importing `starter` — so numpy, scipy and
scikit-learn are all loaded under the block — then replays the public set through the organizer's own
evaluator and compares against `results_after_fieldfactors.json` **session by session**, not just in
aggregate. It exits non-zero unless the scores match to six decimals, all 200 sessions have an
identical hit turn and rank, the agent did not construct in degraded mode, and no turn was answered
from the fallback path.

## Results

The full public set, replayed with every socket operation in the process blocked:

```text
| Metric                      | Reference | Offline run |    |
|-----------------------------|-----------|-------------|----|
| hit_rate_at_10              | 0.98      | 0.98        | ok |
| mrr                         | 0.864018  | 0.864018    | ok |
| mttc                        | 2.85      | 2.85        | ok |
| efficiency                  | 0.815     | 0.815       | ok |
| recommended_technical_score | 0.912205  | 0.912205    | ok |

sessions compared: 200, differing: 0
degraded: False, fallback turns: 0
```

| Metric | Before | After | Delta |
|---|---|---|---|
| HitRate@10 | 0.98 | 0.98 | 0 |
| MRR | 0.864018 | 0.864018 | 0 |
| MTTC | 2.85 | 2.85 | 0 |
| Efficiency | 0.815 | 0.815 | 0 |
| **TechnicalScore** | **0.912205** | **0.912205** | **0** |

### By scenario

| Scenario | n | HitRate@10 | MRR | MTTC |
|---|---|---|---|---|
| boundary | 10 | 1.0 → 1.0 (0) | 1.0 → 1.0 (0) | 3.2 → 3.2 (0) |
| browsing | 80 | 0.9875 → 0.9875 (0) | 0.853001 → 0.853001 (0) | 2.725 → 2.725 (0) |
| buying | 80 | 0.975 → 0.975 (0) | 0.852133 → 0.852133 (0) | 2.525 → 2.525 (0) |
| intent_override | 30 | 0.966667 → 0.966667 (0) | 0.879762 → 0.879762 (0) | 3.933333 → 3.933333 (0) |

**Zero is the intended result, and it is a stronger claim than the table shows.** The comparison is
not "the aggregate matched" — all 200 sessions match `results_after_fieldfactors.json` on `hit`,
`first_hit_turn` and `best_rank` individually, in both the normal run and the network-blocked run.
`fallback_turns` is **0** across all 566 `respond()` calls, so none of the new machinery ran on the
public set: every coercion was the identity function and the slate was never reached.

One real bug was found and fixed along the way, by the test suite rather than by the evaluator:
`coerce_recommendations` tested its length cap *after* appending, so `top_k=0` returned one item.
The agent never hit it (`disclosure_limit` had already truncated the list upstream), which is
precisely why a unit test found it and a 200-session run would not have.

## Known limitations

- **`fallback_turns == 0` means the fallback is untested by the public set, by construction.** Its
  correctness rests on `tests/test_offline_safety.py`, which reaches it by injecting failures
  (a missing catalog, an index removed mid-session). That is the right trade — a fallback that fires
  during a scored run is a bug — but it does mean the guarantee is only as good as the injected
  failure modes, and real environments are more inventive than we are.
- **The guard proves *this* process made no network call; it cannot prove the code never would.**
  A dependency that dials out only on a code path the public set does not exercise would pass.
  `tests/test_offline_safety.py::NoNetworkImportTests` closes part of that gap statically, by AST
  scanning `starter/*.py` for any networking import, but "no direct import" is weaker than "cannot".
- **The audit hook cannot be uninstalled** once added — a CPython constraint. Every caller is
  therefore a dedicated process (the test runner, `offline_check.py`), and the guard must never be
  imported by agent code.
- **Broad `except Exception` can mask a real regression**, which is why `fallback_turns` is counted
  and surfaced through `latency_stats()`, asserted to be 0 by `offline_check.py`, and why every
  genuine fault still prints to stderr. A silent safety net would be worse than none.
- **The three inner fail-soft layers are now redundant** (reranker, dense route, phrase routes each
  have their own `try`). They are kept deliberately: they degrade *one route* and keep the turn's
  real retrieval, where the outer net degrades the whole turn. Redundancy in the right direction.
