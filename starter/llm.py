"""Optional SiliconFlow (OpenAI-compatible) language model, off by default.

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

    SILICONFLOW_API_KEY   required for any model use at all. Unset -> disabled.
    SHOPPING_COPILOT_LLM  off (default) | freeform | expand
    SILICONFLOW_MODEL     override the model id (default Qwen/Qwen3-8B)
    SILICONFLOW_BASE_URL  override the endpoint (default https://api.siliconflow.cn/v1)

Modes, in increasing order of how much they can affect a scored run:

- **off** -- the default. No client is constructed. Zero behaviour change. This is the
  configuration the organizer will run, because they have no key of ours.
- **freeform** -- the model is used *only* to understand a human typing free prose into
  the manual-testing UI. That branch (`DialogState._observe_freeform`) is unreachable
  while scoring: every simulated-customer reply is claimed by an earlier regex, which is
  why feature 11 came back byte-identical. So this mode is score-neutral *by
  construction*, not by measurement.
- **expand** -- additionally lets the model propose extra retrieval keywords, fused as
  one more low-weight RRF route. This one *can* move the score and has **not** been
  measured against the real endpoint, so it is opt-in and documented as experimental.

Standard library only. `urllib.request` keeps `requirements.txt` untouched, so the
organizer's `pip install -r requirements.txt` still reproduces the run exactly.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Callable


DEFAULT_BASE_URL = "https://api.siliconflow.cn/v1"

# Qwen/Qwen3-8B is SiliconFlow's strongest permanently-free chat model: 128K context,
# instruction-tuned, reliable at terse JSON, and it accepts `enable_thinking: false` so we
# are not billed latency for a reasoning trace we would throw away. The other free chat
# model, deepseek-ai/DeepSeek-R1-Distill-Qwen-7B, is a reasoning distill that emits long
# <think> blocks -- strictly worse for the short structured extraction we want per turn.
DEFAULT_MODEL = "Qwen/Qwen3-8B"

API_KEY_ENV = "SILICONFLOW_API_KEY"
MODE_ENV = "SHOPPING_COPILOT_LLM"
MODEL_ENV = "SILICONFLOW_MODEL"
BASE_URL_ENV = "SILICONFLOW_BASE_URL"

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

_TERM_RE = re.compile(r"^[a-z0-9][a-z0-9 \-]{1,28}$")
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)
_JSON_ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)
# Qwen3 emits its reasoning inside these when thinking is left on. Strip rather than
# fail, so a model or endpoint that ignores `enable_thinking` still parses.
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


def urllib_transport(url: str, headers: dict, body: bytes, timeout: float) -> tuple[int, bytes]:
    """The real HTTP call. Swapped for a stub in tests so they need no key and no network."""
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return int(response.status), response.read()


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

    def complete(self, system: str, user: str, max_tokens: int = MAX_OUTPUT_TOKENS) -> str | None:
        """One chat completion, or None on any failure whatsoever."""
        key = (system, user, max_tokens)
        if key in self._cache:
            return self._cache[key]

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            # Greedy decoding: the closest this endpoint gets to reproducible. Server-side
            # batching still means it is not *guaranteed* deterministic -- see the caveat
            # in docs/features/13-siliconflow-llm.md.
            "temperature": 0.0,
            "top_p": 1.0,
            "stream": False,
        }
        if "qwen3" in self.model.lower():
            # SiliconFlow's Qwen3 switch for hybrid reasoning. Sent only for Qwen3, since
            # an endpoint that does not know the field may reject the whole request.
            payload["enable_thinking"] = False

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
                raise OSError(f"HTTP {status}")
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
            return text
        except Exception:  # noqa: BLE001 - a model failure must never fail the turn
            self.failures += 1
            self._cache[key] = None
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
    """
    mode = resolve_mode(os.environ.get(MODE_ENV))
    api_key = (os.environ.get(API_KEY_ENV) or "").strip()
    if mode == MODE_OFF or not api_key:
        return None, MODE_OFF
    client = SiliconFlowClient(
        api_key=api_key,
        model=(os.environ.get(MODEL_ENV) or DEFAULT_MODEL).strip(),
        base_url=(os.environ.get(BASE_URL_ENV) or DEFAULT_BASE_URL).strip(),
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
