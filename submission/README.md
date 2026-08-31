# Shopping Copilot — setup and run

A multi-turn conversational shopping agent that finds a hidden target product inside a
50,000-item catalog. **It runs fully offline: no LLM call, no API key, no network access.**

**This file is only how to get it running.** Method, model choice, cost, latency,
limitations and team contributions are in **[`REPORT.md`](REPORT.md)**.

| | |
|---|---|
| Python | **3.10+** — verified on 3.14.7 and 3.12.0 |
| Dependencies | 3 (`numpy`, `scipy`, `scikit-learn`) |
| Network access | none |
| API keys / env vars | none |
| Time | ~2 min to install, ~30 s per full run |

---

## Step 1 — Create a virtual environment

Recommended, not required — but our three dependencies are pinned to exact versions, and a
venv keeps them away from your system Python.

**Windows (PowerShell):**

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
```

If PowerShell refuses with an execution-policy error, either use the batch activator
(`.venv\Scripts\activate.bat`) or allow scripts for this shell session only:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

**macOS / Linux:**

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Your prompt should now be prefixed with `(.venv)`. Leave it later with `deactivate`.

> **Which launcher to type.** *Outside* a venv on Windows, use **`py`** — `python` and
> `python3` there often resolve to the Microsoft Store stub and fail. *Inside* an activated
> venv, `py` and `python` both resolve to the venv's interpreter (verified on the launcher
> shipped with Python 3.14; launchers older than 3.11 ignore `VIRTUAL_ENV`, so on those
> `python` is the unambiguous choice).
>
> Check it at any point:
>
> ```bash
> python -c "import sys; print(sys.executable)"
> ```
>
> The path printed must be inside your `.venv`. This is worth ten seconds — the common
> failure is installing into the venv and then running the agent outside it.

## Step 2 — Install dependencies

```bash
pip install -r requirements.txt
```

Installs `numpy`, `scipy` and `scikit-learn`, used by the dense-retrieval route. They are
the **only** third-party dependencies; everything else is standard library.

> **This step is required, and the failure mode is quiet.** The dense index imports lazily
> inside a broad `try/except`, so a missing scientific stack degrades to sparse-only
> retrieval rather than crashing. The single symptom is one line on stderr:
>
> ```text
> [dense_retrieval] disabled: ModuleNotFoundError("No module named 'numpy'")
> ```
>
> Miss that line and the run looks normal while scoring a **different agent** — sparse-only
> returns 0.909858 against the 0.912205 of record.

## Step 3 — Put the catalog in place

The agent reads the frozen official catalog from `data/catalog.jsonl` (50,000 rows). It is
deliberately **not** included in this bundle — `submission_rules.md` bars shipping
evaluation data, and you already hold the frozen artifact.

Either place it at `data/catalog.jsonl` relative to your working directory, or pass the
path explicitly; the constructor takes it as its first positional argument:

```python
Agent("/path/to/catalog.jsonl")
```

## Step 4 — Nothing else

No environment variables. No API keys. No network access. No config file. No model weights
to download — the FTS5 index and the LSA embeddings are both built in memory at startup,
from the catalog itself.

---

## Running it

### In the official harness

> **The one thing that must be true: this directory has to be on `sys.path`.** Everything
> else here follows from it. Either run from inside this directory, or name it explicitly:
>
> ```bash
> PYTHONPATH=/path/to/this/directory python3 -m your_harness      # macOS / Linux
> ```
> ```powershell
> $env:PYTHONPATH = "C:\path\to\this\directory"; py -m your_harness   # Windows
> ```
>
> This matters because `python your_harness.py` puts **the harness's own directory** at
> `sys.path[0]` — *not* the working directory. So a harness that lives in a different
> folder cannot import the agent even when launched from in here, and the symptom is
> `ModuleNotFoundError: No module named 'agent'` (or `'starter'`) before any of our code
> runs. Setting `PYTHONPATH`, or invoking with `-m` from this directory, fixes it.
>
> Once the directory is on the path, both import styles work and the bundle may also be
> dropped into a larger tree as a subpackage (`from submissions.our_team.agent import
> Agent`) or loaded by absolute path with `importlib`.

**macOS / Linux** (and any activated venv):

```bash
python3 -m evaluator.local_evaluator
```

**Windows, outside a venv** — `python3` there is the Microsoft Store stub, so use the
launcher:

```powershell
py -m evaluator.local_evaluator
```

Both import paths resolve to the same class, so it does not matter which your harness uses:

```python
from agent import Agent          # canonical entry point (submission_rules.md layout)
from starter.agent import Agent  # the starter-kit path; a 5-line re-export shim
```

There is **one** implementation, in `src/`. Neither shim contains logic.

The harness constructs the agent with the catalog path as a single positional argument,
then drives the session:

```python
agent = Agent("data/catalog.jsonl")          # ~20-30 s, once per process
agent.reset(session_id, user_profile)
response = agent.respond(session_id, user_message, turn, top_k)
```

### Expected result

| Metric | Value |
|---|---|
| Hit Rate@10 | 0.98 |
| MRR | 0.864018 |
| MTTC | 2.85 |
| **TechnicalScore** | **0.912205** |

**Runs are deterministic.** Identical code scores identically, bit for bit — a changed
score means a changed agent, never run-to-run noise. If your number differs, check for the
`[dense_retrieval] disabled` line first; that accounts for the common case.

### Response shape

```python
{
    "message": "Do you have a material preference?",   # str
    "ask_attribute": "material",                       # one allowed attribute, or None
    "recommendations": [{"parent_asin": "B000..."}],   # ordered best to worst
    "usage": {"prompt_tokens": 0, "completion_tokens": 0},
}
```

`ask_attribute` is one of `category`, `material`, `color`, `size`, `style`, `brand`,
`budget`, `feature`, `use_case`, `other`, or `null`. Only the first 10 valid, unique,
in-catalog ids are scored. `usage` reports honest zeros — no model is called.

Latency is deliberately **not** in the response payload: the contract sets
`"additionalProperties": false` on both `turn_response` and `usage`, so an extra key would
be malformed output. It is exposed on the agent instead, via `Agent.latency_stats()` and
`Agent.model_stats()`.

### A first run, by hand

From inside this directory, to confirm the install before wiring up a harness:

```bash
python -c "from agent import Agent; a=Agent('data/catalog.jsonl'); a.reset('s',{}); print(a.respond('s','I need a cotton shirt for work',1,10))"
```

Expect a dict with the four keys above, an `ask_attribute` of `other`, and **one**
recommendation — turn 1 discloses a single item on purpose (see `REPORT.md`, "Trade turns
for rank").

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `[dense_retrieval] disabled: ModuleNotFoundError` on stderr | Step 2 did not take effect, or the venv was activated after installing. Re-run `pip install -r requirements.txt` with the venv active. Scores 0.909858 instead of 0.912205. |
| `python` / `python3` opens the Microsoft Store | The Windows stub. Use `py`, or activate the venv first. |
| `.venv\Scripts\Activate.ps1 cannot be loaded` | PowerShell execution policy — see Step 1. |
| `FileNotFoundError: data/catalog.jsonl` | Step 3. Pass an explicit path to `Agent(...)` if it lives elsewhere. |
| `ModuleNotFoundError: No module named 'agent'` or `'starter'` | This directory is not on `sys.path`. `python harness.py` uses the *harness's* directory, not the working directory — see the note under "In the official harness". Set `PYTHONPATH` to this directory, or invoke with `-m` from inside it. |
| `ModuleNotFoundError: No module named 'src'` | Same cause, one step later: `agent.py` was found but its directory is not on `sys.path`. Both entry points recover from this automatically; if you still see it, `src/` is missing from the bundle. |
| `RuntimeError: reset must be called before respond` | `reset(session_id, ...)` must precede `respond(session_id, ...)` for every session id. |
| The first call takes ~20–30 s | Expected, not a hang. The FTS5 index over 50,000 products and the LSA embeddings are built once at construction, then reused for every session and turn. |

## What this bundle contains

```text
agent.py            entry point — exports Agent
README.md           this file
REPORT.md           method, model choice, cost, latency, limitations, team
requirements.txt    the three pinned dependencies
src/                the implementation (8 modules)
starter/            5-line re-export shim for the starter-kit import path
```

`src/` is generated from the development repository's `starter/` package; the only
difference is the import paths. Nothing in this bundle reads from the network or writes to
disk.
