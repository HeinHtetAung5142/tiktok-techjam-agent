"""The only module in the UI that touches the agent.

Everything the web layer knows about `starter/` goes through here, so the blast radius of
the UI is one file. The agent is used exactly as the evaluator uses it -- construct once,
`reset()` per session, `respond()` per turn -- and is never modified, monkeypatched, or
subclassed.

ISOLATION INVARIANT
-------------------
The randomly drawn target product does not appear anywhere in this module, and there is
no parameter through which it could arrive. `turn()` passes the user's typed text and
nothing else into `Agent.respond`. Whether the target is in the list is decided in the
browser (`static/app.js`) by comparing ids against the response. Grep this file for
"target" and the only hits are this comment and `threading.Thread(target=...)`, which is
the stdlib keyword argument, not the product.
"""

from __future__ import annotations

import queue
import threading
import time
import uuid

from starter import env_file
from starter import llm
from starter import retrieval
from starter.agent import MAX_QUERY_TERMS, Agent

from webui.catalog import CatalogReader, card


# The scored window: the evaluator only ever looks at the first 10 ids
# (evaluator/local_evaluator.py:16), and the contract pins `top_k` to that.
TOP_K = 10

# How deep the page lets you scroll. The agent's answer is still TOP_K; this is the
# display-only tail described in `_deep_list()`.
DISPLAY_DEPTH = 50

# The contract allows turns 1..10 (docs/agent_api_contract.json), and the evaluator stops
# there (MAX_TURNS). The UI honours the same bound so what you see is what would score.
MAX_TURNS = 10

# `reset()` ignores the profile entirely (starter/agent.py:98-100), but the contract
# declares a shape and something has to be passed. This is a neutral stand-in, not a
# personalization signal -- the profile was measured to carry none (see CLAUDE.md).
STUB_PROFILE = {
    "purchase_frequency": "3-4 prior purchases",
    "average_prior_rating": None,
    "rating_style": "unknown",
    "preference_tags": [],
    "summary": "Live web session; no prior history.",
}


def _explain_failure(last_error: str | None) -> str:
    """Turn a raw transport error into something an operator can act on.

    The generic "the call failed" this replaced was true of a dead network, a wrong key and
    an exhausted quota alike -- three problems with three completely different fixes.
    """
    raw = last_error or ""
    lowered = raw.lower()
    if "429" in raw or "rate limit" in lowered:
        hint = ("Rate limited. On OpenRouter's free tier that is 50 requests per DAY across "
                "all models -- it resets on its own, or add credits to raise it.")
    elif "401" in raw or "403" in raw or "user not found" in lowered:
        hint = "The key was rejected. Check it was pasted whole, and that it is for this endpoint."
    elif "404" in raw:
        hint = "The endpoint or model id was not found. Check the model slug still exists."
    elif "timed out" in lowered or "timeout" in lowered:
        hint = "The endpoint did not answer in time."
    elif not raw:
        hint = "No usable response."
    else:
        hint = "The call failed."
    detail = "%s The agent is unaffected -- it falls back to offline retrieval." % hint
    return "%s  [%s]" % (detail, raw) if raw else detail


def _mask_key(key: str) -> str:
    """`sk-abcd...wxyz` -- enough to recognize a key, never enough to use one.

    The browser gets this and only this. The real key is held in memory on the server and
    written to disk only when the operator explicitly asks for it to be persisted.
    """
    key = (key or "").strip()
    if len(key) <= 8:
        return "*" * len(key)
    return "%s...%s" % (key[:4], key[-4:])


class _AgentThread:
    """Owns the `Agent` and is the only thread that ever touches it.

    `CatalogIndex` holds a single `sqlite3.connect(":memory:")` (starter/retrieval.py:169)
    and sqlite3 refuses to be used from any thread but the one that created the
    connection -- a lock is not enough, because the objection is to the thread identity,
    not to concurrency. So the agent is *constructed* on this thread as well as driven
    from it, and request threads hand work over through a queue.

    A side benefit: the queue serializes agent access by construction, so the rest of the
    UI needs no locking, and static assets and catalog reads stay responsive while a turn
    is in flight.
    """

    def __init__(self, catalog_path: str) -> None:
        self.agent: Agent | None = None
        self._jobs: queue.Queue = queue.Queue()
        self._ready = threading.Event()
        self._startup_error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, args=(catalog_path,), daemon=True)
        self._thread.start()
        # Block until the index is built, so the server only says "ready" when it is.
        self._ready.wait()
        if self._startup_error is not None:
            raise self._startup_error

    def _run(self, catalog_path: str) -> None:
        try:
            self.agent = Agent(catalog_path)
        except BaseException as exc:  # surfaced to the constructor
            self._startup_error = exc
            self._ready.set()
            return
        self._ready.set()
        while True:
            work, slot, done = self._jobs.get()
            try:
                slot.append((True, work()))
            except BaseException as exc:
                slot.append((False, exc))
            finally:
                done.set()

    def call(self, work):
        """Run `work()` on the agent thread and return its result here."""
        slot: list = []
        done = threading.Event()
        self._jobs.put((work, slot, done))
        done.wait()
        ok, value = slot[0]
        if not ok:
            raise value
        return value


class AgentBridge:
    """One shared `Agent` plus per-browser-session turn accounting."""

    def __init__(self, catalog_path: str = "data/catalog.jsonl") -> None:
        # ~13.5 s: the FTS5 index over all 50k products is built here. Paid once, at
        # startup, never per request.
        self._worker = _AgentThread(catalog_path)
        self.agent = self._worker.agent
        # Opens its own file handle per read, so request threads may use it directly.
        self.reader = CatalogReader(catalog_path)
        self._turns: dict[str, int] = {}
        self._turns_lock = threading.Lock()

    # -- sessions ---------------------------------------------------------------

    def open_session(self) -> str:
        session_id = f"web_{uuid.uuid4().hex}"
        self._worker.call(lambda: self.agent.reset(session_id, dict(STUB_PROFILE)))
        with self._turns_lock:
            self._turns[session_id] = 0
        return session_id

    def close_session(self, session_id: str) -> None:
        with self._turns_lock:
            self._turns.pop(session_id, None)
        self._worker.call(lambda: self.agent._sessions.pop(session_id, None))

    # -- a turn -----------------------------------------------------------------

    def turn(self, session_id: str, message: str) -> dict:
        """One exchange. `message` is the only user-supplied value that reaches the agent."""
        with self._turns_lock:
            if session_id not in self._turns:
                raise KeyError("unknown session")
            turn_number = self._turns[session_id] + 1
            if turn_number > MAX_TURNS:
                return self._exhausted(self._turns[session_id])
            self._turns[session_id] = turn_number

        started = time.perf_counter()
        result, ranked = self._worker.call(
            lambda: self._exchange(session_id, message, turn_number)
        )
        elapsed_ms = (time.perf_counter() - started) * 1000.0

        disclosed_count = len(result.get("recommendations", []))
        return {
            "turn": turn_number,
            "message": str(result.get("message", "")),
            "ask_attribute": result.get("ask_attribute"),
            "disclosed_count": disclosed_count,
            # Hydration reads the catalog file, not the agent, so it runs here on the
            # request thread and leaves the agent thread free.
            "results": self._hydrate(ranked, disclosed_count),
            "usage": result.get("usage", {}),
            "latency_ms": round(elapsed_ms, 1),
            "done": turn_number >= MAX_TURNS,
        }

    def _exchange(self, session_id: str, message: str, turn_number: int) -> tuple[dict, list[str]]:
        """Runs on the agent thread. The whole of the UI's contact with `Agent`."""
        result = self.agent.respond(session_id, message, turn_number, TOP_K)
        disclosed = [
            str(item.get("parent_asin", "")) for item in result.get("recommendations", [])
        ]
        return result, self._deep_list(session_id, disclosed)

    def _deep_list(self, session_id: str, disclosed: list[str]) -> list[str]:
        """The ranking behind the agent's answer, for display only.

        `DISCLOSURE_SCHEDULE = (1, 1, 4, 8, 10)` (starter/agent.py:37) means turns 1 and 2
        hand back a *single* recommendation -- a deliberate rank-vs-turn trade (feature
        05), not a bug. A page showing one row and nine blanks reads as broken, so the UI
        re-runs the same retrieval the agent just ran and shows the whole ranking, marking
        which rows the agent actually disclosed and which it is withholding.

        Called on the agent thread and *after* `respond()`, so the state has already
        observed this turn's message. `retrieve()` does not mutate state, so this is
        side-effect free -- it costs one extra retrieval pass (~50 ms) and nothing else.
        """
        try:
            state = self.agent._sessions[session_id]
            query_terms = retrieval.terms(state.evidence_text())[:MAX_QUERY_TERMS]
            phrases = state.evidence_phrases()
            is_buying = state.is_buying
            ranked = self.agent.index.retrieve(
                query_terms,
                state.and_terms() if is_buying else [],
                state.price_max() if is_buying else None,
                DISPLAY_DEPTH,
                reranker=lambda pool: self.agent.reranker.order(pool, phrases),
                phrases=phrases,
            )
            deep = [str(item.get("parent_asin", "")) for item in ranked]
        except Exception:
            # A display convenience must never be able to break the page.
            return list(disclosed)

        # Both calls are deterministic and run off the same state, so the agent's answer
        # must be the head of this list. If it somehow is not, trust the agent's.
        if deep[: len(disclosed)] != disclosed:
            return list(disclosed)
        return deep

    def _hydrate(self, ranked: list[str], disclosed_count: int) -> list[dict]:
        """Bare ids -> renderable cards. `respond()` returns no product detail at all."""
        products = self.reader.get_many(ranked)
        rows = []
        for position, parent_asin in enumerate(ranked, start=1):
            product = products.get(parent_asin)
            row = card(product) if product else {
                "parent_asin": parent_asin,
                "title": "(not in catalog)",
                "store": "",
                "price": None,
                "average_rating": None,
                "rating_number": None,
                "categories": [],
                "features": [],
            }
            row["rank"] = position
            row["scored"] = position <= TOP_K
            row["disclosed"] = position <= disclosed_count
            rows.append(row)
        return rows

    def _exhausted(self, turn_number: int) -> dict:
        return {
            "turn": turn_number,
            "message": (
                f"This session has used all {MAX_TURNS} turns, which is where the "
                "evaluator stops. Start a new chat to keep going."
            ),
            "ask_attribute": None,
            "disclosed_count": 0,
            "results": [],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0},
            "latency_ms": 0.0,
            "done": True,
        }

    # -- the optional model -------------------------------------------------------
    #
    # All three go through `_worker.call`, like every other agent access: the client is
    # read on the agent thread during a turn, so it must be swapped there too.

    def llm_config(self) -> dict:
        """Current model configuration for the settings panel. Never returns the key."""
        stats = self._worker.call(self.agent.model_stats)
        client = self.agent.llm
        return {
            "mode": stats.get("mode", "off"),
            "enabled": stats.get("enabled", False),
            "model": stats.get("model", llm.DEFAULT_MODEL),
            "base_url": stats.get("base_url", llm.DEFAULT_BASE_URL),
            "has_key": client is not None,
            "key_hint": _mask_key(client.api_key) if client is not None else "",
            "disabled": stats.get("disabled", False),
            "disabled_reason": stats.get("disabled_reason"),
            "last_error": stats.get("last_error"),
            "calls": stats.get("calls", 0),
            "failures": stats.get("failures", 0),
            "skipped": stats.get("skipped", 0),
            "prompt_tokens": stats.get("prompt_tokens", 0),
            "completion_tokens": stats.get("completion_tokens", 0),
            "mean_ms": stats.get("mean_ms", 0.0),
            "modes": list(llm.MODES),
            "default_model": llm.DEFAULT_MODEL,
            "default_base_url": llm.DEFAULT_BASE_URL,
        }

    def set_llm_config(
        self,
        mode: str,
        api_key: str | None,
        model: str | None,
        base_url: str | None,
        persist: bool = False,
    ) -> dict:
        """Apply a new model configuration, optionally writing it to `.env`.

        An empty `api_key` from the browser means "keep the key I already gave you" rather
        than "clear it" -- the field is rendered masked, so treating blank as a deletion
        would wipe the key every time someone changed only the mode. Clearing is done by
        selecting mode `off`, which drops the client entirely.
        """
        current = self.agent.llm
        key = (api_key or "").strip() or (current.api_key if current is not None else "")
        model = (model or "").strip() or llm.DEFAULT_MODEL
        base_url = (base_url or "").strip() or llm.DEFAULT_BASE_URL

        self._worker.call(
            lambda: self.agent.configure_llm(
                api_key=key, mode=mode, model=model, base_url=base_url
            )
        )

        saved = None
        if persist:
            # The one place a key touches disk, and only because the operator ticked the
            # box. `.env` is gitignored; `update_env_file` preserves the template's
            # comments so the file stays self-documenting.
            values = {
                llm.MODE_ENV: llm.resolve_mode(mode),
                llm.API_KEY_ENV: key,
                llm.MODEL_ENV: "" if model == llm.DEFAULT_MODEL else model,
                llm.BASE_URL_ENV: "" if base_url == llm.DEFAULT_BASE_URL else base_url,
            }
            try:
                saved = str(env_file.update_env_file(values))
            except OSError as exc:
                saved = "could not write .env: %s" % exc

        config = self.llm_config()
        config["saved_to"] = saved
        return config

    def test_llm(self) -> dict:
        """One real round trip, so the operator learns whether the key works.

        Closes the breaker first: the usual reason for pressing Test is that something was
        broken and may now be fixed, and a latched-off client would answer instantly and
        misleadingly with "no".
        """
        client = self.agent.llm
        if client is None:
            return {"ok": False, "detail": "No model configured. Set a key and a mode first."}

        def probe() -> dict:
            client.reenable()
            before_failures = client.failures
            started = time.perf_counter()
            text = client.complete(
                "Reply with the single word: ok", "ping", max_tokens=8
            )
            elapsed = (time.perf_counter() - started) * 1000.0
            if text is None:
                # Say WHY. `last_error` carries the provider's own words ("Rate limit
                # exceeded: free-models-per-day"), which is the difference between a
                # five-second fix and an hour of guessing.
                return {
                    "ok": False,
                    "detail": _explain_failure(client.last_error),
                    "error": client.last_error,
                    "latency_ms": round(elapsed, 1),
                    "failures": client.failures - before_failures,
                }
            return {
                "ok": True,
                "detail": "Model answered: %s" % (text[:80] or "(empty)"),
                "latency_ms": round(elapsed, 1),
            }

        result = self._worker.call(probe)
        result["config"] = self.llm_config()
        return result

    # -- disclosure ---------------------------------------------------------------

    def stats(self) -> dict:
        stats = self._worker.call(self.agent.latency_stats)
        stats["llm"] = self.llm_config()
        return stats
