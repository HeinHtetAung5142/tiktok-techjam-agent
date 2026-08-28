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

| Metric | Baseline (`docs/baseline_results.json`) | Current (`results_after_phrase.json`) |
|---|---|---|
| HitRate@10 | 0.125 | 0.975 |
| MRR | 0.068034 | 0.857935 |
| MTTC | 9.81 | 2.88 |
| **TechnicalScore** | **0.10671** | **0.907281** |

Per scenario HitRate@10, current: boundary 1.0 · browsing 0.9875 · intent_override 0.9667 ·
buying 0.9625.

**Every cheap term is spent.** MRR went 0.652 → 0.852 by trading turns for rank
(`docs/features/05-rank-vs-turn-arbitrage.md`) and plateaus there; phrase retrieval plus two
constraint-extraction bug fixes took HitRate to 0.975
(`docs/features/06-phrase-retrieval.md`). 161 of 195 hits land at rank 1.

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
```

**Use `py`, not `python3`.** On this Windows setup `python` and `python3` resolve to the Microsoft
Store stub and fail; `py` is the real launcher (Python 3.12.0). The organizer README says `python3`
because it assumes Linux — our submission instructions must cover both.

A full run takes roughly 20 seconds: the FTS5 index over all 50k products is rebuilt on `Agent()`
construction, then 200 sessions replay against it. It was ~60s before reranking landed; deferred
disclosure (feature 05) added a few seconds back, since sessions now deliberately run a turn or two
longer to buy rank, and the phrase routes (feature 06) add a handful of extra FTS5 queries per turn.

`requirements.txt` is currently **empty** — the agent is pure standard library. Anything added must
be pinned there, or the organizer cannot reproduce the run.

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

Four modules, no third-party deps, no network:

```text
starter/agent.py         orchestration + the official reset()/respond() contract
starter/retrieval.py     FTS5 index, query routes, RRF fusion
starter/dialog_state.py  per-session slots, evidence accumulation, question policy
starter/ranking.py       IDF coverage + phrase reranking over the fused candidate pool
```

Keep the contract surface in `agent.py`; everything else is imported.

- **Index.** `_build_index` loads all 50k products into an in-memory SQLite **FTS5** table at
  construction. Columns are separately weighted at query time via `bm25()`; `parent_asin` and
  `price` are `UNINDEXED` (price is a numeric filter, not a search term).
- **Constraint extraction.** `detect_constraints` regex-scrapes color, material, and a price
  ceiling from each message, merged **first-write-wins** — which is exactly why intent override
  doesn't work yet (see Known gaps).
- **Evidence accumulation.** `DialogState.evidence` keeps every disclosure the customer has made,
  oldest first, and the query is built from all of it. Retrieving on the latest message alone throws
  away the product category from turn 1.
- **Clarification.** `DialogState.next_attribute()` walks `ASK_ORDER`, retiring attributes the
  customer says are empty. We ask on every turn: recommendations are scored every turn regardless,
  so a question is free.
- **Dual-track routing.** If any constraint has been detected the session is on the *buying* track:
  constraints become hard `AND` terms plus a price filter. Otherwise it is *browsing* — a wide,
  unfiltered `OR` query.
- **Multi-route retrieval + fusion.** Two FTS5 queries per turn: a whole-catalog keyword route, and
  a `categories`-column-only route (so a strong category signal isn't diluted by noisy
  title/description scores). `_fuse_rankings` merges them with weighted **Reciprocal Rank Fusion**
  (`weight / (60 + rank)`), keyword at `1.0` and category at `0.3`.
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
| `CatalogIndex.retrieve` | retrieval.py | the whole two-route + fusion + backfill pipeline |
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

1. **State is first-write-wins**, so an intent override appends instead of replacing, and a stale
   colour/material keeps a wrong hard `AND` filter in place. This is now the top gap and it has
   *measured* victims: `public_0020` and `public_0145` are missed because a scraped colour excludes
   the target's own text. Needs erase-and-rewrite.
2. **`user_profile` is ignored entirely** — `reset()` discards it. Untouched signal, though the
   fields are abstract (`preference_tags` like "fit", "comfort") and may not identify a product.
3. **Five misses are information-theoretically unreachable.** Their disclosed constraints don't
   discriminate at all. Verified in feature 06 — a conjunction route that narrowed the candidate
   set to 100 still could not order it. **Not worth further retrieval work.**
4. **The first two turns return a single recommendation** (feature 05). It never costs a find here
   and it is contract-legal, but it is thin UX and reads oddly in a live demo. Disclose it in the
   final report rather than letting a judge find it.

*Removed:* "turns are wasted once evidence runs dry" was listed here for three features and was
never true — no session runs past turn 4 except the 7 misses. It is now deliberately false in the
other direction: feature 05 spends those turns on purpose.

## Priorities

Cut from the bottom up. Never let a Tier 3 idea pull someone off Tier 1 work.

| Tier | Drives | Items |
|---|---|---|
| 0 | prerequisite | agent contract wired end-to-end; evaluator reproduces a score |
| 1 | HitRate@10 (50%) | dual-track routing ✅ · multi-route retrieval ✅ · slot memory ✅ · clarification trigger ✅ |
| 2 | MRR (30%) | semantic reranking ✅ · rank-vs-turn arbitrage ✅ · hybrid/dense retrieval · intent override · personalization |
| 3 | Efficiency (20%) + feasibility | latency & token logging · offline fallback · boundary handling |

Four roles, split by problem-statement pillar — Retrieval & Routing, Dialog + Ranking, Integration,
Coordination + Evaluation. **Individual assignments are still TBD.**

### Model policy

Local and offline **for now** — no LLM calls, no API keys, no provider chosen. A hosted model may be
added later for query rewriting, clarification, or reranking, but only behind a fallback that still
runs with network access disabled, since official judging may cut the network. Whatever we choose
gets disclosed in the final report: model, approximate cost, token usage, latency, fallback
behavior.

## Workflow

### Definition of done for a feature

A feature is not done until the evaluator has been re-run and the score movement written down.

1. Implement it.
2. `py -m evaluator.local_evaluator --output results_after_<milestone>.json`
3. `py tools/score_delta.py <previous>.json results_after_<milestone>.json`
4. Write `docs/features/NN-<name>.md` — what/why, approach, the delta table (aggregate **and** all
   four scenarios), known limitations. Template in `docs/features/README.md`.
5. Commit code and results snapshot together.

Record flat and negative results too. A regression documented in two minutes stops a teammate from
re-attempting the same idea on the final day.

The public set is 200 sessions, so **one session = ±0.005 HitRate@10**. Deltas below ~0.01 are
noise; say so rather than claiming a win.

### Conventions

- Work on `dev`. `main` holds the untouched starter kit.
- `results.json` is gitignored (scratch). Milestone scores are committed as
  `results_after_<milestone>.json` — precedent: `results_after_multiroute.json`.
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
