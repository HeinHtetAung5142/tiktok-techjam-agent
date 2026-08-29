# TechJam Conversational E-Commerce Search Challenge

Build an AI shopping agent that asks useful follow-up questions and recommends the customer's hidden target product within at most 10 turns.

## What You Receive

- A frozen catalog of 50,000 products from the `Clothing_Shoes_and_Jewelry` category of Amazon Reviews 2023.
- 200 labeled public sessions for local development.
- A weak BM25 starter agent and deterministic local evaluator.
- The Agent API contract and scoring rules.

The organizer keeps 800 additional sessions private for final evaluation.

## Task

For each session, your agent receives an anonymized preference profile and a short customer message. Raw user IDs, review text, timestamps, and purchase history are never disclosed. On every turn the agent may:

- ask a natural clarification question in `message` and identify one requested field in `ask_attribute`;
- return a ranked list of up to 10 catalog `parent_asin` values;
- do both in the same response.

The session ends when the target product appears in the scored Top 10 or after turn 10. Sessions cover Buying, Browsing, Intent Override, and Boundary behavior.

## Download the Catalog

Download `catalog.jsonl.gz` from the GitHub Release attached to this repository, then run:

```bash
gzip -dk catalog.jsonl.gz
mv catalog.jsonl data/catalog.jsonl
```

Verify the downloaded file using the published `SHA256SUMS` file.

## Setup and Reproduction

**Python 3.10 or later.** Developed and verified on 3.14.7.

### 1. Install dependencies — required

```bash
pip install -r requirements.txt
```

This installs `numpy`, `scikit-learn`, and `scipy` (a transitive dependency of scikit-learn,
pinned to the version this agent was verified against). They are used by the dense-retrieval
route in `starter/dense_retrieval.py`. **The agent will not import without them.**

Versions are pinned exactly. If you install different versions the run may still work, but it is
no longer the configuration we validated.

### 2. Download the catalog

Follow *Download the Catalog* above so `data/catalog.jsonl` exists (50,000 rows) before running.

### 3. Run the evaluator — one command

```bash
python3 -m evaluator.local_evaluator
```

On Windows, `python` and `python3` may resolve to the Microsoft Store stub and fail. Use the real
launcher instead — the module path is identical:

```bash
py -m evaluator.local_evaluator
```

Writes per-session results and aggregate metrics to `results.json`, and prints the aggregate plus
per-scenario breakdown to stdout. No environment variables, no API keys, no network access
required — see *Model Choice and Cost* below.

### Expected result

A full 200-session run takes roughly **35 seconds** and is deterministic — identical code always
produces an identical score. Our agent reproduces:

| Metric | Value |
|---|---|
| Hit Rate@10 | 0.98 |
| MRR | 0.864018 |
| MTTC | 2.85 |
| **TechnicalScore** | **0.912205** |

Per-scenario Hit Rate@10: boundary 1.0 · browsing 0.9875 · buying 0.975 · intent_override 0.9667.
The committed snapshot of this run is `results_after_fieldfactors.json`.

For reference, the unmodified weak BM25 starter scores Hit Rate@10 `0.125`, MRR `0.068034`, and
MTTC `9.81` — see `docs/baseline_results.json`.

Edit `starter/agent.py` to implement your system. Do not edit the evaluator or public labels when
reporting your local score.

## Agent Interface

```python
class Agent:
    def reset(self, session_id: str, user_profile: dict) -> None:
        ...

    def respond(self, session_id: str, user_message: str, turn: int, top_k: int) -> dict:
        return {
            "message": "Do you have a material preference?",
            "ask_attribute": "material",
            "recommendations": [
                {"parent_asin": "B000..."},
                {"parent_asin": "B001..."}
            ],
            "usage": {"prompt_tokens": 120, "completion_tokens": 30}
        }
```

`ask_attribute` is one of `category`, `material`, `color`, `size`, `style`, `brand`, `budget`, `feature`, `use_case`, `other`, or `null`. See `docs/agent_api_contract.json`.

## Technical Metrics

- **Hit Rate@10:** fraction of sessions that find the target within 10 turns.
- **MRR:** mean reciprocal rank of the target; a miss contributes zero.
- **MTTC:** mean first-hit turn; a miss is assigned turn 11.
- **Reported token usage:** prompt and completion tokens returned by the team's model client.

```text
TechnicalScore = 0.50 × HitRate@10 + 0.30 × MRR + 0.20 × Efficiency
Efficiency = clip((11 - MTTC) / 10, 0, 1)
```

Only exact `parent_asin` equality produces a hit. Core metrics are also reported by scenario.

## Model Choice and Cost

Organizer policy: teams may use any legally accessible LLM API or local model, must manage their own
credentials, and must never commit API keys. Model choice, estimated cost, token usage, and latency
must be disclosed. Token usage is a feasibility metric, not part of the core technical score.

### Our disclosure

**No model. No network. No credentials. No cost.**

| Item | Value |
|---|---|
| LLM / external API | **None** — no API calls of any kind |
| Network access required | **None.** Runs fully offline |
| API keys / environment variables | **None** |
| Estimated model cost | **$0.00** |
| Reported token usage | `0` prompt, `0` completion — honestly zero, not unreported |

Retrieval is entirely local: an in-memory SQLite **FTS5** index plus offline **LSA** embeddings
(TF-IDF + Truncated SVD) computed at construction from the catalog itself. Nothing is downloaded
at runtime and no pretrained model weights are loaded from disk.

Because official judging may disable network access, there is **no online path to fall back from** —
the offline path is the only path. That is an argument, though, not evidence, so we test it:

```bash
py tools/offline_check.py
```

This replays all 200 sessions with every socket operation in the process hard-blocked (a
`sys.addaudithook` hook plus monkeypatched entry points, installed *before* numpy/scipy/scikit-learn
are imported) and compares the outcome against the committed reference run session by session. It
exits non-zero unless the score matches to six decimals, all 200 sessions have an identical hit turn
and rank, and no turn was answered from a fallback. Current result: **identical, 0/200 sessions
differing, 0 fallback turns.**

The agent also **degrades rather than fails**. `respond()` never raises: inputs are coerced, the
response shape is enforced against the contract, and any internal fault falls back to the session's
last good recommendations, then to a catalog-wide slate, then to an empty list. A missing catalog or
a Python built without FTS5 degrades the agent instead of aborting the run. See
`docs/features/11-offline-safe-fallback.md`, and:

```bash
py -m unittest discover -s tests -t . -v
```

### Measured latency

From the 200-session run (566 `respond()` calls), on the machine described above. Regenerate
these numbers at any time with:

```bash
py tools/feasibility_report.py
```

| Stage | Time |
|---|---|
| `Agent()` construction (FTS5 index + LSA embeddings) | **~13.5 s**, one-time at startup |
| `respond()` — mean | **~55 ms** |
| `respond()` — median | **~44 ms** |
| `respond()` — p95 | **~130 ms** |
| `respond()` — max | 240–400 ms |
| Full 200-session run, end to end | **~35 s** |

Construction cost is paid once per process, not per session or per turn.

Unlike the score, **these timings are not deterministic** — they move with machine load and cache
state. Figures above are typical of three consecutive runs on the development machine; the mean was
stable to within ~3 ms across them, while the single worst-case turn ranged 240–400 ms. Treat them
as representative, not exact. The score, by contrast, reproduces bit-for-bit.

## Files

Our agent — the submitted system:

```text
starter/agent.py            orchestration + the official reset()/respond() contract
starter/retrieval.py        FTS5 index, query routes, RRF fusion
starter/dialog_state.py     per-session slots, evidence accumulation, question policy
starter/ranking.py          IDF coverage + phrase reranking over the fused candidate pool
starter/dense_retrieval.py  offline LSA (TF-IDF + Truncated SVD) embeddings
starter/offline.py          input coercion + the enforced turn_response shape
tools/offline_guard.py      hard network block, for proving the agent runs offline
tools/offline_check.py      full 200-session replay with the network blocked
tests/test_offline_safety.py  offline-safety and response-contract tests
requirements.txt            pinned dependencies -- install before running
```

How it was built, feature by feature, each with a measured before/after score delta:

```text
docs/features/01-dual-track-intent-routing.md   Buying vs Browsing routing
docs/features/02-multi-route-retrieval.md       keyword + category routes, RRF fusion
docs/features/03-clarification-loop.md          targeted questions, evidence accumulation
docs/features/04-semantic-reranking.md          reranking the fused candidate pool
docs/features/05-rank-vs-turn-arbitrage.md      trading turns for rank
docs/features/06-phrase-retrieval.md            exact-phrase routes
docs/features/07-hybrid-dense-retrieval.md      dense LSA route (shipped flat, documented)
docs/features/08-feasibility-disclosure.md      latency / token / cost instrumentation
docs/features/09-optimization-headroom.md       where the remaining points are, and what is closed
docs/features/10-field-factor-calibration.md    field factors corrected to match the evidence source
docs/features/11-offline-safe-fallback.md       offline enforcement + fail-soft respond()
docs/demo-script.md                             narration script for the demo video
results_after_fieldfactors.json                 committed snapshot of the score of record
```

Development tooling (not part of the agent, not needed to reproduce the score):

```text
tools/score_delta.py        markdown before/after delta table for a feature doc
tools/feasibility_report.py regenerates the latency / token / cost tables below
tools/sweep_constants.py    coordinate-descent sweep over the agent's tuned constants
```

Organizer-provided, unmodified:

```text
data/catalog.jsonl                50,000 frozen products (downloaded separately)
data/public_set.jsonl             200 labeled development sessions
docs/competition_specification.md participant rules and evaluation protocol
docs/submission_rules.md          participant submission requirements
docs/agent_api_contract.json      machine-readable Agent contract
docs/evaluation_config.json       scoring configuration
docs/baseline_results.json        reproducible weak-starter reference score
evaluator/local_evaluator.py      public-set simulator and scorer
```

## Judging and Submission Policy

- Participant submission requirements: `docs/submission_rules.md`
- Evaluation protocol and metrics: `docs/competition_specification.md`

Organizer-side judging runbooks referenced in the original starter README
(`organizer/JUDGING_RUNBOOK.md` and similar) are not distributed to participants and are not
present in this repository.

## Data Source

The catalog and sessions are derived from Amazon Reviews 2023 by McAuley Lab, UCSD. See `DATA_ATTRIBUTION.md` before using or redistributing the data.
Sessions are sampled deterministically from the official Clothing 5-core leave-last-out split and joined to the frozen catalog.
