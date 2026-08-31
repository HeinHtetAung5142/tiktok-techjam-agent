# Shopping Copilot — TikTok TechJam 2026

A multi-turn conversational shopping agent that finds a **hidden** target product inside a
50,000-item catalog, in as few turns as possible, by asking targeted clarifying questions
and absorbing the answers into a ranked search.

**It runs fully offline: no LLM call, no API key, no network access, and no pretrained
weights loaded from disk.** Retrieval is an in-memory SQLite FTS5 index plus LSA
embeddings fitted at startup from the catalog itself.

| Metric | Weak BM25 starter | **This agent** |
|---|---|---|
| Hit Rate@10 | 0.125 | **0.98** |
| MRR | 0.068034 | **0.864018** |
| MTTC (mean turns to convert) | 9.81 | **2.85** |
| **TechnicalScore** | **0.10671** | **0.912205** |

Per scenario Hit Rate@10: boundary `1.0` · browsing `0.9875` · buying `0.975` ·
intent_override `0.9667`. 196 of 200 sessions hit, **162 of them at rank 1**.

---

## 1. Project overview

### The problem

Each session the agent receives an anonymized `user_profile` and a simulated customer
message. On every turn it may ask one clarifying question, return a ranked list of
`parent_asin` values, or both. The session ends the moment the target appears in the
scored Top 10, or after turn 10. Only exact `parent_asin` equality counts.

```text
TechnicalScore = 0.50 × HitRate@10 + 0.30 × MRR + 0.20 × Efficiency
Efficiency     = clip((11 − MTTC) / 10, 0, 1)
```

That weighting is the priority order: **find it at all → rank it near the top → get there
in fewer turns.**

### How it works

Six modules, no network on the scored path:

```text
starter/agent.py            orchestration + the official reset()/respond() contract
starter/retrieval.py        FTS5 index, six query routes, weighted RRF fusion
starter/dialog_state.py     per-session slots, evidence accumulation, question policy
starter/ranking.py          IDF coverage + phrase reranking over the fused pool
starter/dense_retrieval.py  offline LSA (TF-IDF + Truncated SVD) embeddings
starter/facets.py           generic attribute facets — free-form input only
```

The four ideas that produced most of the score:

**Ask, then absorb.** The simulated customer discloses a constraint only when you name the
attribute it belongs to, so we ask on every turn — a question is free, since
recommendations are scored regardless. `other` leads the ask order because it is the only
attribute that cannot whiff. Every disclosure accumulates as retrieval evidence, oldest
first, so the query spans the whole conversation rather than the latest message.

**Multi-route retrieval, then rerank.** Up to six routes run per turn — whole-catalog
keyword, category-scoped, up to 12 IDF-weighted exact-phrase routes, a dense LSA route,
and two free-form-only routes — merged by weighted Reciprocal Rank Fusion. Fusion does not
decide the final order; it generates a 120-candidate pool that a reranker reorders on
IDF-weighted term coverage and intact-phrase matching. The premise is that a shopper
quotes the language of the product they want, and the catalog is where that language came
from.

**Trade turns for rank.** The evaluator freezes the target's rank the moment it appears in
the Top 10, so surfacing it early at a bad rank is a *cost*. One turn of delay costs
0.0001 of TechnicalScore while one unit of reciprocal rank is worth 0.0015 — so turns 1–2
disclose a single recommendation and the list widens as evidence arrives
(`DISCLOSURE_SCHEDULE = (1, 1, 4, 8, 10)`).

**Fail soft, everywhere.** A raised exception or malformed output is scored as an outright
miss, so the reranker, the dense route, the phrase routes and the optional model each have
their own `try/except`: a fault costs that component only, never the turn.

### Reading the evaluator was worth more than any model

The simulator's disclosable pool is **four constraints total** (`hard_constraints =
cleaned[:2]`, `soft_preferences = cleaned[2:4]`), and it returns at most two per turn — so
two `other` questions exhaust the customer completely by turn 3. No dialog-side strategy
can beat that, and no session needs to run past turn 4. Given fixed and complete evidence
by turn 3, **the only remaining lever in the whole system is ranking quality.** That
conclusion shaped every feature after it, and it came from reading
`evaluator/local_evaluator.py`, not from tuning.

---

## 2. Setup and installation

**Python 3.10+.** Verified on **3.14.7** and **3.12.0**; 3.10/3.11 are the declared floor
but untested — treat them as expected-to-work.

### Step 1 — Install dependencies (required)

```bash
pip install -r requirements.txt
```

This installs `numpy`, `scikit-learn` and `scipy`, used by the dense-retrieval route. They
are the project's **only** third-party dependencies; everything else is standard library.

**This step is required, and the failure mode is quiet.** `CatalogIndex._build_dense_index`
imports lazily inside a broad `try/except` so a missing stack degrades to sparse-only
retrieval rather than crashing. The only symptom is one line on stderr:

```text
[dense_retrieval] disabled: ModuleNotFoundError("No module named 'numpy'")
```

Miss that line and the run looks normal while scoring a different agent — sparse-only
returns **0.909858** against the **0.912205** of record.

### Step 2 — Download the catalog

Download `catalog.jsonl.gz` from the GitHub Release attached to this repository, verify it
against the published `SHA256SUMS`, then:

```bash
gzip -dk catalog.jsonl.gz
mv catalog.jsonl data/catalog.jsonl
```

`data/catalog.jsonl` must exist with 50,000 rows before anything will run.

### Step 3 — Nothing else

No environment variables. No API keys. No network access. No config file.

> **Windows note.** `python` and `python3` often resolve to the Microsoft Store stub and
> fail. Use the real launcher — `py` — which is what every command below assumes. On
> macOS/Linux substitute `python3`; the module paths are identical.

---

## 3. Steps to reproduce your results

### The one command that matters

```bash
py -m evaluator.local_evaluator
```

Writes per-session results and aggregate metrics to `results.json` and prints the
aggregate plus the per-scenario breakdown. Expect:

| Metric | Value |
|---|---|
| Hit Rate@10 | 0.98 |
| MRR | 0.864018 |
| MTTC | 2.85 |
| **TechnicalScore** | **0.912205** |

**Runs are deterministic.** `materialize_hidden_fields` regenerates intent cards with a
seeded RNG, so identical code always scores identically, bit for bit. A changed score means
a changed agent — never run-to-run noise. The committed snapshot of this exact run is
`results/results_after_fieldfactors.json`.

### Verifying you reproduced it exactly

```bash
py tools/score_ratchet.py
```

This is the project's merge rule — **TechnicalScore may rise or stay level, never fall** —
and it is the fastest way to confirm your run matches ours. It exits non-zero if the score
fell, and distinguishes *byte-identical* (the sessions array matches exactly) from merely
*score-equal*, because offsetting session movements can hide a regression that the
800-session private set would not forgive.

### The full test suite

```bash
py tools/verify_features.py   # 90 feature / contract / isolation checks
py tools/verify_llm.py        # 96 optional-model checks; stubs HTTP, needs no key
```

Both exit non-zero on a regression and need neither network nor credentials. Two
robustness checks report as **XFAIL** — documented known gaps (see §4), not regressions.

`verify_features.py` includes the invariant the whole design rests on: it asserts that a
full scored run makes **566 `observe()` calls and 0 `_observe_freeform` calls**, proving
the human-input code paths are unreachable while scoring.

### Building the submission bundle

```bash
py tools/build_submission.py
```

Regenerates `submission/` from `starter/` — the entry file, `src/`, `requirements.txt` and
the report — and then **proves it is the agent we measured**: it re-runs all 200 sessions
with the bundle ahead of the repo on `PYTHONPATH` and requires the result to be
byte-identical to `results/results_after_fieldfactors.json`. The bundle is never
hand-edited; to change it, change the source in `starter/`, `docs/submission_setup.md` or
`docs/submission_report.md` and rebuild.

`starter/` cannot simply be renamed into the layout `docs/submission_rules.md` recommends,
because `evaluator/local_evaluator.py:12` imports `starter.agent` and the evaluator is
organizer-owned. The bundle carries a four-line `starter/agent.py` shim so both import
paths resolve to the one implementation in `src/`.

### Other useful commands

```bash
py tools/feasibility_report.py                    # regenerate latency / token / cost tables
py tools/score_delta.py <before.json> <after.json>  # markdown delta table
py tools/sweep_constants.py --list                # show the tunable axes
py -m webui.server                                # optional local UI, http://127.0.0.1:8000
```

### Measured latency

Latency is **not** deterministic — unlike the score, it moves with machine load and cache
state, and varies substantially across hardware. On the development machine (Windows 11,
Python 3.14.7) across several runs this session:

| Stage | Time |
|---|---|
| `Agent()` construction (FTS5 index + LSA embeddings) | **21–30 s**, one-time at startup |
| `respond()` — mean | **58–144 ms** |
| `respond()` — p95 | **131–324 ms** |
| Full 200-session run, end to end | **~25–40 s** |

Construction is paid once per process, never per session or per turn. Earlier runs on a
faster machine recorded ~6 s construction and ~31 ms mean; the spread is hardware, not
behaviour. **Regenerate the numbers for your own machine rather than trusting these** —
`py tools/feasibility_report.py`.

### Model choice and cost — our disclosure

**The submitted configuration makes no model call.**

| Item | Value |
|---|---|
| LLM / external API | **None** |
| Network access required | **None** — runs fully offline |
| API keys / environment variables | **None required** |
| Estimated model cost | **$0.00** |
| Reported token usage | `0` prompt, `0` completion — honestly zero, not unreported |

An optional hosted model exists in the repo (`starter/llm.py`, OpenAI-compatible, stdlib
`urllib.request`, no added dependency). It requires **both** `SHOPPING_COPILOT_API_KEY`
and `SHOPPING_COPILOT_LLM` — neither alone does anything, and an unrecognized mode fails
closed to `off`. No key is in the repo; configuration is environment-only.

Because official judging may disable the network, the fallback was **measured, not
asserted**: a full run with the model configured *and every socket raising* produces a
results document byte-identical to `results/results_after_fieldfactors.json`. Setup:
`docs/LLM_SETUP.md`. Rationale and measurements: `docs/features/13-optional-llm.md`.

---

## 4. Limitations, and what we would improve with more time

### What we know is wrong

**The four remaining misses are information-theoretically unreachable, not a retrieval
failure.** `public_0020`, `public_0087`, `public_0144`, `public_0174` disclose constraints
shared with thousands of products — `public_0087` offers only "cotton" (df 9,775),
"100% Cotton" (3,770), "Imported" (15,300), "Button closure" (2,391). Nothing lexical *or*
dense separates a target from 3,000 items when the evidence is identical across all of
them. We verified this rather than assuming it: a conjunction route narrowing to 100
candidates still could not order them, and `public_0020` moves from rank 15 to 14 when the
hard filter is removed entirely. **A better retriever cannot fix these.**

**The real headroom is ranking precision, and we did not get to it.** Of 196 hits, 162 land
at rank 1 and **34 land at ranks 2–10** — 30 of those on turns 3–4, exactly where the
disclosure schedule widens 1 → 4 → 8. Promoting all 34 to rank 1 is worth **+0.0348**,
more than three times the entire miss pool (+0.0160). The diagnosed cause is real and
untried: `Reranker._coverage` measures recall with **no length normalization**. It asks
*how much of the customer's evidence is in this product* and never *how much of this
product is the customer's evidence*, so a sprawling 700-token listing that happens to
contain "100% Cotton" among forty other features scores identically to a focused listing
where those are the whole product. **Adding a precision term is the single highest-value
thing left**, and the realistic ceiling with the current miss set is ~0.947.

**`respond()` has no broad exception guard.** `observe(None, 1)` raises `TypeError`, and a
non-`int` `turn` raises too. The public set never triggers either and the organizer's
evaluator catches exceptions anyway — so this is insurance against a stricter hidden
harness, not a known loss. It is recorded as a decision, not a discovery, and reported as
XFAIL by the test suite.

**Turns 1–2 return a single recommendation.** Deliberate and contract-legal (it buys rank,
see above), but it is thin UX and reads oddly in a live demo.

### What we tried that did not work

Recording these matters as much as the wins:

- **Personalization from `user_profile` is worthless here, measured across all 200
  sessions.** `purchase_frequency` is a constant; `category_bucket` is a constant;
  `average_prior_rating` and `rating_style` describe the *reviewer's temperament*, not the
  product. The only varying field, `preference_tags`, is degenerate — `fit` appears in
  163/200. A field in over half the sessions cannot separate one target from 50,000.
- **Dense retrieval shipped flat.** It earns its keep as a *route*, but blending LSA
  similarity into the reranker regressed MRR monotonically at every nonzero weight tested.
  LSA is a smoothed compression of the same term statistics coverage already uses.
- **Blending the fused BM25 order back in as a ranking prior cost 0.03–0.04.**

### Known rough edges in the free-form (demo) path

These cannot affect the score — the code is unreachable while scoring — but they are real:

- **Facet demotion only fires on an explicit opposite assertion.** A women's dress whose
  title never says "women" is not demoted, so one still reaches rank 9 on a men's query.
  Fixing it needs a category signal, not a title token.
- **Exclusions over-fire on noisy material fields.** "skinny jeans, not polyester" returns
  a tank top first, because most stretch jeans list polyester in the blend.
- **The facet vocabulary is curated, not mined** — ten groups covering the common apparel
  axes. A value nobody listed falls back to ordinary keyword evidence.

### Given more time

1. **Add a precision term to `_coverage`** (or normalize by document length). The one open
   lever with a real mechanism behind it, worth up to +0.0348. Measure it; expect a handful
   of sessions, not all 34.
2. **Move the filler-word fix onto the scored path.** On a live free-form query,
   `under 50 dollars` carried 33% of the query's total IDF mass on words describing no
   product — `dollars` (IDF 6.79) outweighed `tshirt` (4.73). We fixed this for human input
   only; whether it helps the *simulator's* disclosures is an unmeasured, plausible win.
3. **Harden `respond()`** against malformed input, then re-run the ratchet to confirm
   0.912205 is unchanged.
4. **Mine the facet vocabulary from the catalog** instead of curating it, so new attribute
   groups appear without anyone editing a dictionary.
5. **Paraphrase-proof the dialog layer.** The spec warns the organizer may add natural-
   language paraphrasing, and the private set is 4× ours. Our regexes model the mechanism
   ("ask a targeted question, absorb the answer") rather than literal strings, but the
   free-form path has never been tested against a paraphrasing simulator — only against
   humans typing into the WebUI.

### The one thing we would keep

Every feature in `docs/features/` carries a measured before/after delta, including the flat
and negative results. That discipline is why we could say "dense retrieval is flat, ship it
as a route only" instead of arguing about it, and why the score never silently regressed
across 16 features. `tools/score_ratchet.py` now enforces it mechanically.

---

## 5. Team and contributions

Work was split along the four pillars of the problem statement.

| Pillar | Owner | Scope |
|---|---|---|
| Retrieval & Routing | _TODO_ | FTS5 index, dual-track routing, multi-route retrieval and RRF fusion, phrase routes, dense LSA route |
| Dialog & Ranking | _TODO_ | Slot state and evidence accumulation, clarification policy, rank-vs-turn arbitrage, reranker and field-factor calibration |
| Integration | _TODO_ | Agent contract wiring, optional model client and circuit breaker, offline fallback, local demo UI, submission packaging |
| Coordination & Evaluation | _TODO_ | Evaluator analysis, score ratchet and verification suites, feasibility measurement, documentation and reporting |

> **Fill in the owner column before submitting.** The same table appears in
> `docs/submission_report.md`, which becomes `submission/REPORT.md` — update both, then
> re-run `py tools/build_submission.py`.

---

## Agent interface

```python
class Agent:
    def reset(self, session_id: str, user_profile: dict) -> None: ...

    def respond(self, session_id: str, user_message: str, turn: int, top_k: int) -> dict:
        return {
            "message": "Do you have a material preference?",
            "ask_attribute": "material",
            "recommendations": [{"parent_asin": "B000..."}, {"parent_asin": "B001..."}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0},
        }
```

`ask_attribute` is one of `category`, `material`, `color`, `size`, `style`, `brand`,
`budget`, `feature`, `use_case`, `other`, or `null`. Recommendations are ordered best-first;
only the first 10 valid, unique, in-catalog ids are scored. See
`docs/agent_api_contract.json`.

## Repository layout

```text
starter/                    the agent (see §1) — the source of truth for the bundle
submission/                 GENERATED submission bundle; rebuild, never hand-edit
requirements.txt            pinned dependencies — install before running

tools/build_submission.py   builds submission/ and proves it byte-identical
tools/score_ratchet.py      refuses a change that lowers the score
tools/verify_features.py    90 feature / contract / isolation checks
tools/verify_llm.py         96 optional-model checks; stubs HTTP, needs no key
tools/feasibility_report.py regenerates the latency / token / cost tables
tools/score_delta.py        markdown before/after delta table for a feature doc
tools/sweep_constants.py    coordinate-descent sweep over the tuned constants
tools/llm_smoke.py          checks a real API key end to end
tools/benchmark_llms.py     compares candidate models on the free-form path

docs/submission_setup.md    setup instructions; becomes submission/README.md
docs/submission_report.md   the required report; becomes submission/REPORT.md
docs/features/              every feature 01–16, each with a measured score delta
docs/LLM_SETUP.md           optional-model setup, per provider
docs/demo-script.md         narration script for the demo video
results/                    committed milestone snapshots (results_after_*.json)

webui/                      optional local UI for hand-driven sessions; stdlib only,
                            adds no dependency, delete the directory to remove it

data/catalog.jsonl          50,000 frozen products (downloaded separately)
data/public_set.jsonl       200 labeled development sessions
evaluator/local_evaluator.py  organizer-provided simulator and scorer — unmodified
docs/competition_specification.md · submission_rules.md · agent_api_contract.json ·
docs/evaluation_config.json · docs/baseline_results.json — organizer-provided, unmodified
```

## Data source

Catalog and sessions derive from **Amazon Reviews 2023** by McAuley Lab, UCSD. See
`DATA_ATTRIBUTION.md` before using or redistributing. Sessions are sampled deterministically
from the official Clothing 5-core leave-last-out split and joined to the frozen catalog.
The dataset is read-only: we never modify the catalog or invent ASINs, and agent code never
reads `ground_truth`.
