# `webui/` — local web UI for the shopping agent

A small website for talking to the agent by hand and watching the ranking move, plus a
"guess the hidden product" game. It exists for demos and debugging; it is **not** part of
the competition entry.

```bash
py -m webui.server                 # http://127.0.0.1:8000
py -m webui.server --port 9000 --catalog data/catalog.jsonl
```

Startup takes ~15 s: `Agent()` builds the FTS5 index over all 50k products once, and the
UI's own catalog offset index adds about 0.1 s. Every request after that reuses them.

## It is fully removable

Deleting this directory is a complete uninstall:

```bash
rm -rf webui/
```

- Nothing under `starter/`, `evaluator/`, or `data/` was modified to add it, and nothing
  outside `webui/` imports it.
- It adds **no** dependency. `requirements.txt` still pins only `numpy`, `scipy`,
  `scikit-learn` — all of which belong to the agent's dense route, none of which this UI
  uses. The server is `http.server` + `json` from the standard library.
- It makes no network call and loads no CDN asset, so it runs with the network off, like
  the agent.
- The agent is used exactly as `evaluator/local_evaluator.py` uses it — constructed,
  `reset()`, `respond()` — never subclassed, patched, or edited.

The score is unchanged with this directory present or absent; `py -m evaluator.local_evaluator`
still reproduces TechnicalScore **0.912205**.

## The target never reaches the agent

The random product at the top of the page is a UI-side game. It is isolated by
construction, not by convention:

- `webui/target.py` draws it, builds the payload, and returns it. **The server stores it
  nowhere** — no session field, no global.
- `POST /api/message` reads `session_id` and `message` from the request body and nothing
  else. `message` is the only user-supplied value passed to `Agent.respond`.
- `webui/agent_bridge.py` — the one module that touches the agent — has no parameter
  through which a target could arrive. Grep it for `target` and the only hits are the
  header comment saying so and `threading.Thread(target=...)`, the stdlib keyword.
- "Is the target in the list?" is answered in `static/app.js`, in the browser, by
  comparing ids against the response that already came back.

So the agent's ranking is produced from your typed messages alone, exactly as it would be
during scoring.

## Why the list shows more than the agent returned

`DISCLOSURE_SCHEDULE = (1, 1, 4, 8, 10)` in `starter/agent.py:37` means the agent hands
back a **single** recommendation on turns 1 and 2, four on turn 3, eight on turn 4. That
is deliberate — the evaluator freezes the target's rank the moment it appears in the top
10, so surfacing a list early at a bad rank is a cost (feature 05,
`docs/features/05-rank-vs-turn-arbitrage.md`).

A page showing one row and nine blanks reads as broken, so the UI re-runs the same
retrieval call after `respond()` and shows the full ranking, tagging every row the agent
withheld. What the agent actually disclosed is the untagged head of the list, and the
counter above the list spells out the split. The extra pass is display-only, costs ~50 ms,
is wrapped in `try/except`, and cannot affect the agent's answer.

## Layout

| File | Role |
|---|---|
| `catalog.py` | byte-offset index over `data/catalog.jsonl`; `parent_asin` → full product row |
| `agent_bridge.py` | the only module that touches the agent; owns the lock, turns, deep list |
| `target.py` | random product + the evaluator's own `intent_card()` disclosures |
| `server.py` | `http.server` JSON API and static files |
| `static/` | the page — `index.html`, `styles.css`, `app.js`, no external assets |

`CatalogIndex` holds one `sqlite3` connection created on its constructing thread, so
`AgentBridge` serializes every agent touch behind a `threading.Lock`.

## API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/session` | new session; calls `agent.reset` with a neutral stub profile |
| `POST` | `/api/target` | draw a random product (reroll); touches no agent state |
| `POST` | `/api/message` | one turn: `{session_id, message}` → ranking, question, usage |
| `POST` | `/api/end` | drop a session |
| `GET` | `/api/stats` | `agent.latency_stats()` |

Sessions are capped at 10 turns, matching `MAX_TURNS` in the evaluator and the
`docs/agent_api_contract.json` bound, so what you see on screen is what would score.
