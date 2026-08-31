# CLAUDE.md

Guidance for Claude Code (and teammates) working in this repository.

## Project

TikTok TechJam 2026 — **Shopping Copilot track**. We build a multi-turn conversational shopping
agent that finds a *hidden* target product inside a Top-10 recommendation list, in as few turns as
possible.

Each session the agent receives an anonymized `user_profile` and a simulated customer message. It
may ask a clarifying question, return a ranked list of catalog `parent_asin` values, or both. The
session ends when the target appears in the scored Top 10, or after turn 10.

- Catalog: 50,000 frozen products, Amazon Reviews 2023 `Clothing_Shoes_and_Jewelry`.
- Local dev set: 200 labeled sessions (`data/public_set.jsonl`). The organizer holds 800 private
  sessions for final judging.
- Only `parent_asin` is scored, by **exact string equality**. Nothing else counts.

**Deadline: 2026-09-01, 12:00 +08.**

### Scoring

```text
TechnicalScore = 0.50 x HitRate@10 + 0.30 x MRR + 0.20 x Efficiency
Efficiency     = clip((11 - MTTC) / 10, 0, 1)
```

- **HitRate@10** — fraction of sessions where the target is found within 10 turns.
- **MRR** — mean reciprocal rank of the target; a miss contributes 0.
- **MTTC** — mean first-hit turn; **a miss is charged as turn 11**.

That weighting is the priority order: *find it at all* → *rank it near the top* → *get there in
fewer turns*. Token usage and latency are disclosed feasibility metrics; they do **not** enter the
score.

Scenario mix (identical in both splits): 40% buying, 40% browsing, 15% intent_override, 5% boundary.
Metrics are also reported per scenario — always read the breakdown, not just the aggregate.

### Score of record

| Metric | Baseline (`docs/baseline_results.json`) | Current (`results/results_after_fieldfactors.json`) |
|---|---|---|
| HitRate@10 | 0.125 | 0.98 |
| MRR | 0.068034 | 0.864018 |
| MTTC | 9.81 | 2.85 |
| **TechnicalScore** | **0.10671** | **0.912205** |

Per scenario HitRate@10, current: boundary 1.0 · browsing 0.9875 · buying 0.975 ·
intent_override 0.9667. `boundary` MRR is a perfect 1.0.

**Treat 0.912205 as 0.906791 plus a marginal rescue, not as a solid +0.005.** Feature 10 raised
`features`/`details` field factors to parity with `title` -- correct on mechanism, since the
simulator generates every disclosure from those two fields -- but the public-set gain rests on five
sessions: `public_0145` scraping in at **rank 10 on turn 5**, plus four rank improvements against
compensating drift elsewhere (12 sessions improved, 11 worsened). Read
`docs/features/10-field-factor-calibration.md` before quoting the number.

**Every cheap term is spent.** MRR went 0.652 → 0.852 by trading turns for rank
(`docs/features/05-rank-vs-turn-arbitrage.md`) and plateaus there; phrase retrieval plus two
constraint-extraction bug fixes took HitRate to 0.975
(`docs/features/06-phrase-retrieval.md`); feature 10 then took it to 0.98. 162 of 196 hits land at
rank 1. Hybrid/dense retrieval
(`docs/features/07-hybrid-dense-retrieval.md`) shipped as a route only — TechnicalScore moved
-0.00049, inside the noise floor — after measuring that blending it into the reranker regressed
MRR monotonically at every nonzero weight tested. Read that doc before touching either number
above; the plateau is real and measured, not just asserted.

**Where the remaining points actually are** (measured, `docs/features/09-optimization-headroom.md`):
195 sessions hit, but only 161 at rank 1 — 34 land at ranks 2–8, and **all 34 hit on turn 3 or 4,
exactly where `DISCLOSURE_SCHEDULE` widens 1 → 4 → 8.** Turns 1–2 disclose a single slot and are
84/84 at rank 1.

| Pool | Worth in TechnicalScore |
|---|---|
| 34 hits at rank 2–8 → rank 1 | **+0.0348** |
| 4 misses → found | +0.0100 (HitRate) + 0.0060 (MRR) |

So the realistic ceiling with the current miss set is **~0.947**, against 0.912205 today, and the
rank-2-to-8 pool is worth over 2x the entire miss pool. **That pool has now been chased, with the
one mechanism anybody had for it, and it did not move** — see feature 17 and Known gap 1. The
headroom is real; the route to it is not known. Treat ~0.947 as an upper bound nobody has a plan
for, not as a target.

**The 5 remaining misses are not a retrieval problem and cannot be fixed by a better retriever.**
Their disclosed constraints are shared with thousands of products — `public_0087` discloses only
"cotton" (df 9775), "100% Cotton" (3770), "Imported" (15300), "Button closure" (2391). Nothing
lexical *or* dense separates a target from 3,000 items when the evidence is identical across all of
them. This was verified, not assumed: see the rejected conjunction route in feature 06. Do not
spend the remaining time here.

## Commands

```bash
py -m evaluator.local_evaluator                      # full 200-session run -> results.json
py -m evaluator.local_evaluator --output foo.json    # write elsewhere
py tools/score_delta.py <before.json> <after.json>   # markdown delta table for a feature doc
py tools/feasibility_report.py                       # latency / token / cost disclosure tables
py tools/sweep_constants.py --list                   # show the tunable axes
py tools/sweep_constants.py --axis A B                # coordinate-descent sweep over those axes
py tools/verify_features.py                          # 95 feature/contract/isolation checks
py tools/verify_llm.py                               # 96 LLM/env/breaker checks; stubs HTTP, no key
py tools/score_ratchet.py                            # REFUSES a change that lowers the score
py tools/build_submission.py                         # rebuild submission/ + prove it byte-identical
py tools/llm_smoke.py                                # check a real SiliconFlow key end-to-end
py tools/benchmark_llms.py --offline                 # compare models; --offline uses stubs
py tools/benchmark_llms.py --models A,B --sessions 50  # ...and score them against a control arm
```

**`score_ratchet.py` is the rule: TechnicalScore may rise or stay level, never fall.** Run it after
any change under `starter/`; non-zero exit means revert. It separates *byte-identical* (the strong
result, and the only honest way to claim "no effect on scoring") from merely *score-equal*, where
offsetting session movements can hide a regression the 800-session private set would not forgive.

**How a free-form feature is made score-safe.** The scored and human paths are disjoint — a full run
makes 566 `observe()` calls and **0** `_observe_freeform` calls. So every such capability enters as
an optional parameter that is *empty on every scored turn* (`avoid_terms`, `facets`, `extra_terms`),
guarded by `if x:`. `verify_features.py` asserts the 0-free-form-calls invariant, which is what keeps
that argument true as the regexes evolve rather than letting it decay into folklore.

`verify_features.py` and `verify_llm.py` are the pre-submission gate: both exit non-zero on a
regression, and neither needs network or credentials. **There are no XFAILs left**: the two
robustness checks that were reported as expected failures under known gap 4 became real passes in
feature 17, and the "respond() before reset() raises RuntimeError" check was inverted rather than
deleted, so the contract softening is recorded as a decision.

**Use `sweep_constants.py`, not repeated evaluator runs, to tune a constant.** It builds one
`Agent` and reuses it across every variant, so the ~13.5 s index construction is paid once instead
of per variant — a 30-variant sweep is minutes rather than an hour. It aborts if the control arm
does not reproduce the score of record exactly, so a broken harness can't quietly produce
plausible numbers. **Bump `BASELINE` in that file whenever a feature moves the score.**
Whenever the pipeline changes, the previously fitted argmax is no longer known to be the argmax:
re-run the affected axes rather than trusting a number tuned against an older stack.

**Use `py`, not `python3`.** On this Windows setup `python` and `python3` resolve to the Microsoft
Store stub and fail; `py` is the real launcher (Python 3.14.7 on this machine — verify locally,
since the exact patch version isn't pinned). The organizer README says `python3`
because it assumes Linux — our submission instructions must cover both.

A full run takes roughly 20 seconds: the FTS5 index over all 50k products is rebuilt on `Agent()`
construction, then 200 sessions replay against it. It was ~60s before reranking landed; deferred
disclosure (feature 05) added a few seconds back, since sessions now deliberately run a turn or two
longer to buy rank, and the phrase routes (feature 06) add a handful of extra FTS5 queries per turn.

`requirements.txt` pins **`numpy`, `scipy`, `scikit-learn`** — added for the dense route in feature
07, and the project's only third-party dependencies. Everything else is standard library. The agent
makes **no network call in its default configuration**, which is the one the organizer runs; the
optional SiliconFlow route added in feature 13 is `urllib.request` and is off unless both
`SHOPPING_COPILOT_API_KEY` and `SHOPPING_COPILOT_LLM` are set. Anything further added must be pinned there too, or
the organizer cannot reproduce the run: `pip install -r requirements.txt` is a required step in our
setup instructions, not an optional one.

## Hard rules

These are competition constraints, not style preferences. Violating them invalidates a run.

- **Only `starter/agent.py` and our own new modules are editable.** Never modify `evaluator/`,
  `data/`, or the organizer files in `docs/` (`competition_specification.md`,
  `submission_rules.md`, `agent_api_contract.json`, `evaluation_config.json`,
  `baseline_results.json`).
- **Never modify the catalog or invent ASINs.** The dataset is read-only.
- **Agent code must never read `ground_truth`.** It sits in `data/public_set.jsonl` for the
  evaluator's benefit only. Reading it from agent code is cheating, and it would not transfer to the
  private set anyway.
- `respond()` must return exactly `{message, ask_attribute, recommendations, usage}`
  (`docs/agent_api_contract.json`). `message` must be a string; `ask_attribute` must be one of
  `category, material, color, size, style, brand, budget, feature, use_case, other` or `null`;
  recommendations ordered best-to-worst, only the first 10 valid unique IDs are scored.
- **A raised exception, malformed output, or a timeout is scored as a miss** — the evaluator catches
  it and moves on (`evaluator/local_evaluator.py:241`). Fail soft; always return a valid shape.
- **No API keys in the repo.** Env vars only. Final judging may run with network access disabled, so
  anything online needs an offline fallback path.
- Out of scope, don't spend time here: any UI, model fine-tuning, hosted vector-DB clusters,
  multimodal input.

## Architecture

Six modules, and no network in the default configuration. `numpy`/`scipy`/`scikit-learn` (pinned in
`requirements.txt`) are the project's only third-party dependency, added for dense retrieval — see
`docs/features/07-hybrid-dense-retrieval.md`. Everything else is still pure standard library,
`starter/llm.py` included.

```text
starter/agent.py           orchestration + the official reset()/respond() contract
starter/retrieval.py       FTS5 index, query routes, RRF fusion
starter/dialog_state.py    per-session slots, evidence accumulation, question policy
starter/ranking.py         IDF coverage + phrase reranking over the fused candidate pool
starter/dense_retrieval.py offline LSA (TF-IDF + Truncated SVD) embeddings, no file I/O
starter/llm.py             OPTIONAL SiliconFlow Qwen3-8B client; off by default, stdlib-only
starter/facets.py          generic attribute facets (gender, neckline, ...); free-form path only
starter/env_file.py        .env scaffolding/loading; NEVER imported by the scored path
```

Keep the contract surface in `agent.py`; everything else is imported.

**Submission artifacts.** `starter/` is the source of truth; the judged bundle is generated
from it. `evaluator/local_evaluator.py:12` does `from starter.agent import Agent` and the
evaluator is not editable, so `starter/` **cannot be renamed** into the layout
`docs/submission_rules.md` recommends. `tools/build_submission.py` emits that layout instead
— `submission/agent.py` + `src/` with imports rewritten, plus a four-line `starter/agent.py`
shim so both import paths resolve to one implementation — and then re-runs all 200 sessions
against the bundle, requiring byte-identical output. The bundle's two documents are
authored in `docs/` and copied in on build: `submission_setup.md` becomes its `README.md`
(setup and run only) and `submission_report.md` becomes its `REPORT.md`. The split is what
`docs/submission_rules.md` asks for -- setup instructions *and* a report *and* a
latency/token/cost disclosure -- without burying the install steps under the method.

```text
tools/build_submission.py  builds and verifies submission/
docs/submission_setup.md   setup instructions; the bundle's README.md
docs/submission_report.md  the required report; the bundle's REPORT.md
submission/                GENERATED — rebuild it, never edit it
```

- **Index.** `_build_index` loads all 50k products into an in-memory SQLite **FTS5** table at
  construction. Columns are separately weighted at query time via `bm25()`; `parent_asin` and
  `price` are `UNINDEXED` (price is a numeric filter, not a search term).
- **Constraint extraction.** `detect_constraints` regex-scrapes color, material, and a price
  ceiling from each message, merged **first-write-wins**, so a contradicting value is dropped rather
  than overwriting. That never got fixed and measurably stopped mattering — the phrase and dense
  routes bypass the hard filter, so a stale constraint can't suppress the identifying route. See the
  demoted note in Known gaps before touching it.
- **Evidence accumulation.** `DialogState.evidence` keeps every disclosure the customer has made,
  oldest first, and the query is built from all of it. Retrieving on the latest message alone throws
  away the product category from turn 1.
- **Clarification.** `DialogState.next_attribute()` walks `ASK_ORDER`, retiring attributes the
  customer says are empty. We ask on every turn: recommendations are scored every turn regardless,
  so a question is free.
- **Two speakers, one state machine.** Everything above is keyed to the sentence shapes the
  *simulated* customer emits. A person typing into the WebUI matches none of them, so
  `DialogState._observe_freeform` (feature 11) handles the fall-through: it accumulates the reply as
  evidence, lets an explicit correction overwrite a slot *and* scrub the superseded value out of
  `evidence`/`phrases`, and retires the attribute just answered so the question rotates. Feature 15
  adds the two things a person does that the simulator cannot: **negation** — "not fully polyester"
  is recorded in `DialogState.avoided`, kept out of the slot, scrubbed from the evidence and demoted
  in the results (`CatalogIndex.demote_terms`, after reranking) instead of becoming a polyester
  *requirement* — and **slot-aware questioning**, retiring the attribute for any filled slot so we
  stop asking for what we were already told. That branch
  is **unreachable while scoring** — every evaluator reply is claimed by an earlier regex — and the
  proof is that a full run with it in place is byte-identical to `results/results_after_fieldfactors.json`.
  Widen the correction cues or the extended colour/material vocabularies freely; they cannot move
  the score. Do **not** widen `COLOR_RE`/`MATERIAL_RE` themselves — those run on evaluator messages.
- **Dual-track routing.** If any constraint has been detected the session is on the *buying* track:
  constraints become hard `AND` terms plus a price filter. Otherwise it is *browsing* — a wide,
  unfiltered `OR` query.
- **Multi-route retrieval + fusion.** Up to four routes per turn: a whole-catalog keyword route; a
  `categories`-column-only route (so a strong category signal isn't diluted by noisy
  title/description scores); up to 12 IDF-weighted exact-phrase FTS5 routes for disclosures
  specific enough to narrow the catalog (feature 06); and a dense LSA route (feature 07) for
  semantically related items exact terms miss. `_fuse_rankings` merges them with weighted
  **Reciprocal Rank Fusion** (`weight / (60 + rank)`) — keyword `1.0`, category `0.3`, phrase routes
  up to `0.5` scaled by rarity, dense `0.3`. Phrase and dense routes are deliberately unfiltered by
  hard constraints, so a wrong filter can't suppress the one route that identifies the product.
- **Reranking.** Fusion no longer decides the final order; it generates a pool of
  `RERANK_POOL` (120) candidates and `Reranker.order` reorders them. The score is half IDF-weighted
  term coverage (discounted by which field matched) and half intact-phrase matching, on the premise
  that a shopper quotes the language of the product they want. Blending the fused BM25 order back in
  as a prior was measured and **removed** — it cost 0.03–0.04 TechnicalScore. Fusion still picks the
  pool and breaks ties.
- **Backfill.** If hard filters narrow the pool below the pool size, an unfiltered wide search tops
  it up rather than returning a short list.

### Reuse these — don't rewrite them

| Helper | In | Does |
|---|---|---|
| `CatalogIndex.retrieve` | retrieval.py | the whole multi-route + fusion + backfill pipeline |
| `CatalogIndex.run_ranked_query` | retrieval.py | one FTS5 MATCH + optional price filter + BM25 ordering |
| `CatalogIndex.fuse_rankings` | retrieval.py | weighted RRF over any number of ranked lists |
| `terms` / `or_expression` / `with_and_terms` | retrieval.py | tokenizing and FTS5 expression building |
| `detect_constraints` | dialog_state.py | color / material / price-ceiling regex extraction |
| `DialogState.evidence_text` | dialog_state.py | everything the customer has revealed, oldest first |
| `DialogState.evidence_phrases` | dialog_state.py | the same disclosures split into individual claims |
| `CatalogIndex.document_profile` | retrieval.py | cached `(term -> field factor, token string)` per product |
| `CatalogIndex.document_frequency` | retrieval.py | document frequency from FTS5's own `fts5vocab` table |
| `Reranker.order` | ranking.py | reorders a candidate pool against the customer's own phrasing |
| `tokens` | retrieval.py | tokenizing that keeps order *and* duplicates (`terms` dedupes) |
| `fts_tokens` | retrieval.py | tokenizing that matches the **index** — keeps stopwords, for querying |
| `phrase_expression` | retrieval.py | one disclosure -> an FTS5 phrase query, or None if too short |
| `CatalogIndex.phrase_routes` | retrieval.py | the disclosures worth their own query, IDF-weighted |
| `disclosure_limit` | agent.py | how much of the ranked list this turn is allowed to reveal |
| `DenseIndex.top_k` / `.similarity_scores` | dense_retrieval.py | LSA nearest-neighbours / per-candidate cosine similarity |

## How scoring actually behaves

The evaluator is participant-visible, and reading it explains most of our lost points. Everything
below is derived from `evaluator/local_evaluator.py` — no ground-truth peeking.

- **The customer only tells you what you ask for.** `customer_reply`
  (`evaluator/local_evaluator.py:166`) discloses a constraint only when
  `classify_constraint(value) == ask_attribute`. Pick the wrong attribute and you learn nothing;
  re-ask an exhausted one and you get *"I don't have an additional preference for X."*
- **`ask_attribute: null` wastes the turn.** With no attribute the customer replies *"Those options
  are not quite right yet. Ask me about one specific attribute."* and reveals nothing
  (`evaluator/local_evaluator.py:171`). Always ask something.
- **`other` is the only attribute that cannot whiff.** The disclosure filter is
  `attribute == "other" or classify_constraint(value) == attribute`
  (`evaluator/local_evaluator.py:178-181`), so `other` matches any undisclosed constraint and returns
  up to two per turn. This is simulator-specific — see the risk note in
  `docs/features/03-clarification-loop.md`.
- **"an additional preference" vs "a preference" are different replies.** The first means the
  attribute is genuinely empty (retire it); the second is the boundary customer deferring once (do
  **not** retire it, they answer normally afterwards).
- **Disclosures are near-verbatim target text.** Intent cards are built from the target's own
  `features`/`details` (`evaluator/local_evaluator.py:52-71`), so getting the customer to speak is
  getting them to quote the answer. Feed it all straight into the query.
- **The customer knows at most 4 things, and is drained by turn 3.** This is the single most
  important mechanism in the file. `intent_card` sets `hard_constraints = cleaned[:2]` and
  `soft_preferences = cleaned[2:4]` (`evaluator/local_evaluator.py:69-71`), so the entire
  disclosable pool is **4 constraints**; `customer_reply` returns at most two undisclosed ones per
  turn (`evaluator/local_evaluator.py:177`). Two `other` questions therefore exhaust the customer
  completely:

  | Turn | Evidence in hand |
  |---|---|
  | 1 | the opening message (category) |
  | 2 | + 2 constraints |
  | 3 | + the remaining 2 — **everything obtainable, ever** |
  | 4+ | *"I don't have an additional preference"* — nothing new arrives |

  **Consequence: no dialog-side strategy can ever beat the current one.** Deferring past turn 3
  buys zero new information and only costs MTTC; a better `ASK_ORDER` cannot extract a fifth
  constraint that does not exist. This is why feature 05 measured an MRR plateau at ~0.858 and why
  no session runs past turn 4 — it is a structural ceiling, not a tuning artifact. Given fixed and
  complete evidence by turn 3, **the only remaining lever in the whole system is ranking quality.**
- **The first hit ends the session and freezes the rank.** `evaluator/local_evaluator.py:243`
  `break`s the moment the target appears anywhere in the top 10, so `best_rank` is its rank on that
  turn and no later turn can improve it. Surfacing the target early at a bad rank is therefore a
  *cost*, not a win — and the weights say so: one turn of delay costs 0.0001 of TechnicalScore while
  one unit of RR is worth 0.0015, so deferring pays whenever it buys more than ~0.067 RR. This is
  the whole basis of feature 05; don't "optimize" the disclosure gate away.
- **Intent-override sessions cannot convert early.** The `override_applied` guard
  (`evaluator/local_evaluator.py:252`) discards hits before the override message fires on turn 3
  or 4. Ranking the target at #1 on turn 1 scores nothing in those sessions.
- **Boundary sessions decline exactly once**, then answer normally. Looping clarifying questions at
  them is pure turn burn.
- **Runs are deterministic.** `materialize_hidden_fields` regenerates intent cards from the target
  product with a seeded RNG, so identical code always scores identically. A changed score means a
  changed agent — never run-to-run noise.

**Tune to the mechanism, not to the strings.** The spec says the organizer may add natural-language
paraphrasing, and the private set is 4x the size of ours. Matching literal simulator phrases will
not survive that; modelling "ask a targeted question, absorb the answer into state" will.

## Known gaps (highest leverage first)

1. **There is no open lever left. The last one was measured and rejected**
   (`docs/features/17-coverage-precision-and-robustness.md`). `_coverage`
   (`starter/ranking.py`) scores recall with no length normalization: it divides by `total_mass`,
   which is constant across candidates, so it asks *how much of the customer's evidence is in this
   product* and never the converse. That was listed here for several features as the one idea with
   a real mechanism behind it that had not been tried, and the reasoning was sound about the
   symptom and **wrong about the cause**. BM25 length normalization was implemented and swept:

   | Direction | Result |
   |---|---|
   | penalise length (`b` 0.15 → 1.0) | **−0.036 → −0.325**, monotone, all three metrics at once |
   | reward length (`b` −0.1 → −0.5) | **−0.020 → −0.245**, monotone |
   | distinct-vocabulary length instead of raw | **−0.011 → −0.038** |

   **`b = 0` is a strict local maximum in both directions under both definitions of length** —
   nothing tested is inside the noise floor. Two reasons it had to fail: **length is positively
   correlated with being a target** (the simulator generates every disclosure from the target's own
   `features`/`details`, so a rich listing is both disclosable *and* long), and **precision is
   already priced in twice**, by `_phrase_score` and by the `description` 0.65 / `store` 0.7 field
   factors. Do not re-parameterize this — log damping is a gentler member of a family that is
   already negative at its gentlest tested point. A new idea is needed, not another curve.
2. **Four misses remain, and they are information-theoretically unreachable:** `public_0020`,
   `public_0087`, `public_0144`, `public_0174`. Their disclosed constraints don't discriminate at
   all. Verified in feature 06 — a conjunction route that narrowed the candidate set to 100 still
   could not order it. `public_0020` was re-checked directly: it survives the hard `AND` filter and
   lands at rank 15 after reranking; *removing the filter entirely* moves it to 14. **Not worth
   further retrieval work.**

   This list was five until feature 10. `public_0145` is now a **marginal hit at rank 10 on turn
   5** — one position from being a miss again. Do not read HitRate 0.98 as robust: it is 0.975 plus
   a session hanging on the boundary of the cut, and any ranking change can push it back out.
3. **The first two turns return a single recommendation** (feature 05). It never costs a find here
   and it is contract-legal, but it is thin UX and reads oddly in a live demo. Disclose it in the
   final report rather than letting a judge find it.
4. **Closed (feature 17): `respond()` now has a broad exception guard.** It was `try/finally` for
   latency timing only and re-raised, so an exception in `DialogState.observe` or in retrieval
   routes 1–2 escaped, and a raised exception is scored as a miss
   (`evaluator/local_evaluator.py:239-244`). Now inputs are coerced (`user_message` to `str` with
   the falsy case going to `""` first, so `None` cannot become the query term "none"; `turn` to
   `int`, default 1), an unknown `session_id` auto-creates its session rather than raising, and a
   broad `except Exception` returns `_fallback_response()` — contract-valid, `ask_attribute="other"`
   (never `null`, which wastes the turn outright), empty recommendations. It is the **outermost**
   layer only; the inner fault isolation still keeps the turn *working*, and this only stops what
   escapes it from becoming a miss.

   Banked in the same feature: `_capped_terms` reserves `QUERY_TERM_HEAD = 16` and drops the
   *middle* of an over-long query rather than the newest terms. Feature 09's proposal to take the
   tail instead would have inverted the bug — dropping the turn-1 category, which is the reason
   evidence accumulates at all.

   Both are **byte-identical** to `results/results_after_fieldfactors.json`, ratchet reports
   `PASS: byte-identical`, and `verify_features.py` went 90 checks with 2 XFAILs → **95 with none**.
   Neither buys a public-set point; both remove a private-set failure mode on a set 4x larger with
   paraphrasing the spec permits.

*Demoted (do not re-attempt without new evidence):* **`user_profile` carries no retrieval signal.**
`reset()` discards it (`starter/agent.py:98-100`; the "may be used for personalization" comment
there is starter-kit boilerplate, not a description of behaviour) and the parameter appears nowhere
else in `starter/`. This was listed for several features as the top *open* gap with "unproven
upside". It has now been measured across all 200 sessions, and the upside is **zero**:
`purchase_frequency` is the constant `"3-4 prior purchases"` in all 200; `category_bucket` is
`"clothing"` in all 200; `summary` is mechanically derived from `preference_tags` + `rating_style`;
`average_prior_rating` and `rating_style` are a perfectly correlated pair describing the *reviewer's
temperament*, not the product. The only field with any variation is `preference_tags` — 9 values,
heavily degenerate: `fit` 163/200, `material` 154, `comfort` 144, `style` 101, then a long tail. A
field present in over half the sessions cannot separate one target from 50,000 products. The only
defensible use is biasing `ASK_ORDER` (`starter/dialog_state.py:72-75`), and that competes directly
with the deliberate `"other"`-first design — `other` is the only attribute that cannot whiff. There
is no personalization win here; spend the time on the submission artifacts instead.

*Demoted (an earlier gap #1, do not re-attempt without new evidence):* **state is first-write-wins**, so a
contradicting value lands in a filled slot and is dropped (`starter/dialog_state.py:125-132`). The
claim that this had "measured victims" in `public_0020`/`public_0145` was **wrong** — both are
`buying` sessions, which never receive an override message at all, and neither is excluded by the
filter (see gap 2). Instrumenting `DialogState.observe` over a full run shows the drop fires in
**3 of 200 sessions and is benign in every one** — all three are multi-material listings where the
retained value is still correct (`leather`←"Polyester lining", `cotton`←"Polyester,Cotton,Spandex",
`spandex`←"92% Polyester, 8% Spandex"), and none is among the five misses. No public-set session
loses a genuine correction to this. `intent_override` sits at 29/30 with MRR 0.851 and 24 of 29 hits
at rank 1, because the phrase and dense routes deliberately bypass the `AND` filter
(`starter/retrieval.py:392-411`) — which is *why* the bug stopped mattering without being fixed.
**Erase-and-rewrite has since been taken, and intent override is now a closed question**
(`docs/features/12-intent-override.md`). Features 11 and 12 clear **all** of `SLOTS` on an
`OVERRIDE_RE` match and let that message refill them — colour/material (fires in 21 of 30 override
sessions) and `price_max` (0 of 30 here). Both are byte-identical to
`results/results_after_fieldfactors.json`: zero deltas, not small ones. **Do not quote either as a gain**,
and note the `price_max` clear is **unmeasured rather than proven harmless** — it never fires on the
public set. Clearing a filter only widens the pool, so it cannot *exclude* the target the way the
rejected variants below can; it can still change the target's *rank* by admitting competitors.

**Do not go further and retract the withdrawn requirement itself — both ways of doing it were
measured and rejected.** `behavior_for` (`evaluator/local_evaluator.py:74-86`) sets
`old_value = soft[-1]` and `new_value = hard[0]`, *both from the target's own intent card*: the
"abandoned" preference is a true attribute of the product we are hunting, and the target never
changes. The override is cosmetic, and the private set is generated by the same function. Deleting
the retracted claim from `evidence`/`phrases` costs **−0.003875** TechnicalScore
(`intent_override` MRR 0.880 → 0.800). Dropping it from phrase routes only is byte-identical but
discards 19 identifying routes across 16 sessions, several at **df = 1** — flat here, a miss waiting
to happen on the private set.

**The 4 remaining non-rank-1 override sessions are not an override problem.** They sit at ranks
2/2/7/4, and `public_0103` never leaves rank 4 on any turn, before or after the override. They are
the same residual as the other rank-2-to-8 sessions — which feature 17 has now shown is *not* a
coverage-precision problem either. `intent_override` is 29/30 with 25 at rank 1 and MTTC 3.69 against a
structural floor of 3.60 — there is 0.09 of a turn left in the whole scenario.

*Removed:* "turns are wasted once evidence runs dry" was listed here for three features and was
never true — no session runs past turn 4 except the 5 misses. It is now deliberately false in the
other direction: feature 05 spends those turns on purpose.

## Priorities

Cut from the bottom up. Never let a Tier 3 idea pull someone off Tier 1 work.

| Tier | Drives | Items |
|---|---|---|
| 0 | prerequisite | agent contract wired end-to-end; evaluator reproduces a score |
| 1 | HitRate@10 (50%) | dual-track routing ✅ · multi-route retrieval ✅ · slot memory ✅ · clarification trigger ✅ |
| 2 | MRR (30%) | semantic reranking ✅ · rank-vs-turn arbitrage ✅ · hybrid/dense retrieval ✅ (route only, flat) · intent override ✅ (feature 12 — closed: slots cleared, retraction measured and rejected, 29/30 at MTTC 3.69 vs floor 3.60) · personalization ❌ (measured: profile is degenerate, no retrieval signal — see Known gaps) · coverage precision term ❌ (feature 17 — measured and rejected: b=0 is a strict two-sided optimum, −0.036 to −0.325 penalising length, −0.020 to −0.245 rewarding it) |
| 3 | Efficiency (20%) + feasibility | latency & token logging ✅ (feature 08) · offline fallback ✅ (feature 13 — no longer vacuous: there is now an online path, and a full run with it configured *and* the network dead is byte-identical) · optional SiliconFlow Qwen3-8B ✅ (feature 13 — off by default; `expand` mode unmeasured against the live model) · boundary handling ✅ (`DECLINE_RE` vs `EXHAUSTED_RE`, `starter/dialog_state.py:56-59`) · free-form input robustness ✅ (features 11 + 15 — WebUI/demo only, both verified score-neutral byte-for-byte; 15 adds negation-as-exclusion and stops re-asking a filled slot) |

Four roles, split by problem-statement pillar — Retrieval & Routing, Dialog + Ranking, Integration,
Coordination + Evaluation. **Individual assignments are still TBD.**

### Model policy

**Provider: OpenRouter, model `inclusionai/ling-3.0-flash-fin:free`, and it is OFF by default.**
SiliconFlow was the original choice (`docs/features/13-optional-llm.md`) but its free tier
needs mainland-Chinese real-name verification, so no key was ever obtained against it; the
default moved to OpenRouter, which needs no identity check. **The provider is pluggable**: the
client is plain OpenAI-compatible chat completions, so `SHOPPING_COPILOT_BASE_URL` +
`SHOPPING_COPILOT_MODEL` point it at SiliconFlow, a local Ollama, or anything else — setup
recipes in `docs/LLM_SETUP.md`. The env vars are now provider-neutral `SHOPPING_COPILOT_*`;
the old `SILICONFLOW_*` names still work as aliases and `.env` migrates itself on the next
write. The `SiliconFlowClient` class name is still historical.

The model was picked by **measurement** (`py tools/benchmark_llms.py`, run twice): it was the
only free slug scoring 100% on all four probes, at ~1.5s. **Two live gotchas:** OpenRouter's
free tier is **50 requests/day** shared across models (one benchmark run over five models
spends half of it, and exhaustion returns 429 on everything — looks exactly like a bad key),
and free pools are rate-limited upstream per model on top of that. Free slugs come and go; if
the default fails, re-run the benchmark and take the winner. The default configuration is the judged one
and makes no model call at all; a full run with the model code in place is **byte-identical** to
`results/results_after_fieldfactors.json`, sessions array included.

Enabling requires **both** `SHOPPING_COPILOT_API_KEY` and `SHOPPING_COPILOT_LLM`; neither alone does
anything, and an unrecognized mode fails closed to `off`. Modes:

| Mode | Reach | Score risk |
|---|---|---|
| `off` (default) | nothing runs | none — byte-identical |
| `freeform` | only `DialogState._observe_freeform`, unreachable while scoring | none by construction |
| `expand` | adds retrieval route 5 at weight 0.25 | real but tiny; **unmeasured against the live model** |

Everything fails soft: timeout, HTTP error, bad JSON, or no network returns `None` and the agent
falls through to the pipeline that scores 0.912205 on its own. Feature 14
(`docs/features/14-llm-circuit-breaker.md`) adds a **circuit breaker** on top: 2 consecutive connection failures, 3 failures of any kind, or 3 consecutive
successes slower than 4.5 s latch the client off for the rest of the process, so a dead endpoint
costs one timeout rather than one per turn. `.env` is scaffolded and loaded by `webui/` and
`tools/` (never by the evaluator, which reads `os.environ` directly), a real environment variable
always wins over the file, and the WebUI's **Model** button changes mode/key/model live via
`Agent.configure_llm`. Verified, not asserted — a full
200-session run with `expand` configured *and every socket raising* is byte-identical too.

Two cautions before anyone enables `expand` for real. **It has only ever been measured against
stubs** (no key exists on the team yet); the worst stub cost 0.00013 with HitRate untouched, but a
stub is not a model. And **`expand` breaks reproducibility**: greedy decoding is the closest this
endpoint offers, so "a changed score means a changed agent" — and the control-arm guard in
`sweep_constants.py` — hold only in `off` and `freeform`.

Disclosed in the final report either way: model, cost (free tier), token usage (`usage` now reports
real counts when enabled, honest zeros when not), latency, and fallback behavior.

## Workflow

### Definition of done for a feature

A feature is not done until the evaluator has been re-run and the score movement written down.

1. Implement it.
2. `py -m evaluator.local_evaluator --output results/results_after_<milestone>.json`
3. `py tools/score_delta.py <previous>.json results/results_after_<milestone>.json`
4. Write `docs/features/NN-<name>.md` — what/why, approach, the delta table (aggregate **and** all
   four scenarios), known limitations. Template in `docs/features/README.md`.
5. Commit code and results snapshot together.

Record flat and negative results too. A regression documented in two minutes stops a teammate from
re-attempting the same idea on the final day.

The public set is 200 sessions, so **one session = ±0.005 HitRate@10**. Deltas below ~0.01 are
noise; say so rather than claiming a win.

### Conventions

- **`main` and `dev` are identical and both carry the full work** (`git rev-list
  --left-right --count main...dev` is `0 0`). The original convention — "work on `dev`, `main`
  holds the untouched starter kit" — stopped being true well before submission; the starter kit
  is recoverable from history, not from a branch. Branch off whichever you have checked out.
- `results.json` is gitignored (scratch). Milestone scores are committed under `results/` as
  `results/results_after_<milestone>.json` — precedent: `results/results_after_multiroute.json`.
- **`submission/` is generated, never hand-edited.** `py tools/build_submission.py` rebuilds it
  from `starter/` and refuses to pass unless a full 200-session run against the bundle is
  byte-identical to `results/results_after_fieldfactors.json`. Change `starter/` or
  `docs/submission_setup.md` / `docs/submission_report.md`, then rebuild — an edit made
  directly in `submission/` is destroyed by the next build.
- `graphify-out/` is gitignored; each clone builds its own graph.
- Git hooks are **local to each clone**. Every teammate runs the setup below once after cloning.

### First-time setup

Run once per clone — git hooks and `.claude/` are not version-controlled, so every teammate does
this themselves.

```bash
graphify extract . --code-only                  # build the knowledge graph (local AST, no API key)
graphify hook install                           # rebuild on commit + checkout
cp tools/git-hooks/post-merge .git/hooks/       # rebuild on merge (see below)
chmod +x .git/hooks/post-merge
graphify claude install                         # CLAUDE.md section + Claude Code PreToolUse hooks
```

**Why the extra hook.** `graphify hook install` only covers post-commit and post-checkout, and
neither fires on a merge: a fast-forward merge creates no commit and no checkout, and a merge commit
is skipped by post-commit's own `MERGE_HEAD` guard. Both were verified on this repo. Without
`tools/git-hooks/post-merge` the graph goes stale exactly when it matters most — right after you
pull in a teammate's work.

The catalog must be present at `data/catalog.jsonl` (50,000 rows) before the evaluator will run —
see `README.md` for the download and checksum steps.

## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

Rules:
- For codebase questions, first run `graphify query "<question>"` when graphify-out/graph.json exists. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).
