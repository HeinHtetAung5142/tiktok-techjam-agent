# 13 — Optional language model (off by default)

**Status:** merged, off by default
**Commits:** `d3dd020`, (this one)
**Owner:** Integration
**Tier:** 3 (feasibility / product) — deliberately score-neutral in the judged configuration

## What & why

The model policy said "local and offline **for now** … a hosted model may be added later …
but only behind a fallback that still runs with network access disabled." This is that
addition, wired so it can be demonstrated without ever putting the 0.912205 score of record
at risk, and then made actually *operable* — configurable, comparable, and survivable when
the endpoint dies.

The requirement was explicit: **the feature only counts if the score and accuracy do not go
down.** That rules out any design where a model sits on the scored path and we argue
afterwards that it probably helped. So the model is opt-in, and the judged configuration is
a provable no-op.

### On "fall back to the local LLM"

There is no local LLM in this project and there should not be one — `requirements.txt` pins
`numpy`/`scipy`/`scikit-learn` and nothing else, and model weights are out of scope per
`CLAUDE.md`. The fallback target is the **offline retrieval pipeline** (FTS5 + LSA +
reranker), which scores 0.912205 entirely on its own. That is what "falls back" means
everywhere below. (A local Ollama *can* be pointed at, as a provider — see
`docs/LLM_SETUP.md` — but it is a configuration, not a bundled dependency.)

No new dependency either way. The client is `urllib.request` from the standard library, so
`requirements.txt` is untouched and `pip install -r requirements.txt` still reproduces the
run exactly. `numpy`/`scipy`/`scikit-learn` remain the only third-party pins.

## Provider and model choice

This changed twice, and the history is the useful part.

### First choice: SiliconFlow — abandoned, not deprecated

`Qwen/Qwen3-8B` on SiliconFlow's OpenAI-compatible endpoint.

| Candidate | Free? | Why / why not |
|---|---|---|
| **`Qwen/Qwen3-8B`** | permanently free | Chosen at the time. 128K context, instruction-tuned, reliable at terse JSON, and accepts `enable_thinking: false` so we don't pay latency for a reasoning trace we discard. |
| `deepseek-ai/DeepSeek-R1-Distill-Qwen-7B` | permanently free | Rejected. A reasoning distill: emits long `<think>` blocks, so higher latency and tokens for the short structured extraction we actually want per turn. |
| `deepseek-ai/DeepSeek-OCR` | free, rate-limited | Not a chat model. Irrelevant — this track is text-only. |

Its free tier (1,000 RPM / 50K TPM, no credit card) requires **real-name verification, which
assumes mainland-Chinese documents**; international accounts must contact support. No key was
ever obtainable, which is why every enabled-path number stayed stubbed for as long as it did.
SiliconFlow remains one environment variable away and is still covered by a test — see
*Approach*.

### Second choice: OpenRouter — and the model picked by measurement

The client was always plain OpenAI-compatible chat completions, so nothing had to change to
move providers; only the defaults and the documentation did. OpenRouter needs no identity
verification and has free model slugs.

The model was then chosen by running `py tools/benchmark_llms.py` against every free slug
that would answer, **twice, with identical results**:

| Model | parse / slots / price / terms | mean |
|---|---|---|
| **`inclusionai/ling-3.0-flash-fin:free`** | **100 / 100 / 100 / 100** | **~1.5 s** |
| `nvidia/nemotron-3-super-120b-a12b:free` | 80 / 100 / 100 / 50 | 4–11 s, unstable |
| `google/gemma-4-26b-a4b-it:free` | rate-limited upstream, no reading | — |
| `nvidia/nemotron-3.5-lightning:free` | 0 / 0 / 0 / 0 | ~8.7 s |
| `liquid/lfm-2.5-2.6b:free`, `minimax/minimax-m2.7:free` | 20 / 0 / 0 / 0 | — |

This is the benchmark paying for itself. An interim default (`google/gemma-4-26b-a4b-it:free`)
had been picked by *reading model cards* — instruction-tuned, few active parameters, advertises
the `temperature`/`top_p` the client always sends — and turned out to be unavailable in
practice. The measured winner is a **finance**-tuned variant nobody would have guessed.

That `-fin` suffix is recorded as a caveat rather than hidden: it looks wrong for a clothing
catalog and is the first thing to re-measure if quality ever looks off. It simply beat every
general-purpose free slug on our own probes, twice, and the probes are the job.

`openrouter/free` was rejected regardless of score — it selects a free model **at random per
call**, so the feasibility disclosure could not name a model and `expand` would be even less
reproducible than it already is.

**Two operational facts, learned the hard way** (both now in `docs/LLM_SETUP.md`):
OpenRouter's free tier is **50 requests per day** shared across all models — one five-model
benchmark run spends about half of it, and exhaustion returns 429 on *everything*, which
looks exactly like a bad key — and free pools are rate-limited **upstream** per model, on top
of your own quota. That upstream limit is what took both Google slugs out of the comparison.

## Approach

### The client — `starter/llm.py`

Configuration is environment-only; no key ever enters the repo (a hard rule), and `.env` is
gitignored.

| Variable | Meaning |
|---|---|
| `SHOPPING_COPILOT_LLM` | `off` (default) · `freeform` · `expand` |
| `SHOPPING_COPILOT_API_KEY` | Required for any model use. Unset → disabled. |
| `SHOPPING_COPILOT_MODEL` | Override the model id. |
| `SHOPPING_COPILOT_BASE_URL` | Override the endpoint. |

**Both** a key and an explicit mode are required. Neither alone turns the model on, so a
stray key in a teammate's shell cannot silently change a scored run. An unrecognized mode
fails *closed* to `off`.

The names are provider-neutral because the client is provider-neutral. The `SILICONFLOW_*`
names this shipped with **still work everywhere**: `llm.env_value` falls back to them, the
canonical name wins when both are set, and `env_file.update_env_file` rewrites a legacy line
*in place* on the next write — so an old `.env` keeps working and then quietly migrates
without losing its position under the comment documenting it. Nothing prints a deprecation
warning: `client_from_env` runs during `Agent()` construction, which is on the scored path,
and a warning there would land in the middle of an evaluator run. `env_file.MANAGED_KEYS` is
built from the `llm` constants rather than retyped, so the two cannot drift.

The three modes, in increasing order of how much they can touch a scored run:

- **`off`** — the default, and what the organizer runs (they have no key of ours). No client
  is constructed; every call site is skipped.
- **`freeform`** — the model is used *only* by `DialogState._observe_freeform`, to understand
  a human typing prose into the WebUI. That branch is **unreachable while scoring**: every
  simulated-customer reply is claimed by an earlier regex, which is why feature 11 came back
  byte-identical. So this mode is score-neutral *by construction*. The deterministic regexes
  stay authoritative — the model only fills slots they left empty ("burgundy", "a deep wine
  shade"), and its keywords are added as rerankable phrases.
- **`expand`** — additionally lets the model propose retrieval keywords, fused as **route 5**
  at `EXPANSION_ROUTE_WEIGHT = 0.25`. Deliberately below the category route: model-proposed
  terms are the only query text that neither the catalog nor the conversation vouches for.
  Like the phrase and dense routes it is unfiltered and can only *add* candidates; the
  reranker still decides the final order. Terms already in the query are dropped, so the
  route contributes nothing unless the model said something new.

Model output is treated as untrusted input throughout. Colours and materials must be single
tokens (a multi-word hallucination would become an FTS5 `AND` term and empty the catalog);
prices must be positive finite numbers (`bool` explicitly excluded, since `True` is an `int`
in Python); keywords are charset- and length-clamped, which drops injection-shaped strings
such as `red; DROP TABLE products` and `ignore all previous instructions` outright. Nothing
that survives is ever more than a quoted term inside an FTS5 `OR`.

`complete()` cannot raise: timeout, HTTP error, unparseable JSON, missing choices, non-string
content and malformed token counts all return `None`. There is no retry — a retry doubles
worst-case latency to chase an outcome we already have a good answer for. Timeout is 6 s.
Identical prompts are cached in-process.

Exactly one provider-specific field is ever sent, and it is gated on **both** the model id
and the endpoint: `enable_thinking: false` for a Qwen3 model on a SiliconFlow base URL. The
model half alone is not enough — the same weights are served under ids matching `qwen3`
elsewhere (OpenRouter's `qwen/qwen3-8b:free`, Ollama's `qwen3:8b`), and posting a vendor's
field to an endpoint that does not know it risks a rejected request.

`usage` is honest in both directions: zeros when no model is configured, real per-turn
`prompt_tokens`/`completion_tokens` when one is. `Agent.model_stats()` is the model-side
companion to `latency_stats()`.

### `.env` — `starter/env_file.py` (new)

Standard library only. `ensure_env_file` writes a documented template (no key, `off` by
default) if the file is absent and **never overwrites an existing one**; `parse_env_file` /
`load_env_file` / `update_env_file` read and rewrite it while preserving comments;
`bootstrap` does scaffold-then-load and returns a record with no values in it, so a key
cannot reach a log.

Two invariants make this safe:

- **A real environment variable wins.** `load_env_file(override=False)` is the default, so an
  export in the shell, in CI, or in front of a judging command still beats the file.
- **No scored module imports it.** `agent.py`, `dialog_state.py`, `retrieval.py`, `ranking.py`
  and `dense_retrieval.py` never touch it, so the evaluator run performs no file I/O beyond
  the catalog. `tools/verify_llm.py` asserts this by scanning the sources, so it cannot rot.

`webui/server.py` calls `bootstrap()` **before** constructing the agent (`Agent()` reads the
environment once, in its constructor). `--no-env` opts out; `--env-file` relocates it. The
same asymmetry applies to tooling: `webui/`, `tools/llm_smoke.py` and `tools/benchmark_llms.py`
load `.env`; `evaluator/` and `tools/feasibility_report.py` deliberately do not, because they
describe the *judged* configuration.

### The Model button — `webui/`

`Agent.configure_llm(api_key, mode, model, base_url)` swaps the client at runtime and
re-points live `DialogState`s, which captured the old client at `reset()`. An empty key or
`mode="off"` clears the client outright rather than leaving a disabled object around.

Three endpoints, all marshalled onto the agent thread like every other agent access:
`GET/POST /api/llm` and `POST /api/llm/test` (one real round trip, so an operator learns
whether the key works without reading a log).

**The key travels one way.** It is typed into the panel, POSTed to the local server, and
never returned — `GET /api/llm` reports `sk-1a2b...9f0e` via `agent_bridge._mask_key`. It is
written to disk only when the operator ticks "Save to `.env`".

A `model` chip in the top bar shows the mode at a glance and reads `freeform (paused)` in
amber when the breaker has tripped, so a demo cannot silently look healthy while running
offline.

### `tools/benchmark_llms.py` (new)

`llm_smoke.py` answers "does my key work". This answers "which model, and is it worth
enabling at all", in two layers:

- **Probes** — fixed prompts through `parse_freeform` and `expand_query`, the only two entry
  points the agent has. Scored on JSON parse rate, slot accuracy, price accuracy and term
  recall, plus mean/p95 latency and tokens. A model that is fast and *wrong* is worse than no
  model, and a latency table alone hides that.
- **`--sessions N`** — replays real public-set sessions in `expand` mode and reports
  TechnicalScore **against an offline control arm run on the same sessions**. Without the
  control, a model arm is a number with nothing to beat.

`--offline` swaps in a deterministic stub so the harness is exercisable, and CI-able, with no
key. Its output is labelled as measuring nothing about any real model.

### Circuit breaker — `starter/llm.py`

Per-call fail-soft was never enough: with the network down, every turn still paid
`TIMEOUT_SECONDS` before falling through. Repeated failure now latches the client off, and
`complete()` returns the same `None` a failure would — no socket, no wait — so every existing
caller's fallback runs unchanged.

Three trip conditions, because "unusable" has three shapes:

| Constant | Default | Trips on |
|---|---|---|
| `BREAKER_NETWORK_FAILURES` | 2 | consecutive *connection* failures — no route, DNS dead |
| `BREAKER_FAILURES` | 3 | consecutive failures of any kind |
| `BREAKER_SLOW_CALLS` / `BREAKER_SLOW_MS` | 3 / 4500 ms | consecutive *successes* too slow to be worth the wait |

An `HTTPError` is deliberately **not** a network failure: reaching the service and being told
"no" is a service problem, so it takes the slower 3-strike path. `reenable()` closes the
breaker again and is called only by the UI's Test button — an automatic half-open retry would
put the timeout back on the turn the breaker exists to protect.

## Measured impact

**Default configuration (`off`) — the judged one.** Full 200-session run against the score of
record:

| Metric | Before | After | Delta |
|---|---|---|---|
| HitRate@10 | 0.98 | 0.98 | 0 |
| MRR | 0.864018 | 0.864018 | 0 |
| MTTC | 2.85 | 2.85 | 0 |
| Efficiency | 0.815 | 0.815 | 0 |
| **TechnicalScore** | **0.912205** | **0.912205** | **0** |

All four scenarios are 0 on every metric. The comparison is stronger than the table shows:
the **entire results JSON is byte-identical** to `results/results_after_fieldfactors.json`, sessions
array included — not merely score-identical. That still holds with a `.env` sitting in the
repo root, because the evaluator reads `os.environ` directly.

**Configured for `expand`, but the network is dead.** Key and mode both exported, every socket
raising — i.e. official judging with the network cut while a teammate's shell still has
credentials in it. Also **byte-identical**, full document. This is the fallback requirement
discharged as a measurement rather than an assertion.

**Configured for `expand`, with the route actually firing.** Measured against deterministic
stubs, so this measures the *mechanism*, not any model:

| Stub | What it feeds the route | TechnicalScore | Delta |
|---|---|---|---|
| `echo` | words already in the evidence | 0.912205 | 0 (see below) |
| `novel` | plausible related terms the shopper didn't say | 0.912089 | −0.000116 |
| `noise` | pure generic filler, every turn | 0.912072 | −0.000133 |

HitRate@10 stays 0.98 in all three; the movement is MRR and a half-turn of MTTC. So the worst
case measured — a model emitting pure noise into the query every single turn — costs
**0.00013**, about 1/75th of the 200-session noise floor, with HitRate untouched. The reranker
absorbs it, which is the same reason the dense route was safe to ship at 0.3.

**The circuit breaker fired in anger, twice, unprompted** — the first live confirmations that
it works outside a stub. `nemotron-3.5-lightning` tripped the slow-call rule at ~8.7 s mean;
the rate-limited `gemma` tripped the failure rule after three fast 429s.

Verification grew from **52 to 96 checks** (`py tools/verify_llm.py`), covering all three
breaker trip paths, breaker-open making no network call, `.env` round-tripping, precedence,
legacy-alias loading and in-place migration, the no-scored-module-imports-`env_file` scan,
`configure_llm` including that clearing re-points live sessions, and provider-message
extraction. `verify_features.py` is unchanged at 61/63 with the two pre-existing XFAILs.

End-to-end through the running UI: `.env` auto-created on first launch, mode/key applied live,
key returned masked (`sk-t...7890`), an invalid mode rejected with 400, a real turn answered
in 37 ms with a broken key configured, and persistence writing `.env` with the template's
comments intact.

## Bugs found and fixed

### The model dialog could not be closed

Reported from the UI: after pressing Apply the popup would not go away. Nothing to do with
`off`, and nothing to do with the server — the dialog was **never** dismissible, and it was a
CSS cascade mistake in `webui/static/styles.css`.

`closeLlm()` sets `el.hidden = true`, which relies on the browser's own
`[hidden] { display: none }`. But `.modal { display: flex }` is an *author* rule, and author
rules beat user-agent rules regardless of specificity — so the attribute toggled with no
visual effect and the close button, Escape and the backdrop click were all inert.

Fixed with one global declaration, placed with the reset rather than patched onto `.modal`,
so the next element toggled through `.hidden` cannot hit the same trap:

```css
[hidden] { display: none !important; }
```

`.found` (the other `hidden`-toggled element) sets no `display` and was unaffected, but is now
covered by the same rule. The `off` transition itself was verified clean server-side:
`freeform` + key → `off` → `off` with persist all return correct state and no error.

### "Call failed" told the operator nothing

Reported from the UI: Test connection kept saying the call failed. The call really was failing
— an exhausted OpenRouter daily quota — but **the message was the bug**. The same sentence
covered a dead network, a rejected key and a spent quota: three problems whose fixes have
nothing in common.

Three changes, all outside the scored path:

- `urllib_transport` now catches `HTTPError` and *returns* `(code, body)` instead of letting
  it propagate. `urlopen` raises on any non-2xx and threw the body away — and the body is
  where the provider explains itself. Returned rather than re-raised so it takes the same path
  as a stubbed non-200, and the breaker still classifies it as a service failure (we reached
  the service) rather than an outage.
- `_provider_message` pulls the useful sentence out of the error body, preferring
  `error.metadata.raw` (OpenRouter's upstream text) over `error.message` (its own wrapper),
  and falling back to a trimmed raw body for non-JSON. `SiliconFlowClient.last_error` keeps
  it, `stats()` exposes it, a success clears it.
- `agent_bridge._explain_failure` turns it into an instruction — 429 says it is a quota and
  not a bad key, 401/403 says check the key, 404 says the slug is gone — with the provider's
  raw text appended in brackets. `llm_smoke.py` prints the same triage.

Before: *"The call failed (timeout, bad key, or no network)."*
After: *"Rate limited. On OpenRouter's free tier that is 50 requests per DAY across all models
— it resets on its own, or add credits. [HTTP 429: Rate limit exceeded: free-models-per-day]"*

Six checks cover it, including that a non-JSON error body still surfaces and that the text
never contains the key.

## Negative results — do not re-attempt

- **Suppressing reasoning on OpenRouter.** Most free slugs in this tier are reasoning models
  that return the trace in a separate `reasoning` field and, when they ramble, spend the whole
  `max_tokens` budget there and return `content: null`. Three switches were tried against the
  live endpoint: `reasoning={"enabled": false}` **nulled content outright** (worse),
  `reasoning={"exclude": true}` only hides the trace without freeing the budget (no measured
  benefit), and the `effort: "low"` run was contaminated by the daily quota running out. Plain
  OpenAI payload scored best of the lot, so **nothing provider-specific is sent to
  OpenRouter.** The null content is already handled — `complete()` treats it as a failed call
  and the agent falls through to offline retrieval.
- **The `echo` stub is a trap, not a result.** It was the first stub written for the `expand`
  measurement and returned only words already in the evidence, every one of which route 5
  strips as non-novel — so no route was ever appended and the run came back byte-identical.
  That looked like proof of safety and was proof of nothing. The `novel` and `noise` rows in
  the table above are the ones that actually exercise the route. Any future stub must be
  checked for this.

## Limitations & follow-ups

- **`expand` has still never been measured end-to-end against a live model.** The probes have
  (see the benchmark table), but no full 200-session run in `expand` mode against a real
  endpoint — the free quota does not stretch to it. `expand` stays experimental and off; the
  submitted configuration is `off`, which is byte-identical to the score of record and needs
  no key at all.
- **`expand` is not reproducible the way the rest of the agent is.** Greedy decoding
  (`temperature 0`, `top_p 1`) is the closest these endpoints offer, but server-side batching
  means identical input is not *guaranteed* to give identical output. The project's "a changed
  score means a changed agent" property — and the control-arm guard in
  `tools/sweep_constants.py`, which now refuses to run at all when a mode is set — hold only
  in `off` and `freeform`.
- **Free-tier quota is the binding constraint, not correctness.** 50 requests/day shared
  across models. `expand` calls the model every turn, so it exhausts the quota in a handful of
  sessions; `freeform` only calls when a regex cannot parse the input, which is far lighter.
- **Free slugs come and go.** If the default starts 404ing, re-run the benchmark and take the
  winner. Nothing depends on the exact value.
- **Breaker thresholds are reasoned, not fitted.** 2/3/3 and 4500 ms are chosen against a
  6000 ms timeout to trip before a demo becomes unwatchable. There is no principled way to
  tune them without far more live traffic than the free tier allows.
- **The breaker never self-closes.** Deliberate — an automatic half-open retry would put the
  timeout back on the turn the breaker exists to protect — but it means a transient outage
  keeps the model off until someone presses Test or restarts.
- **`freeform` cannot help the score, by design.** It is a product/demo improvement. If the
  organizer adds the natural-language paraphrasing the spec hints at, that branch starts being
  reachable and this becomes a genuine robustness asset — but that is a hypothesis about a
  future dataset, not a measured gain.
- **Latency.** A `freeform` turn adds one round trip (~1.5 s typical, 6 s worst case) to a turn
  that is currently ~58 ms. Fine for a human at a keyboard, not something to put on the scored
  path even if it were allowed to help.
- **`configure_llm` is unreachable from the evaluator**, so runtime mode switching is untested
  under scoring conditions and should stay that way: the judged configuration is whatever the
  environment said at construction.
- **A key in `.env` is plaintext on disk.** It is gitignored and never logged or returned, but
  it is not encrypted. Persisting is opt-in per save for that reason.
- **The `SiliconFlowClient` class name is still historical.** The environment variables were
  made provider-neutral; the class was not, because renaming it touches `starter/`, `webui/`
  and both verify tools for a purely cosmetic gain. This is the one follow-up worth taking if
  time appears.
