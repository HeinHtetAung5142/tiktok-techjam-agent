# Shopping Copilot — TikTok TechJam 2026 report

Method, model choice, cost, latency, limitations and team contributions.
**Installing and running it is in [`README.md`](README.md).**

A multi-turn conversational shopping agent that finds a **hidden** target product inside a
50,000-item catalog, in as few turns as possible, by asking targeted clarifying questions
and absorbing the answers into a ranked search.

**It runs fully offline: no LLM call, no API key, no network access, and no pretrained
weights loaded from disk.**

| Metric | Weak BM25 starter | **This agent** |
|---|---|---|
| Hit Rate@10 | 0.125 | **0.98** |
| MRR | 0.068034 | **0.864018** |
| MTTC (mean turns to convert) | 9.81 | **2.85** |
| **TechnicalScore** | **0.10671** | **0.912205** |

Per scenario Hit Rate@10: boundary `1.0` · browsing `0.9875` · buying `0.975` ·
intent_override `0.9667`. 196 of 200 sessions hit, **162 of them at rank 1**.

---

## 1. Method

### Architecture

```text
agent.py               entry point — exports Agent for the harness
src/agent.py           orchestration + the official reset()/respond() contract
src/retrieval.py       FTS5 index, query routes, weighted RRF fusion
src/dialog_state.py    per-session slots, evidence accumulation, question policy
src/ranking.py         IDF coverage + phrase reranking over the fused pool
src/dense_retrieval.py offline LSA (TF-IDF + Truncated SVD) embeddings
src/facets.py          generic attribute facets — free-form input only
src/llm.py             OPTIONAL hosted model client; off by default, stdlib-only
src/env_file.py        .env scaffolding; never imported by the scored path
```

All 50,000 products are loaded into an in-memory SQLite **FTS5** table at construction.
Columns are weighted separately at query time via `bm25()`; nothing is read from disk after
startup, and no index is shipped as an asset.

### The four ideas that produced most of the score

**Ask, then absorb.** The simulated customer discloses a constraint only when you name the
attribute it belongs to, so we ask on every turn — a question is free, since
recommendations are scored regardless. `other` leads the ask order because it is the only
attribute that cannot whiff. Every disclosure accumulates as retrieval evidence, oldest
first, so the query spans the whole conversation rather than the latest message.

**Multi-route retrieval, then rerank.** Up to six routes run per turn — whole-catalog
keyword, category-scoped, up to 12 IDF-weighted exact-phrase routes, a dense LSA route, and
two free-form-only routes — merged by weighted Reciprocal Rank Fusion (`weight / (60 +
rank)`). Fusion does not decide the final order; it generates a 120-candidate pool that a
reranker reorders on IDF-weighted term coverage and intact-phrase matching. The premise is
that a shopper quotes the language of the product they want, and the catalog is where that
language came from. The phrase and dense routes are deliberately left unfiltered by hard
constraints, so a wrong filter cannot suppress the one route that identifies the product.

**Trade turns for rank.** The evaluator freezes the target's rank the moment it appears in
the Top 10, so surfacing it early at a bad rank is a *cost*, not a win. One turn of delay
costs 0.0001 of TechnicalScore while one unit of reciprocal rank is worth 0.0015 — so turns
1–2 disclose a single recommendation and the list widens as evidence arrives
(`DISCLOSURE_SCHEDULE = (1, 1, 4, 8, 10)`).

**Fail soft, everywhere.** A raised exception or malformed output is scored as an outright
miss, so the reranker, the dense route, the phrase routes and the optional model each carry
their own `try/except`: a fault costs that component only, never the turn.

### Reading the evaluator was worth more than any model

The simulator's disclosable pool is **four constraints total** (`hard_constraints =
cleaned[:2]`, `soft_preferences = cleaned[2:4]`), and it returns at most two per turn — so
two `other` questions exhaust the customer completely by turn 3. No dialog-side strategy can
beat that, and no session needs to run past turn 4. Given fixed and complete evidence by
turn 3, **the only remaining lever in the whole system is ranking quality.** That conclusion
shaped every feature after it, and it came from reading the evaluator, not from tuning.

---

## 2. Feasibility disclosure

### Model choice and cost

**The submitted configuration makes no model call.**

| Item | Value |
|---|---|
| LLM / external API | **None** |
| Network access required | **None** — runs fully offline |
| API keys / environment variables | **None required** |
| Estimated model cost | **$0.00** |
| Reported token usage | `0` prompt, `0` completion — honestly zero, not unreported |

`usage` reports literal zeros rather than being omitted, so the disclosure is explicit
("we used no tokens") rather than merely absent ("they didn't say").

**Why no model.** The disclosures the simulator emits are near-verbatim text from the
target product's own `features`/`details`. Getting the customer to speak is getting them to
quote the answer, and the winning move is to feed that text straight into lexical retrieval
— not to paraphrase it through a model that can only add noise and latency. We measured
this rather than assuming it; see "what did not work" below.

**The optional route, and why it is off.** `src/llm.py` contains an OpenAI-compatible
client (stdlib `urllib.request`, no added dependency) used during development for
understanding free-form human input in our local demo UI. It requires **both**
`SHOPPING_COPILOT_API_KEY` and `SHOPPING_COPILOT_LLM` to be set — neither alone does
anything, and an unrecognized mode fails closed to `off`. No key is in this bundle;
configuration is environment-only.

**Offline fallback, measured rather than asserted.** Because official judging may disable
the network, we ran the full 200 sessions with the model configured *and every socket
raising*. The result document was byte-identical to the score of record. A circuit breaker
latches the client off for the process after 2 consecutive connection failures, 3 failures
of any kind, or 3 consecutive successes slower than 4.5 s — so a dead endpoint costs one
timeout, not one per turn. Every failure mode (timeout, HTTP error, bad JSON, no network)
returns `None` and falls through to the pipeline that scores 0.912205 on its own.

### Latency

Latency is **not** deterministic — unlike the score, it moves with machine load and cache
state, and varies substantially across hardware. On the development machine (Windows 11,
Python 3.14.7):

| Stage | Time |
|---|---|
| `Agent()` construction (FTS5 index + LSA embeddings) | **21–30 s**, one-time at startup |
| `respond()` — mean | **58–144 ms** |
| `respond()` — p95 | **131–324 ms** |
| Full 200-session run, end to end | **~25–40 s** |

Construction is paid once per process, never per session or per turn. Earlier runs on a
faster machine recorded ~6 s construction and ~31 ms mean; the spread is hardware, not
behaviour. Latency is deliberately **not** returned in the response payload: `turn_response`
and `usage` both set `"additionalProperties": false` in the contract, so an extra key would
be malformed output — scored as a miss. It is exposed on the agent instead, via
`Agent.latency_stats()` and `Agent.model_stats()`.

---

## 3. Limitations

**The four remaining misses are information-theoretically unreachable, not a retrieval
failure.** `public_0020`, `public_0087`, `public_0144`, `public_0174` disclose constraints
shared with thousands of products — `public_0087` offers only "cotton" (df 9,775),
"100% Cotton" (3,770), "Imported" (15,300), "Button closure" (2,391). Nothing lexical *or*
dense separates a target from 3,000 items when the evidence is identical across all of
them. We verified this rather than assuming it: a conjunction route narrowing to 100
candidates still could not order them, and `public_0020` moves from rank 15 to 14 when the
hard filter is removed entirely. **A better retriever cannot fix these.**

**Hit Rate 0.98 is 0.975 plus one marginal session.** `public_0145` converts at **rank 10
on turn 5** — one position from being a miss again. We report the number honestly rather
than treating it as robust.

**The real headroom is ranking precision, and we did not get to it.** Of 196 hits, 162 land
at rank 1 and 34 land below it, most on turns 3–4 where the disclosure schedule widens.
Promoting all 34 to rank 1 would be worth **+0.0348**, more than twice the entire miss pool.
The diagnosed cause is real and untried: `Reranker._coverage` measures recall with **no
length normalization**. It asks *how much of the customer's evidence is in this product* and
never *how much of this product is the customer's evidence*, so a sprawling listing that
happens to contain "100% Cotton" among forty other features scores identically to a focused
listing where those are the whole product. Adding a precision term is the single
highest-value thing left; the realistic ceiling with the current miss set is ~0.947.

**`respond()` has no broad exception guard.** `observe(None, 1)` raises `TypeError`, and a
non-`int` `turn` raises too. The public set never triggers either and the organizer's
evaluator catches exceptions anyway — so this is insurance against a stricter hidden
harness, not a known loss. It is recorded as a decision, not a discovery, and our test
suite reports it as XFAIL rather than quietly passing.

**Turns 1–2 return a single recommendation.** Deliberate and contract-legal — it buys rank,
per the arbitrage above — but it is thin UX and reads oddly in a live demo.

**The dialog layer has never been tested against a paraphrasing simulator.** The spec warns
the organizer may add natural-language paraphrasing, and the private set is 4× ours. We
modelled the *mechanism* ("ask a targeted question, absorb the answer into state") rather
than literal strings, and added a free-form path for human input, but that path has only
been exercised by people typing into our local UI.

### What we tried that did not work

Recording these matters as much as the wins:

- **Personalization from `user_profile` is worthless here, measured across all 200
  sessions.** `purchase_frequency` and `category_bucket` are constants;
  `average_prior_rating` and `rating_style` describe the *reviewer's temperament*, not the
  product. The only varying field, `preference_tags`, is degenerate — `fit` appears in
  163/200. A field in over half the sessions cannot separate one target from 50,000.
- **Dense retrieval shipped flat.** It earns its keep as a *route*, but blending LSA
  similarity into the reranker regressed MRR monotonically at every nonzero weight tested.
  LSA is a smoothed compression of the same term statistics coverage already uses.
- **Blending the fused BM25 order back in as a ranking prior cost 0.03–0.04.**
- **Retracting the withdrawn preference on an intent override cost −0.003875.** The
  "abandoned" attribute is generated from the target's own intent card, so it still
  describes the product we are hunting. Clearing the slots and letting the new message
  refill them is correct; deleting the old evidence is not.

---

## 4. Team and contributions

Work was split along the four pillars of the problem statement. Every feature carries a
measured before/after score delta in the project's feature log.

| Pillar | Owner | Scope |
|---|---|---|
| Retrieval & Routing | _TODO_ | FTS5 index, dual-track routing, multi-route retrieval and RRF fusion, phrase routes, dense LSA route |
| Dialog & Ranking | _TODO_ | Slot state and evidence accumulation, clarification policy, rank-vs-turn arbitrage, reranker and field-factor calibration |
| Integration | _TODO_ | Agent contract wiring, optional model client and circuit breaker, offline fallback, local demo UI, submission packaging |
| Coordination & Evaluation | _TODO_ | Evaluator analysis, score ratchet and verification suites, feasibility measurement, documentation and reporting |

> Fill in the owner column before submitting.

---

## 5. Reproducibility summary

| Requirement | This submission |
|---|---|
| Python version | 3.10+ (verified on 3.14.7 and 3.12.0) |
| Dependency install | `pip install -r requirements.txt` |
| One command to run | `py -m evaluator.local_evaluator` |
| Environment variables | none required |
| Network access | none required; no call is made in this configuration |
| External services | none |
| Determinism | full — identical code scores identically, bit for bit |

## Data source

Catalog and sessions derive from **Amazon Reviews 2023** by McAuley Lab, UCSD. The dataset
is read-only: we never modify the catalog or invent ASINs, and agent code never reads
`ground_truth`.
