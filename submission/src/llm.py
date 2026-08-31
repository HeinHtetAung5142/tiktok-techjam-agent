"""Optional OpenAI-compatible language model, off by default.

Provider-agnostic by construction: this is plain chat completions -- `POST
{base_url}/chat/completions`, `Bearer` auth, `messages`/`temperature`/`max_tokens` -- so
OpenRouter (the default), SiliconFlow, Groq, Together or a local Ollama all work with no
code change. The `SILICONFLOW_*` variable names and the `SiliconFlowClient` class name are
historical: feature 13 targeted SiliconFlow before its free tier turned out to require
mainland-Chinese real-name verification. Setup recipes: docs/LLM_SETUP.md.

Why this module is shaped so defensively
----------------------------------------
Three hard constraints from CLAUDE.md decide the whole design:

1. **Official judging may run with network access disabled.** So a model call can never
   be on the critical path; it must be an enhancement that vanishes cleanly.
2. **A raised exception or malformed output is scored as an outright miss.** So no call
   here may raise, ever. Every public function returns `None`/`[]` on any failure --
   timeout, HTTP error, bad JSON, unparseable content, missing key.
3. **The score of record is 0.912205 and must not drop.** The only way to *prove* that
   rather than argue it is to make the judged configuration a no-op: with no API key and
   no mode set, not one line below runs and the agent behaves exactly as it did before
   this file existed. That claim is checked by a full 200-session run coming back
   byte-identical, not merely score-identical.

Configuration is environment-only -- no key ever lands in the repo (a hard rule).

    SHOPPING_COPILOT_LLM       off (default) | freeform | expand
    SHOPPING_COPILOT_API_KEY   required for any model use at all. Unset -> disabled.
    SHOPPING_COPILOT_MODEL     model id (default inclusionai/ling-3.0-flash-fin:free)
    SHOPPING_COPILOT_BASE_URL  endpoint (default https://openrouter.ai/api/v1)

The `SILICONFLOW_*` names feature 13 shipped with still work as aliases; see `LEGACY_ENV`.

Modes, in increasing order of how much they can affect a scored run:

- **off** -- the default. No client is constructed. Zero behaviour change. This is the
  configuration the organizer will run, because they have no key of ours.
- **freeform** -- the model is used *only* to understand a human typing free prose into
  the manual-testing UI. That branch (`DialogState._observe_freeform`) is unreachable
  while scoring: every simulated-customer reply is claimed by an earlier regex, which is
  why feature 11 came back byte-identical. So this mode is score-neutral *by
  construction*, not by measurement.
- **expand** -- additionally lets the model propose extra retrieval keywords, fused as
  one more low-weight RRF route. This one *can* move the score. Its probes have been
  measured against a live endpoint, but a full 200-session run in this mode never has
  (the free-tier quota does not stretch to it), so it is opt-in and experimental.

Standard library only. `urllib.request` keeps `requirements.txt` untouched, so the
organizer's `pip install -r requirements.txt` still reproduces the run exactly.
"""

from __future__ import annotations

import json
import os
import re
import socket
import time
import urllib.error
import urllib.request
from typing import Callable


# OpenRouter, because it is the free endpoint a teammate can actually sign up for: no
# identity check, and free model slugs. SiliconFlow (the original default, feature 13) is
# still one `SHOPPING_COPILOT_BASE_URL` away, but its free tier requires mainland-Chinese
# real-name verification, which is why no key was ever obtained against it.
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"

# Chosen by measurement, not by reading model cards: `py tools/benchmark_llms.py` against
# every free slug that would answer, twice. It is the only one that scored 100% on all four
# probes (JSON parse rate, slot accuracy, price accuracy, expansion-term recall) at ~1.5s
# mean -- comfortably inside the 4.5s BREAKER_SLOW_MS threshold -- and it reproduced those
# numbers exactly on a second run. What the field looked like:
#
#   inclusionai/ling-3.0-flash-fin  100/100/100/100  ~1.5s   <- this one
#   nvidia/nemotron-3-super-120b     80/100/100/50   4-11s   leaks reasoning prose into
#                                                            `content`; latency unstable
#   google/gemma-4-26b-a4b-it        rate-limited upstream (shared Google AI Studio pool)
#   nvidia/nemotron-3.5-lightning     0/0/0/0        ~8.7s   tripped the breaker outright
#   liquid/lfm-2.5-2.6b, minimax-m2.7 20/0/0/0              cannot hold the JSON contract
#
# Caveat worth knowing: the `-fin` suffix is a finance-tuned variant. That looks wrong for a
# clothing catalog, and it is the first thing to re-measure if quality ever looks off -- but
# it beat every general-purpose free slug on our own probes, twice, and the probes are the
# job. `openrouter/free` was rejected outright regardless of score: it picks a free model at
# random per call, so the feasibility disclosure could not name a model.
#
# Two live constraints worth knowing before trusting any of the above:
#
#   - **OpenRouter's free tier is 50 requests per DAY** on a keyless-credit account, shared
#     across every model. The benchmark above spends ~5 per model per run, and exhausting
#     it returns 429 on everything -- which looks exactly like a broken key if you are not
#     expecting it. `X-RateLimit-Remaining` in the 429 body tells you.
#   - The free pools are also rate-limited *upstream*, per model and independently of your
#     quota, which is what took both Google slugs out during the measurement above.
#
# So a failing default is more likely to be quota than a bad id. Free slugs do also come
# and go: if this one starts 404ing, run the benchmark again and pick the winner -- see
# docs/LLM_SETUP.md. Nothing depends on this exact value; it is only what you get when you
# set a key and nothing else.
DEFAULT_MODEL = "inclusionai/ling-3.0-flash-fin:free"

# Provider-neutral, and all one family with the mode variable that was always called this.
# The client is plain OpenAI-compatible chat completions, so naming these after one vendor
# was wrong the moment the default moved off it.
MODE_ENV = "SHOPPING_COPILOT_LLM"
API_KEY_ENV = "SHOPPING_COPILOT_API_KEY"
MODEL_ENV = "SHOPPING_COPILOT_MODEL"
BASE_URL_ENV = "SHOPPING_COPILOT_BASE_URL"

# The names feature 13 shipped with. Still honoured, so a teammate's existing shell or
# `.env` keeps working -- but the canonical name above wins when both are set, and
# `src/env_file.py` rewrites the old ones on its next write. Deliberately silent: this
# is read during `Agent()` construction, which is on the scored path, and a deprecation
# warning printed there would land in the middle of an evaluator run.
LEGACY_ENV = {
    API_KEY_ENV: "SILICONFLOW_API_KEY",
    MODEL_ENV: "SILICONFLOW_MODEL",
    BASE_URL_ENV: "SILICONFLOW_BASE_URL",
}


def env_value(name: str) -> str:
    """The canonical variable, falling back to its legacy alias. `""` if neither is set."""
    value = (os.environ.get(name) or "").strip()
    if value:
        return value
    legacy = LEGACY_ENV.get(name)
    return (os.environ.get(legacy) or "").strip() if legacy else ""


MODE_OFF = "off"
MODE_FREEFORM = "freeform"
MODE_EXPAND = "expand"
MODES = (MODE_OFF, MODE_FREEFORM, MODE_EXPAND)

# A turn that waits 30s for a model is a turn the harness may score as a timeout. Six
# seconds is generous for an 8B model returning a handful of tokens, and it bounds the
# worst case: on failure we fall through to the sparse pipeline that scores 0.912205
# on its own.
TIMEOUT_SECONDS = 6.0

# Deliberately no retry. A retry doubles the worst-case latency to buy an outcome we
# already have a good answer for -- the offline pipeline.
MAX_OUTPUT_TOKENS = 256

# Cap on how many expansion terms are allowed to reach retrieval, so a chatty or
# adversarial completion cannot flood the query.
MAX_EXPANSION_TERMS = 8

# --- Circuit breaker -----------------------------------------------------------------
#
# Per-call fail-soft is not enough on its own. With the network down, every turn still
# pays the full TIMEOUT_SECONDS before falling through -- ten turns of a dead endpoint is
# a minute of dead air for an outcome we already know. So repeated failure *latches the
# client off* for the rest of the process: `complete()` then returns None immediately,
# with no socket and no wait, and the agent runs on the offline pipeline that scores
# 0.912205 by itself.
#
# Three ways to trip it, because "unusable" has three shapes:
BREAKER_FAILURES = 3          # consecutive failures of any kind (HTTP 500s, bad JSON)
BREAKER_NETWORK_FAILURES = 2  # consecutive *connection* failures -- no route, DNS dead
BREAKER_SLOW_CALLS = 3        # consecutive successes that were too slow to be worth it
BREAKER_SLOW_MS = 4500.0      # what "too slow" means, against a 6000 ms timeout

# Connection-level failures: the network is unreachable, rather than the service being
# unhappy. Tripped on sooner because retrying is hopeless, not merely unlucky.
_NETWORK_ERRORS = (urllib.error.URLError, TimeoutError, ConnectionError, socket.gaierror,
                   socket.timeout)
# `HTTPError` subclasses `URLError`, but it means we *reached* the service and it answered.
# That is a service problem, not an outage, so it takes the slower failure path.
_REACHED_SERVICE_ERRORS = (urllib.error.HTTPError,)


def _is_network_error(exc: BaseException) -> bool:
    """True when the failure looks like "there is no network", not "the service said no"."""
    if isinstance(exc, _REACHED_SERVICE_ERRORS):
        return False
    return isinstance(exc, _NETWORK_ERRORS)

_TERM_RE = re.compile(r"^[a-z0-9][a-z0-9 \-]{1,28}$")
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)
_JSON_ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)
# Qwen3 emits its reasoning inside these when thinking is left on. Strip rather than
# fail, so a model or endpoint that ignores `enable_thinking` still parses.
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


def _provider_message(raw: bytes) -> str:
    """The provider's own error text, or a trimmed body. Never raises, never leaks a key.

    Providers nest this differently -- OpenRouter puts the useful sentence in
    `error.metadata.raw` for upstream failures and `error.message` for its own, so both are
    checked before falling back to the raw body.
    """
    try:
        document = json.loads(raw.decode("utf-8", "replace"))
    except (ValueError, AttributeError):
        return raw.decode("utf-8", "replace")[:200].strip() if raw else "no response body"
    error = document.get("error") if isinstance(document, dict) else None
    if isinstance(error, dict):
        metadata = error.get("metadata")
        if isinstance(metadata, dict) and isinstance(metadata.get("raw"), str):
            return metadata["raw"][:200].strip()
        if isinstance(error.get("message"), str):
            return error["message"][:200].strip()
    if isinstance(error, str):
        return error[:200].strip()
    return json.dumps(document)[:200]


def urllib_transport(url: str, headers: dict, body: bytes, timeout: float) -> tuple[int, bytes]:
    """The real HTTP call. Swapped for a stub in tests so they need no key and no network."""
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), response.read()
    except urllib.error.HTTPError as error:
        # `urlopen` raises on any non-2xx, which threw away the response body -- and the
        # body is where the provider explains itself ("Rate limit exceeded:
        # free-models-per-day"). Without this the operator saw only urllib's generic
        # "HTTP Error 429: Too Many Requests" and could not tell a quota from a bad key.
        # Returned rather than re-raised so it takes the same path as a stubbed non-200:
        # `complete()` builds the message and the breaker still classifies it as a service
        # failure (we reached the service), not a network outage.
        return int(error.code), error.read()


class SiliconFlowClient:
    """A minimal OpenAI-compatible chat client that never raises.

    Tracks its own tokens and latency so `Agent` can report honest `usage` and so the
    feasibility disclosure has real numbers instead of an estimate.
    """

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = TIMEOUT_SECONDS,
        transport: Callable[[str, dict, bytes, float], tuple[int, bytes]] | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._transport = transport or urllib_transport
        # Identical prompts recur constantly within a session (the evidence text grows by
        # one disclosure per turn, and sessions repeat phrasings), so caching cuts both
        # latency and the token bill without changing any answer.
        self._cache: dict[tuple, str | None] = {}
        self.calls = 0
        self.failures = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.latencies_ms: list[float] = []
        # Circuit breaker. `disabled` latches True and every later call short-circuits to
        # None without touching the network -- see the BREAKER_* constants above.
        self.disabled = False
        self.disabled_reason: str | None = None
        # Why the most recent call failed, for the operator. None until something does.
        self.last_error: str | None = None
        self.skipped = 0
        self._failure_streak = 0
        self._network_streak = 0
        self._slow_streak = 0

    # -- circuit breaker ---------------------------------------------------------

    def trip(self, reason: str) -> None:
        """Latch the client off for the rest of the process. Idempotent."""
        if not self.disabled:
            self.disabled = True
            self.disabled_reason = reason

    def reenable(self) -> None:
        """Close the breaker again and forget the streaks.

        For the WebUI's "retry" affordance: the operator plugged the network back in and
        wants another go without restarting a 15-second index build. Nothing calls this
        automatically -- an automatic half-open retry would put the timeout back on the
        turn we tripped the breaker to protect.
        """
        self.disabled = False
        self.disabled_reason = None
        self.last_error = None
        self._failure_streak = 0
        self._network_streak = 0
        self._slow_streak = 0

    def _note_success(self, elapsed_ms: float) -> None:
        self.last_error = None
        self._failure_streak = 0
        self._network_streak = 0
        if elapsed_ms > BREAKER_SLOW_MS:
            self._slow_streak += 1
            if self._slow_streak >= BREAKER_SLOW_CALLS:
                self.trip(
                    "%d consecutive calls slower than %.0f ms"
                    % (self._slow_streak, BREAKER_SLOW_MS)
                )
        else:
            self._slow_streak = 0

    def _note_failure(self, exc: BaseException) -> None:
        self._failure_streak += 1
        self._slow_streak = 0
        if _is_network_error(exc):
            self._network_streak += 1
            if self._network_streak >= BREAKER_NETWORK_FAILURES:
                self.trip(
                    "%d consecutive connection failures (%s) -- treating the network as down"
                    % (self._network_streak, type(exc).__name__)
                )
                return
        else:
            self._network_streak = 0
        if self._failure_streak >= BREAKER_FAILURES:
            self.trip("%d consecutive failed calls" % self._failure_streak)

    def complete(self, system: str, user: str, max_tokens: int = MAX_OUTPUT_TOKENS) -> str | None:
        """One chat completion, or None on any failure whatsoever."""
        key = (system, user, max_tokens)
        if key in self._cache:
            return self._cache[key]

        # Breaker open: answer immediately with the same None a failure would produce, so
        # every caller's existing fallback handles it and no turn waits on a dead socket.
        if self.disabled:
            self.skipped += 1
            return None

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            # Greedy decoding: the closest this endpoint gets to reproducible. Server-side
            # batching still means it is not *guaranteed* deterministic -- see the caveat
            # in docs/features/13-optional-llm.md.
            "temperature": 0.0,
            "top_p": 1.0,
            "stream": False,
        }
        if "qwen3" in self.model.lower() and "siliconflow" in self.base_url.lower():
            # SiliconFlow's Qwen3 switch for hybrid reasoning. Gated on BOTH halves,
            # because an endpoint that does not know the field may reject the whole
            # request. The model half alone is not enough: the same weights are served
            # under ids that match "qwen3" elsewhere (OpenRouter's `qwen/qwen3-8b:free`,
            # Ollama's `qwen3:8b`), and this field is SiliconFlow's, not OpenAI's. Any
            # OpenAI-compatible endpoint works here -- see docs/LLM_SETUP.md.
            payload["enable_thinking"] = False
        # No OpenRouter equivalent is sent, and that is a measured decision rather than an
        # oversight. Most free slugs there are reasoning models that return the trace in a
        # separate `reasoning` field and, when they ramble, spend the whole `max_tokens`
        # budget on it and hand back `content: null` with finish_reason "length". Three
        # candidate switches were tried against the live endpoint:
        #   reasoning={"enabled": False}  -> nulled content outright. Worse.
        #   reasoning={"exclude": True}   -> hides the trace but still generates it, so it
        #                                    does not free the budget. No measured benefit.
        #   reasoning={"effort": "low"}   -> the run was contaminated by a rate limit
        #                                    (see below); no trustworthy reading.
        # Baseline with no reasoning field scored best of the lot (5/6 usable replies at
        # max_tokens=256) before the daily free quota ran out, so plain OpenAI it stays.
        # `complete()` already treats a null content as a failed call and the agent falls
        # through to offline retrieval, which is the behaviour we actually rely on.

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        body = json.dumps(payload).encode("utf-8")
        started = time.perf_counter()
        self.calls += 1
        try:
            status, raw = self._transport(
                f"{self.base_url}/chat/completions", headers, body, self.timeout
            )
            if status != 200:
                # Carry the provider's own words up with the status. Without this a 429
                # ("you are out of free requests until 08:00") and a 401 ("your key is
                # wrong") both surfaced to the operator as the identical, useless "the
                # call failed" -- which is the hardest possible version of this to debug.
                raise OSError("HTTP %d: %s" % (status, _provider_message(raw)))
            document = json.loads(raw.decode("utf-8"))
            content = document["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise ValueError("content was not a string")
            usage = document.get("usage") or {}
            # Reported to the evaluator, so only trust well-formed non-negative ints.
            for field, attribute in (
                ("prompt_tokens", "prompt_tokens"),
                ("completion_tokens", "completion_tokens"),
            ):
                value = usage.get(field)
                if isinstance(value, int) and value >= 0:
                    setattr(self, attribute, getattr(self, attribute) + value)
            text = _THINK_RE.sub("", content).strip()
            self._cache[key] = text
            self._note_success((time.perf_counter() - started) * 1000.0)
            return text
        except Exception as exc:  # noqa: BLE001 - a model failure must never fail the turn
            self.failures += 1
            self._cache[key] = None
            # Kept so the WebUI and llm_smoke can say WHY, not just "it failed". Never
            # includes the key: only the status line and the provider's message.
            self.last_error = "%s: %s" % (type(exc).__name__, exc) if not isinstance(
                exc, OSError
            ) else str(exc)
            self._note_failure(exc)
            return None
        finally:
            self.latencies_ms.append((time.perf_counter() - started) * 1000.0)

    def stats(self) -> dict:
        """Feasibility disclosure for the model, mirroring Agent.latency_stats()."""
        return {
            "model": self.model,
            "base_url": self.base_url,
            "calls": self.calls,
            "failures": self.failures,
            "skipped": self.skipped,
            "disabled": self.disabled,
            "disabled_reason": self.disabled_reason,
            "last_error": self.last_error,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "mean_ms": round(sum(self.latencies_ms) / len(self.latencies_ms), 2)
            if self.latencies_ms
            else 0.0,
        }


def resolve_mode(raw: str | None) -> str:
    """Normalize the mode env var. Anything unrecognized is treated as `off`.

    Failing closed matters: a typo in a demo shell must degrade to the known-good
    offline agent, never to some half-configured state.
    """
    value = (raw or "").strip().lower()
    return value if value in MODES else MODE_OFF


def client_from_env(
    transport: Callable[[str, dict, bytes, float], tuple[int, bytes]] | None = None,
) -> tuple[SiliconFlowClient | None, str]:
    """`(client, mode)` from the environment. `(None, "off")` unless fully configured.

    Both an API key *and* an explicit mode are required. Neither alone turns the model
    on, so a stray key in a teammate's shell cannot silently change a scored run.

    Every variable is read through `env_value`, so the legacy `SILICONFLOW_*` names still
    work and the canonical `SHOPPING_COPILOT_*` ones win when both are set.
    """
    mode = resolve_mode(os.environ.get(MODE_ENV))
    api_key = env_value(API_KEY_ENV)
    if mode == MODE_OFF or not api_key:
        return None, MODE_OFF
    client = SiliconFlowClient(
        api_key=api_key,
        model=env_value(MODEL_ENV) or DEFAULT_MODEL,
        base_url=env_value(BASE_URL_ENV) or DEFAULT_BASE_URL,
        transport=transport,
    )
    return client, mode


# --- Response parsing ---------------------------------------------------------------
#
# Model output is untrusted input. It is parsed defensively and every value is
# type-checked and clamped before it can reach a slot, a filter or a query.


def _strip_to_json(text: str, pattern: re.Pattern) -> object | None:
    cleaned = _FENCE_RE.sub("", text).strip()
    match = pattern.search(cleaned)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except (ValueError, TypeError):
        return None


def _clean_term(value: object) -> str | None:
    """A model-proposed keyword, or None if it is not one we are willing to search on."""
    if not isinstance(value, str):
        return None
    term = " ".join(value.strip().lower().split())
    return term if _TERM_RE.match(term) else None


FREEFORM_SYSTEM = (
    "You extract shopping constraints from one shopper message. "
    "Reply with ONLY a JSON object, no prose and no code fence, with keys: "
    '"color" (string or null), "material" (string or null), '
    '"price_max" (number or null, a budget ceiling in dollars, never a measurement), '
    'and "keywords" (array of up to 6 short lowercase product phrases the shopper implied). '
    "Use null when the shopper did not say. Do not invent preferences."
)

EXPAND_SYSTEM = (
    "You help search a clothing, shoes and jewelry catalog. "
    "Given what a shopper has said, reply with ONLY a JSON array of up to 8 short "
    "lowercase keywords or two-word phrases that would literally appear in the title or "
    "feature bullets of a matching product. No prose, no code fence, no explanations. "
    "Only words justified by what the shopper said; do not invent new constraints."
)


def parse_freeform(client: SiliconFlowClient, message: str) -> dict | None:
    """Slots + keywords from a human's free prose, or None if the model gave us nothing.

    Only ever called from the free-form branch, which the simulated customer cannot
    reach -- so nothing here can move the competition score.
    """
    if not message or not message.strip():
        return None
    text = client.complete(FREEFORM_SYSTEM, message.strip())
    if not text:
        return None
    document = _strip_to_json(text, _JSON_OBJECT_RE)
    if not isinstance(document, dict):
        return None

    result: dict = {"color": None, "material": None, "price_max": None, "keywords": []}
    for slot in ("color", "material"):
        value = document.get(slot)
        if isinstance(value, str):
            cleaned = " ".join(value.strip().lower().split())
            # One word only. A slot value becomes an FTS5 AND term, and a multi-word
            # hallucination there would filter the catalog down to nothing.
            if cleaned and " " not in cleaned and _TERM_RE.match(cleaned):
                result[slot] = cleaned

    price = document.get("price_max")
    if isinstance(price, bool):
        price = None
    if isinstance(price, (int, float)) and 0 < float(price) < 100_000:
        result["price_max"] = float(price)

    keywords = document.get("keywords")
    if isinstance(keywords, list):
        for item in keywords[:6]:
            term = _clean_term(item)
            if term and term not in result["keywords"]:
                result["keywords"].append(term)
    return result


def expand_query(client: SiliconFlowClient, evidence: str) -> list[str]:
    """Extra retrieval keywords for the accumulated evidence. `[]` on any failure."""
    if not evidence or not evidence.strip():
        return []
    text = client.complete(EXPAND_SYSTEM, evidence.strip())
    if not text:
        return []
    document = _strip_to_json(text, _JSON_ARRAY_RE)
    if not isinstance(document, list):
        return []
    terms: list[str] = []
    for item in document:
        term = _clean_term(item)
        if term and term not in terms:
            terms.append(term)
        if len(terms) >= MAX_EXPANSION_TERMS:
            break
    return terms
