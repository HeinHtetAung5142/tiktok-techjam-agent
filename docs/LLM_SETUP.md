# Setting up a language model

## Do you need this?

Probably not, for the competition run. **The submitted configuration is `off`**: no key, no
network, no model call, and it scores TechnicalScore **0.912205** on its own. That is the
configuration the organizer runs, and nothing here changes it.

This is for two other things:

- Typing real prose at the agent in the WebUI (`py -m webui.server`). The agent's regexes
  are shaped for the *simulated* customer; a model helps interpret a human.
- Experimenting with model-assisted retrieval (`expand` mode).

If you only want to reproduce the score, skip this file entirely.

## The two variables

| Variable | Meaning |
|---|---|
| `SHOPPING_COPILOT_LLM` | `off` (default) · `freeform` · `expand` |
| `SHOPPING_COPILOT_API_KEY` | your key. Unset → the model is off |

**Both are required.** Neither alone does anything, so a stray key in someone's shell
cannot silently change a scored run, and an unrecognized mode fails *closed* to `off`.

> The `SILICONFLOW_*` names this shipped with still work everywhere — in the shell and in
> `.env` — so nothing you already set breaks. The `SHOPPING_COPILOT_*` name wins when both
> are present, and `.env` rewrites the old spelling to the new one the next time anything
> saves to it.

Which mode:

- **`freeform`** — what you want. The model is consulted only from
  `DialogState._observe_freeform`, a branch the simulated customer never reaches, so it
  **cannot** move the competition score. The deterministic regexes stay authoritative; the
  model only fills slots they left empty.
- **`expand`** — adds a low-weight keyword route to retrieval. Experimental: never
  measured against a live model, and it makes runs non-reproducible (see the caveat at the
  bottom). Use it for experiments, not for anything you intend to quote.

## Any OpenAI-compatible provider works

Two more variables override the endpoint and the model id. **You do not need to set either
one for the default provider.**

| Variable | Default |
|---|---|
| `SHOPPING_COPILOT_MODEL` | `inclusionai/ling-3.0-flash-fin:free` |
| `SHOPPING_COPILOT_BASE_URL` | `https://openrouter.ai/api/v1` |

The names are historical — feature 13 targeted SiliconFlow before its free tier turned out
to need mainland-Chinese ID verification. `starter/llm.py` speaks plain OpenAI-compatible
chat completions — `POST {base_url}/chat/completions`, `Bearer` auth, `messages` /
`temperature` / `max_tokens` — so **OpenRouter (the default), Groq, Together, DeepInfra,
LM Studio, SiliconFlow or a local Ollama all work with no code change.**

### OpenRouter — the default, and the easiest key to get

No identity verification, and genuinely free model slugs. A key is the only thing you set:

```bash
SHOPPING_COPILOT_LLM=freeform
SHOPPING_COPILOT_API_KEY=sk-or-v1-...
# base_url and model can stay unset -- these are the defaults
```

The default model is `inclusionai/ling-3.0-flash-fin:free`, and it was chosen by
**measurement** — `py tools/benchmark_llms.py` against every free slug that would answer,
run twice:

| Model | parse / slots / price / terms | mean |
|---|---|---|
| **`inclusionai/ling-3.0-flash-fin:free`** | **100 / 100 / 100 / 100** | **~1.5 s** |
| `nvidia/nemotron-3-super-120b-a12b:free` | 80 / 100 / 100 / 50 | 4–11 s, unstable |
| `google/gemma-4-26b-a4b-it:free` | rate-limited upstream, no reading | — |
| `nvidia/nemotron-3.5-lightning:free` | 0 / 0 / 0 / 0 | ~8.7 s, tripped the breaker |
| `liquid/lfm-2.5-2.6b:free`, `minimax/minimax-m2.7:free` | 20 / 0 / 0 / 0 | cannot hold the JSON contract |

The `-fin` suffix is a finance-tuned variant, which looks wrong for a clothing catalog —
it is the first thing to re-measure if quality ever looks off. It simply beat every
general-purpose free slug on our own probes, twice, and the probes are the job.

**Two things will bite you, and neither is a broken key:**

- **OpenRouter's free tier is 50 requests per _day_** on an account with no credits, shared
  across all models. One benchmark run over five models spends about half of it. When it is
  gone *everything* returns 429; the `X-RateLimit-Remaining` header in the error body is how
  you tell. Adding 10 credits raises it to 1,000/day.
- **Free pools are rate-limited upstream too**, per model and independently of your quota.
  That is what took both Google slugs out of the comparison above.

**Free slugs also come and go.** If the default starts failing, re-run the benchmark and
take the winner. Nothing depends on the exact value. Avoid `openrouter/free` — it selects a
free model *at random* per call, so the feasibility disclosure could not name a model and
`expand` would be even less reproducible than it already is.

Most free slugs in this tier are reasoning models: they return the trace in a separate
`reasoning` field and, when they ramble, spend the whole token budget on it and hand back
`content: null`. The client treats that as a failed call and falls through to offline
retrieval. Suppressing it was tried — `reasoning.enabled=false` made it *worse* and
`reasoning.exclude=true` showed no benefit — so nothing provider-specific is sent.

### Ollama — a real local model, no key, no network

The only option that keeps the "runs offline" property literally true, and the one to use
if you want the demo to work with the wifi off.

```bash
ollama pull qwen3:8b          # a few GB, once
ollama serve                  # usually already running
```

```bash
SHOPPING_COPILOT_LLM=freeform
SHOPPING_COPILOT_API_KEY=local     # any non-empty placeholder; Ollama ignores it
SHOPPING_COPILOT_BASE_URL=http://localhost:11434/v1
SHOPPING_COPILOT_MODEL=qwen3:8b
```

The key must be non-empty or the client stays off — that check is what stops a half-configured
shell from enabling a model. On CPU expect seconds per call; the breaker will latch it off
if it is consistently slower than 4.5 s, which is the intended behaviour, not a bug.

### SiliconFlow — what the code was originally built against

```bash
SHOPPING_COPILOT_LLM=freeform
SHOPPING_COPILOT_API_KEY=sk-...
SHOPPING_COPILOT_BASE_URL=https://api.siliconflow.cn/v1
SHOPPING_COPILOT_MODEL=Qwen/Qwen3-8B
```

Pointing back at SiliconFlow also re-enables the `enable_thinking: false` switch, which is
sent only for a Qwen3 model on a SiliconFlow endpoint — it is their field, not OpenAI's,
and other endpoints may reject a request carrying it.

`Qwen/Qwen3-8B` is permanently free there (1,000 RPM / 50K TPM, no credit card). **But the
free tier requires real-name verification, which assumes mainland-Chinese documents**;
international accounts have to go through their support. That is why it is no longer the
default, and why nobody on this team ever obtained a key for it.

## Three ways to set the variables

### 1. The WebUI Model button — easiest

```bash
py -m webui.server
```

Click **model** in the top bar. Pick a mode, paste the key, override the model id and base
URL only if you are not on OpenRouter, press **Apply** — it takes effect immediately, with no
restart and no second index build. Press **Test connection** to make one real call and
find out straight away whether it works. Tick **Save to `.env`** to keep it.

The key never comes back out: the panel only ever shows a mask like `sk-1a2b...9f0e`.

### 2. `.env` in the repo root

Created automatically with offline defaults the first time you run `py -m webui.server`.
Edit it:

```ini
SHOPPING_COPILOT_LLM=freeform
SHOPPING_COPILOT_API_KEY=sk-...
SHOPPING_COPILOT_MODEL=
SHOPPING_COPILOT_BASE_URL=
```

It is **gitignored** — never commit a key, that is a hard rule. Blank values mean "use the
default", not "set it to empty".

### 3. A shell export

```powershell
$env:SHOPPING_COPILOT_LLM = "freeform"      # PowerShell
$env:SHOPPING_COPILOT_API_KEY = "sk-..."
```

```bash
export SHOPPING_COPILOT_LLM=freeform         # bash
export SHOPPING_COPILOT_API_KEY=sk-...
```

**Precedence: a real environment variable always beats `.env`.** So an export in your
shell, in CI, or in front of a single command wins, and judging is unaffected by whatever
`.env` happens to contain.

**Who reads `.env`:** `webui/`, `tools/llm_smoke.py` and `tools/benchmark_llms.py` — the
interactive tools. **Who does not:** `evaluator/` and `tools/feasibility_report.py`, which
describe the *judged* configuration and read real environment variables only. That
asymmetry is deliberate: it is why a full 200-session run stays byte-identical with a
`.env` sitting in the repo root.

## Check it worked

Run these in order. Each one proves something the previous one could not.

```bash
py tools/verify_llm.py                      # 96 checks, no key, no network
py tools/llm_smoke.py                       # YOUR key/model/endpoint, one real call
py tools/benchmark_llms.py --offline        # the benchmark harness, on a stub
py tools/benchmark_llms.py --models A,B     # compare real candidates
py tools/benchmark_llms.py --models A --sessions 50   # ...and score against a control arm
```

- **`verify_llm.py`** proves the *wiring* — parsing, clamping, fail-soft, the breaker. It
  stubs HTTP, so it passes with no key and tells you nothing about your provider.
- **`llm_smoke.py`** is the one that needs credentials. Exit 0 means the key, the model id
  and the endpoint all work *and* the responses parse into the shapes the agent expects. It
  prints the resolved model and base URL first, so check those match what you intended.
- **`benchmark_llms.py`** compares candidates on the two jobs the agent actually gives a
  model, and with `--sessions N` reports TechnicalScore against an offline control arm run
  on the same sessions. Never read a model arm without the control.

## When it goes wrong

Mostly, it doesn't visibly — and that is by design.

- **Every failure returns `None`** — timeout, HTTP error, bad key, malformed JSON, no
  network — and the agent falls through to the offline pipeline that scores 0.912205 by
  itself. A broken model makes the agent *ordinary*, never broken.
- **Repeated failure latches it off.** 2 consecutive connection failures ("the network is
  down"), 3 failures of any kind, or 3 consecutive responses slower than 4.5 s trip a
  circuit breaker, and the client then answers instantly with `None` instead of waiting on
  a dead socket every turn. The WebUI chip turns amber and reads `freeform (paused)`.
  **Test connection clears it.**
- **`llm_smoke.py` says `NOT SET`** — you set only one of the two variables, or you put the
  key somewhere the tool does not read. Both are required.
- **A 404 on a valid-looking key** is usually a model id that provider does not serve.
  Check `SHOPPING_COPILOT_MODEL` against their models page.

## Two cautions

**`expand` is not reproducible.** Greedy decoding (`temperature 0`, `top_p 1`) is the
closest these endpoints offer, but server-side batching means identical input is not
*guaranteed* to give identical output. The project's "a changed score means a changed
agent" property — and the control-arm guard in `tools/sweep_constants.py`, which refuses to
run at all when a mode is set — hold only in `off` and `freeform`.

**No key ever goes in the repo.** `.env` is gitignored, the generated template ships blank,
the WebUI returns only a mask, and nothing logs a key. Keep it that way.
