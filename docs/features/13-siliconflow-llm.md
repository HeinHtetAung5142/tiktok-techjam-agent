# 13 — Optional SiliconFlow LLM (Qwen3-8B)

**Status:** merged, off by default
**Commit:** (this one)
**Owner:** Integration
**Tier:** 3 (feasibility / product) — deliberately score-neutral in the judged configuration

## What & why

The model policy said "local and offline **for now** … a hosted model may be added later …
but only behind a fallback that still runs with network access disabled." This is that
addition, wired so it can be demonstrated without ever putting the 0.912205 score of
record at risk.

The requirement was explicit: **the feature only counts if the score and accuracy do not
go down.** That rules out any design where a model sits on the scored path and we argue
afterwards that it probably helped. So the model is opt-in, and the judged configuration
is a provable no-op.

## Model choice

`Qwen/Qwen3-8B` on SiliconFlow's OpenAI-compatible endpoint
(`https://api.siliconflow.cn/v1/chat/completions`).

| Candidate | Free? | Why / why not |
|---|---|---|
| **`Qwen/Qwen3-8B`** | permanently free | **Chosen.** 128K context, instruction-tuned, reliable at terse JSON, and accepts `enable_thinking: false` so we don't pay latency for a reasoning trace we discard. |
| `deepseek-ai/DeepSeek-R1-Distill-Qwen-7B` | permanently free | Rejected. A reasoning distill: emits long `<think>` blocks, so higher latency and tokens for the short structured extraction we actually want per turn. |
| `deepseek-ai/DeepSeek-OCR` | free, rate-limited | Not a chat model. Irrelevant here — this track is text-only. |

Free tier at time of writing: 1,000 RPM / 50K TPM, no credit card. Note for the team:
SiliconFlow requires real-name verification to use the free models, which assumes
mainland-Chinese documents; international accounts must contact their support. **That is
a reason the default must be "off" rather than "on if a key happens to exist."**

No new dependency. The client is `urllib.request` from the standard library, so
`requirements.txt` is untouched and `pip install -r requirements.txt` still reproduces
the run exactly. `numpy`/`scipy`/`scikit-learn` remain the only third-party pins.

## Approach

New module `starter/llm.py`. Configuration is environment-only — no key ever enters the
repo (a hard rule), and `.env` is already gitignored.

| Variable | Meaning |
|---|---|
| `SILICONFLOW_API_KEY` | Required for any model use. Unset → disabled. |
| `SHOPPING_COPILOT_LLM` | `off` (default) · `freeform` · `expand` |
| `SILICONFLOW_MODEL` | Override the model id. |
| `SILICONFLOW_BASE_URL` | Override the endpoint. |

**Both** a key and an explicit mode are required. Neither alone turns the model on, so a
stray key in a teammate's shell cannot silently change a scored run. An unrecognized mode
fails *closed* to `off`.

The three modes, in increasing order of how much they can touch a scored run:

- **`off`** — the default, and what the organizer runs (they have no key of ours). No
  client is constructed; every call site is skipped.
- **`freeform`** — the model is used *only* by `DialogState._observe_freeform`, to
  understand a human typing prose into the WebUI. That branch is **unreachable while
  scoring**: every simulated-customer reply is claimed by an earlier regex, which is why
  feature 11 came back byte-identical. So this mode is score-neutral *by construction*.
  The deterministic regexes stay authoritative — the model only fills slots they left
  empty ("burgundy", "a deep wine shade"), and its keywords are added as rerankable
  phrases.
- **`expand`** — additionally lets the model propose retrieval keywords, fused as
  **route 5** at `EXPANSION_ROUTE_WEIGHT = 0.25`. Deliberately below the category route:
  model-proposed terms are the only query text that neither the catalog nor the
  conversation vouches for. Like the phrase and dense routes it is unfiltered and can only
  *add* candidates; the reranker still decides the final order. Terms already in the query
  are dropped, so the route contributes nothing unless the model said something new.

Model output is treated as untrusted input throughout. Colours and materials must be
single tokens (a multi-word hallucination would become an FTS5 `AND` term and empty the
catalog); prices must be positive finite numbers (`bool` explicitly excluded, since
`True` is an `int` in Python); keywords are charset- and length-clamped, which drops
injection-shaped strings such as `red; DROP TABLE products` and
`ignore all previous instructions` outright. Nothing that survives is ever more than a
quoted term inside an FTS5 `OR`.

`complete()` cannot raise: timeout, HTTP error, unparseable JSON, missing choices,
non-string content and malformed token counts all return `None`. There is no retry — a
retry doubles worst-case latency to chase an outcome we already have a good answer for.
Timeout is 6s. Identical prompts are cached in-process.

`usage` is now honest in both directions: zeros when no model is configured (unchanged),
real per-turn `prompt_tokens`/`completion_tokens` when one is. `Agent.model_stats()` is
the model-side companion to `latency_stats()`.

## Measured impact

**Default configuration (`off`) — the judged one.** Full 200-session run, compared to the
score of record:

| Metric | Before | After | Delta |
|---|---|---|---|
| HitRate@10 | 0.98 | 0.98 | 0 |
| MRR | 0.864018 | 0.864018 | 0 |
| MTTC | 2.85 | 2.85 | 0 |
| Efficiency | 0.815 | 0.815 | 0 |
| **TechnicalScore** | **0.912205** | **0.912205** | **0** |

All four scenarios are 0 on every metric. The comparison is stronger than the table
shows: the **entire results JSON is byte-identical** to `results_after_fieldfactors.json`,
sessions array included — not merely score-identical.

**Configured for `expand`, but the network is dead.** Key and mode both exported, every
socket raising — i.e. official judging with the network cut while a teammate's shell still
has credentials in it. Also **byte-identical**, full document. This is the fallback
requirement discharged as a measurement rather than an assertion.

**Configured for `expand`, with the route actually firing.** No key exists for a real
200-session run, so the transport was replaced by deterministic stubs. This measures the
*mechanism*, not the model:

| Stub | What it feeds the route | TechnicalScore | Delta |
|---|---|---|---|
| `echo` | words already in the evidence | 0.912205 | 0 (see below) |
| `novel` | plausible related terms the shopper didn't say | 0.912089 | −0.000116 |
| `noise` | pure generic filler, every turn | 0.912072 | −0.000133 |

HitRate@10 stays 0.98 in all three; the movement is MRR and a half-turn of MTTC.

**Read the `echo` row as a warning, not a result.** It was the first stub written and it
returned only words already in the evidence, every one of which route 5 strips as
non-novel — so no route was ever appended and the run came back byte-identical. That
looked like proof of safety and was proof of nothing. The `novel` and `noise` rows are
the ones that actually exercise the route.

So the worst case measured — a model emitting pure noise into the query on every single
turn — costs **0.00013**, about 1/75th of the 200-session noise floor, with HitRate
untouched. The reranker absorbs it, which is the same reason the dense route was safe to
ship at 0.3.

## Limitations & follow-ups

- **The real model has never been measured on the public set.** Nobody on the team has a
  SiliconFlow key yet, so every enabled-path number above comes from a stub. `expand` is
  therefore marked experimental and left off. Run `py tools/llm_smoke.py` with a key to
  confirm the endpoint, then a full evaluator run before quoting any enabled-mode score.
- **`expand` is not reproducible the way the rest of the agent is.** Greedy decoding
  (`temperature 0`, `top_p 1`) is the closest this endpoint offers, but server-side
  batching means identical input is not *guaranteed* to give identical output. The
  project's "a changed score means a changed agent" property — and the control-arm guard
  in `tools/sweep_constants.py` — hold only in `off` and `freeform`.
- **`freeform` cannot help the score, by design.** It is a product/demo improvement. If
  the organizer adds the natural-language paraphrasing the spec hints at, that branch
  starts being reachable and this becomes a genuine robustness asset — but that is a
  hypothesis about a future dataset, not a measured gain.
- **Latency.** A `freeform` turn adds one round trip (~6s worst case) to a turn that is
  currently ~58ms. Fine for a human at a keyboard, not something to put on the scored
  path even if it were allowed to help.
- Rate limits and the real-name-verification requirement make SiliconFlow unsuitable as a
  hard dependency for judging. It is an enhancement, and the offline agent remains the
  product.
