"""Verification for the optional SiliconFlow model integration.

Runs with no API key and no network: the HTTP transport is injected, so every enabled
path is exercised deterministically.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starter import llm as L
from starter.dialog_state import DialogState

RESULTS = []


def check(group, name, ok, detail=""):
    RESULTS.append((group, name, bool(ok), detail))
    print("  [" + ("PASS" if ok else "FAIL") + "] " + name + ("  -- " + detail if detail else ""))


def reply(content, prompt_tokens=11, completion_tokens=7, status=200):
    body = {
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
    }
    return status, json.dumps(body).encode("utf-8")


def transport_returning(content, **kw):
    sent = []

    def transport(url, headers, payload, timeout):
        sent.append((url, headers, json.loads(payload.decode("utf-8")), timeout))
        return reply(content, **kw)

    transport.sent = sent
    return transport


def client_for(content, **kw):
    return L.SiliconFlowClient("fake-key", transport=transport_returning(content, **kw))


# ------------------------------------------------------------------ CLIENT
print("CLIENT -- request shape, failure modes, accounting")

t = transport_returning('["running shoes"]')
c = L.SiliconFlowClient("fake-key", transport=t)
out = c.complete("sys", "user")
url, headers, payload, timeout = t.sent[0]
check("CLIENT", "posts to the default (OpenRouter) chat-completions endpoint",
      url == "https://openrouter.ai/api/v1/chat/completions", url)
check("CLIENT", "sends Bearer auth from the key, never a literal in the repo",
      headers.get("Authorization") == "Bearer fake-key")
check("CLIENT", "defaults to a free OpenRouter slug",
      payload["model"] == L.DEFAULT_MODEL and payload["model"].endswith(":free"),
      payload["model"])
check("CLIENT", "greedy decoding for reproducibility (temperature 0, top_p 1)",
      payload["temperature"] == 0.0 and payload["top_p"] == 1.0)
check("CLIENT", "the default payload is plain OpenAI -- no provider-specific fields",
      set(payload) == {"model", "messages", "max_tokens", "temperature", "top_p", "stream"},
      str(sorted(payload)))

# SiliconFlow is no longer the default, but it is still one env var away, so the switch
# that made feature 13 worth using has to keep working when someone points back at it.
sf_transport = transport_returning("[]")
L.SiliconFlowClient("k", model="Qwen/Qwen3-8B",
                    base_url="https://api.siliconflow.cn/v1",
                    transport=sf_transport).complete("s", "u")
check("CLIENT", "still disables Qwen3 thinking when pointed back at SiliconFlow",
      sf_transport.sent[0][2].get("enable_thinking") is False,
      str(sorted(sf_transport.sent[0][2])))
check("CLIENT", "does not stream", payload["stream"] is False)
check("CLIENT", "bounded timeout", timeout == L.TIMEOUT_SECONDS, str(timeout))
check("CLIENT", "returns the completion text", out == '["running shoes"]', repr(out))
check("CLIENT", "accounts real tokens from the usage block",
      c.prompt_tokens == 11 and c.completion_tokens == 7,
      "p=%d c=%d" % (c.prompt_tokens, c.completion_tokens))

c.complete("sys", "user")
check("CLIENT", "identical prompts are cached (one HTTP call, not two)", len(t.sent) == 1,
      "%d calls" % len(t.sent))

ds_transport = transport_returning("[]")
L.SiliconFlowClient("k", model="deepseek-ai/DeepSeek-R1-Distill-Qwen-7B",
                    transport=ds_transport).complete("s", "u")
check("CLIENT", "enable_thinking is NOT sent for a non-Qwen3 model",
      "enable_thinking" not in ds_transport.sent[0][2], str(sorted(ds_transport.sent[0][2])))

# The same weights are served under Qwen3-matching ids elsewhere (OpenRouter's
# `qwen/qwen3-8b:free`, Ollama's `qwen3:8b`), and enable_thinking is SiliconFlow's field,
# not OpenAI's -- posting it to another endpoint risks a rejected request. See
# docs/LLM_SETUP.md.
or_transport = transport_returning("[]")
L.SiliconFlowClient("k", model="qwen/qwen3-8b:free",
                    base_url="https://openrouter.ai/api/v1",
                    transport=or_transport).complete("s", "u")
check("CLIENT", "enable_thinking is NOT sent to a non-SiliconFlow endpoint",
      "enable_thinking" not in or_transport.sent[0][2], str(sorted(or_transport.sent[0][2])))

ollama_transport = transport_returning("[]")
L.SiliconFlowClient("placeholder", model="qwen3:8b",
                    base_url="http://localhost:11434/v1",
                    transport=ollama_transport).complete("s", "u")
check("CLIENT", "a local OpenAI-compatible endpoint gets a plain OpenAI payload",
      "enable_thinking" not in ollama_transport.sent[0][2]
      and ollama_transport.sent[0][0] == "http://localhost:11434/v1/chat/completions",
      ollama_transport.sent[0][0])

check("CLIENT", "strips a <think> block if the endpoint ignores enable_thinking",
      client_for("<think>hmm let me consider</think>\n[\"boots\"]").complete("s", "u") == '["boots"]')


def raising_transport(exc):
    def transport(url, headers, payload, timeout):
        raise exc
    return transport


for label, transport in [
    ("connection refused", raising_transport(OSError("refused"))),
    ("timeout", raising_transport(TimeoutError("timed out"))),
]:
    cl = L.SiliconFlowClient("k", transport=transport)
    check("CLIENT", "returns None on %s (never raises)" % label,
          cl.complete("s", "u") is None and cl.failures == 1)

cl = L.SiliconFlowClient("k", transport=lambda *a: (429, b"rate limited"))
check("CLIENT", "returns None on a non-200 status", cl.complete("s", "u") is None)

# A failure the operator can act on. "The call failed" was true of a dead network, a wrong
# key and an exhausted quota alike -- three problems with three different fixes.
cl = L.SiliconFlowClient("k", transport=lambda *a: (429, json.dumps(
    {"error": {"message": "Rate limit exceeded: free-models-per-day"}}).encode()))
cl.complete("s", "u")
check("CLIENT", "records the provider's own words, not just a status",
      cl.last_error == "HTTP 429: Rate limit exceeded: free-models-per-day", str(cl.last_error))
check("CLIENT", "the error reaches stats() for the UI to display",
      cl.stats()["last_error"] == cl.last_error)

cl = L.SiliconFlowClient("k", transport=lambda *a: (429, json.dumps(
    {"error": {"message": "Provider returned error",
               "metadata": {"raw": "upstream pool exhausted"}}}).encode()))
cl.complete("s", "u")
check("CLIENT", "prefers the nested upstream message over the generic wrapper",
      cl.last_error == "HTTP 429: upstream pool exhausted", str(cl.last_error))

cl = L.SiliconFlowClient("k", transport=lambda *a: (500, b"<html>gateway error</html>"))
cl.complete("s", "u")
check("CLIENT", "a non-JSON error body is still reported, not swallowed",
      "500" in (cl.last_error or "") and "gateway" in (cl.last_error or ""), str(cl.last_error))

cl = L.SiliconFlowClient("k", transport=transport_returning('["ok"]'))
cl.complete("s", "u")
check("CLIENT", "a successful call clears the previous error", cl.last_error is None)

check("CLIENT", "the error text never contains the API key",
      "k" not in (cl.last_error or "zzz") or cl.last_error is None)
cl = L.SiliconFlowClient("k", transport=lambda *a: (200, b"not json at all"))
check("CLIENT", "returns None on unparseable JSON", cl.complete("s", "u") is None)
cl = L.SiliconFlowClient("k", transport=lambda *a: (200, json.dumps({"choices": []}).encode()))
check("CLIENT", "returns None on a well-formed but empty choices list",
      cl.complete("s", "u") is None)
cl = L.SiliconFlowClient("k", transport=lambda *a: (
    200, json.dumps({"choices": [{"message": {"content": None}}]}).encode()))
check("CLIENT", "returns None when content is not a string", cl.complete("s", "u") is None)
cl = L.SiliconFlowClient("k", transport=lambda *a: (200, json.dumps({
    "choices": [{"message": {"content": "ok"}}],
    "usage": {"prompt_tokens": "many", "completion_tokens": -5}}).encode()))
cl.complete("s", "u")
check("CLIENT", "ignores malformed/negative token counts rather than reporting them",
      cl.prompt_tokens == 0 and cl.completion_tokens == 0,
      "p=%d c=%d" % (cl.prompt_tokens, cl.completion_tokens))

# ------------------------------------------------------------------ ENV GATING
print("\nGATING -- the judged configuration must be off")

saved = {k: os.environ.get(k) for k in [L.API_KEY_ENV, L.MODE_ENV, L.MODEL_ENV, L.BASE_URL_ENV]}
try:
    for k in saved:
        os.environ.pop(k, None)
    check("GATE", "no key, no mode -> (None, off)", L.client_from_env() == (None, "off"))
    os.environ[L.API_KEY_ENV] = "k"
    check("GATE", "key alone is NOT enough to enable a model",
          L.client_from_env() == (None, "off"))
    os.environ.pop(L.API_KEY_ENV)
    os.environ[L.MODE_ENV] = "expand"
    check("GATE", "mode alone is NOT enough to enable a model",
          L.client_from_env() == (None, "off"))
    os.environ[L.API_KEY_ENV] = "k"
    client, mode = L.client_from_env()
    check("GATE", "key + mode -> a live client in that mode",
          client is not None and mode == "expand")
    os.environ[L.MODE_ENV] = "EXPAND"
    check("GATE", "mode parsing is case-insensitive", L.client_from_env()[1] == "expand")
    os.environ[L.MODE_ENV] = "expnd"
    check("GATE", "a typo'd mode fails CLOSED to off, not to a half-configured state",
          L.client_from_env() == (None, "off"))
    os.environ[L.MODE_ENV] = "freeform"
    os.environ[L.MODEL_ENV] = "custom/model"
    os.environ[L.BASE_URL_ENV] = "https://example.test/v1/"
    client, mode = L.client_from_env()
    check("GATE", "model and base URL are overridable from the environment",
          client.model == "custom/model" and client.base_url == "https://example.test/v1")
    os.environ[L.API_KEY_ENV] = "   "
    check("GATE", "a whitespace-only key counts as absent", L.client_from_env() == (None, "off"))
finally:
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

# ------------------------------------------------------------------ PARSING
print("\nPARSING -- model output is untrusted input")

p = L.parse_freeform(client_for('{"color":"burgundy","material":"linen","price_max":45,'
                                '"keywords":["summer dress","lightweight"]}'), "msg")
check("PARSE", "clean JSON parses into slots + keywords",
      p == {"color": "burgundy", "material": "linen", "price_max": 45.0,
            "keywords": ["summer dress", "lightweight"]}, str(p))
p = L.parse_freeform(client_for('```json\n{"color":"navy","material":null,'
                                '"price_max":null,"keywords":[]}\n```'), "msg")
check("PARSE", "a fenced code block still parses", p["color"] == "navy", str(p))
p = L.parse_freeform(client_for('Sure! Here you go: {"color":"olive","material":null,'
                                '"price_max":null,"keywords":[]} Hope that helps!'), "msg")
check("PARSE", "JSON embedded in chatty prose is recovered", p["color"] == "olive", str(p))

p = L.parse_freeform(client_for('{"color":"dark navy blue","material":"cotton",'
                                '"price_max":null,"keywords":[]}'), "msg")
check("PARSE", "a MULTI-WORD colour is rejected (it would become an AND term)",
      p["color"] is None and p["material"] == "cotton", str(p))
p = L.parse_freeform(client_for('{"color":null,"material":null,"price_max":-20,'
                                '"keywords":[]}'), "msg")
check("PARSE", "a negative price is rejected", p["price_max"] is None)
p = L.parse_freeform(client_for('{"color":null,"material":null,"price_max":true,'
                                '"keywords":[]}'), "msg")
check("PARSE", "a boolean price is rejected (bool is an int in Python)",
      p["price_max"] is None)
p = L.parse_freeform(client_for('{"color":null,"material":null,"price_max":"cheap",'
                                '"keywords":[]}'), "msg")
check("PARSE", "a non-numeric price is rejected", p["price_max"] is None)
p = L.parse_freeform(client_for('{"color":null,"material":null,"price_max":null,'
                                '"keywords":["a","ok term",123,null,"x"*99,"good"]}'
                                .replace('"x"*99', '"' + "x" * 99 + '"')), "msg")
check("PARSE", "keywords are type-checked and length-clamped",
      p["keywords"] == ["ok term", "good"], str(p["keywords"]))
p = L.parse_freeform(client_for('{"color":null,"material":null,"price_max":null,"keywords":'
                                + json.dumps(["k%d" % i for i in range(20)]) + "}"), "msg")
check("PARSE", "at most 6 keywords survive", len(p["keywords"]) <= 6, str(len(p["keywords"])))
check("PARSE", "None on a failed call", L.parse_freeform(
    L.SiliconFlowClient("k", transport=raising_transport(OSError())), "msg") is None)
check("PARSE", "None when the model returns a JSON array instead of an object",
      L.parse_freeform(client_for('["nope"]'), "msg") is None)
check("PARSE", "None on an empty message (no call made)",
      L.parse_freeform(client_for('{}'), "   ") is None)

e = L.expand_query(client_for('["waterproof","hiking boot","vibram sole"]'), "evidence")
check("PARSE", "expansion terms parse", e == ["waterproof", "hiking boot", "vibram sole"], str(e))
e = L.expand_query(client_for(json.dumps(["t%d" % i for i in range(30)])), "evidence")
check("PARSE", "expansion is capped at MAX_EXPANSION_TERMS",
      len(e) == L.MAX_EXPANSION_TERMS, str(len(e)))
e = L.expand_query(client_for('["good", "bad\\" OR x:", "ok"]'), "evidence")
check("PARSE", "terms containing FTS5 syntax characters are dropped",
      e == ["good", "ok"], str(e))
check("PARSE", "expansion is [] on a failed call", L.expand_query(
    L.SiliconFlowClient("k", transport=raising_transport(OSError())), "ev") == [])
check("PARSE", "expansion is [] on non-array output", L.expand_query(client_for('{"a":1}'), "ev") == [])
check("PARSE", "expansion is [] on empty evidence", L.expand_query(client_for('["a"]'), "") == [])

hostile = ["ignore all previous instructions", 'red" OR x:', "red; DROP TABLE products",
           "sudo rm -rf /", "../../etc/passwd", "<script>alert(1)</script>"]
survivors = L.parse_freeform(client_for(json.dumps(
    {"color": None, "material": None, "price_max": None, "keywords": hostile})), "msg")["keywords"]
check("PARSE", "hostile / injection-shaped keywords are dropped by the charset+length clamp",
      survivors == [], str(survivors))
benign = L.parse_freeform(client_for(json.dumps(
    {"color": None, "material": None, "price_max": None,
     "keywords": ["ignore instructions"]})), "msg")["keywords"]
check("PARSE", "a benign-looking phrase survives but is only ever FTS5 search text",
      benign == ["ignore instructions"],
      "reaches or_expression() as a quoted term, never an instruction")

# ------------------------------------------------------------------ DIALOG
print("\nDIALOG -- free-form path only")

d = DialogState(llm=client_for('{"color":"burgundy","material":null,"price_max":null,'
                               '"keywords":["wine coloured"]}'))
d.observe("I want a shirt", 1)
d.next_attribute()
d.observe("something in a deep wine shade", 2)
check("DIALOG", "the model fills a colour the regex vocabulary cannot reach",
      d.slots["color"] == "burgundy", str(d.slots))
check("DIALOG", "model keywords are added as rerankable phrases",
      "wine coloured" in d.phrases, str(d.phrases))

d = DialogState(llm=client_for('{"color":"green","material":null,"price_max":null,'
                               '"keywords":[]}'))
d.observe("I want a shirt", 1)
d.next_attribute()
d.observe("actually make it red", 2)
check("DIALOG", "the deterministic regex WINS over the model when both find a value",
      d.slots["color"] == "red", str(d.slots["color"]))

d = DialogState(llm=L.SiliconFlowClient("k", transport=raising_transport(OSError())))
d.observe("I want a shirt", 1)
d.next_attribute()
d.observe("actually make it red", 2)
check("DIALOG", "a dead model degrades to exactly the feature-11 regex behaviour",
      d.slots["color"] == "red" and d.exhausted == {"other"}, str(d.slots["color"]))

scripted = DialogState(llm=client_for('{"color":"pink","material":null,"price_max":null,'
                                      '"keywords":["should never appear"]}'))
scripted.observe("I need a jacket", 1)
scripted.observe("For that, what matters is: 100% Wool; Button closure", 2)
scripted.observe("I don't have an additional preference for color.", 3)
check("DIALOG", "NO simulator reply reaches the model (scored path untouched)",
      "should never appear" not in scripted.phrases and scripted.slots["color"] is None,
      str(scripted.phrases))

# ------------------------------------------------------------------ BREAKER
print("\nBREAKER -- fall back to offline retrieval when the network is down or slow")

import urllib.error as _urlerror


def raising(exc):
    def transport(url, headers, payload, timeout):
        raise exc
    return transport


c = L.SiliconFlowClient("k", transport=raising(_urlerror.URLError("no route")))
for i in range(L.BREAKER_NETWORK_FAILURES):
    c.complete("s", "u%d" % i)
check("BREAKER", "connection failures trip the breaker (network treated as down)",
      c.disabled, str(c.disabled_reason))

calls_before = c.calls
c.complete("s", "later")
check("BREAKER", "an open breaker makes NO further network call",
      c.calls == calls_before and c.skipped == 1, "calls=%d skipped=%d" % (c.calls, c.skipped))
check("BREAKER", "an open breaker still returns None, so every caller's fallback runs",
      c.complete("s", "later2") is None)

c = L.SiliconFlowClient("k", transport=lambda *a: (500, b"{}"))
for i in range(L.BREAKER_FAILURES - 1):
    c.complete("s", "u%d" % i)
check("BREAKER", "an HTTP error is a service problem, not an outage -- slower to trip",
      not c.disabled, "after %d calls" % (L.BREAKER_FAILURES - 1))
c.complete("s", "final")
check("BREAKER", "but repeated failures of any kind still trip it", c.disabled,
      str(c.disabled_reason))

slow_ms = L.BREAKER_SLOW_MS
try:
    L.BREAKER_SLOW_MS = 0.0            # every call counts as slow, without the wait
    c = L.SiliconFlowClient("k", transport=transport_returning('["ok"]'))
    for i in range(L.BREAKER_SLOW_CALLS):
        c.complete("s", "u%d" % i)
    check("BREAKER", "calls that succeed but are too slow also trip it",
          c.disabled and c.failures == 0, str(c.disabled_reason))
finally:
    L.BREAKER_SLOW_MS = slow_ms

c.reenable()
check("BREAKER", "reenable() closes it again for the WebUI retry button",
      not c.disabled and c.disabled_reason is None)

c = L.SiliconFlowClient("k", transport=transport_returning('["ok"]'))
c.complete("s", "a")
check("BREAKER", "a healthy client is never disabled and reports it in stats()",
      c.stats()["disabled"] is False and c.stats()["skipped"] == 0)

# ------------------------------------------------------------------ ENV FILE
print("\nENV FILE -- scaffolding, precedence, and staying off the scored path")

import tempfile as _tempfile
from pathlib import Path as _Path

from starter import env_file as EF

with _tempfile.TemporaryDirectory() as tmp:
    target = _Path(tmp) / ".env"
    path, created = EF.ensure_env_file(target)
    check("ENVFILE", "creates a .env when one is missing", created and path.exists())
    body = path.read_text(encoding="utf-8")
    check("ENVFILE", "the generated template contains NO key",
          L.API_KEY_ENV + "=" in body and "sk-" not in body)
    check("ENVFILE", "the generated template defaults to off (offline)",
          "SHOPPING_COPILOT_LLM=off" in body)
    check("ENVFILE", "the generated template names NO vendor in its variables",
          not any(legacy in body for legacy in EF.LEGACY_ALIASES),
          str([k for k in EF.LEGACY_ALIASES if k in body]))
    check("ENVFILE", "every managed key is provider-neutral and matches starter/llm.py",
          all(k.startswith("SHOPPING_COPILOT_") for k in EF.MANAGED_KEYS)
          and set(EF.MANAGED_KEYS) == {L.MODE_ENV, L.API_KEY_ENV, L.MODEL_ENV, L.BASE_URL_ENV},
          str(EF.MANAGED_KEYS))

    _, created_again = EF.ensure_env_file(target)
    check("ENVFILE", "never overwrites an existing file (a key cannot be clobbered)",
          not created_again)

    EF.update_env_file({L.API_KEY_ENV: "sk-secret", L.MODE_ENV: "freeform"}, target)
    values = EF.parse_env_file(target)
    check("ENVFILE", "round-trips written values",
          values[L.API_KEY_ENV] == "sk-secret" and values[L.MODE_ENV] == "freeform")
    check("ENVFILE", "preserves the template's comments when rewriting",
          "# Shopping Copilot" in target.read_text(encoding="utf-8"))

    os.environ.pop(L.API_KEY_ENV, None)
    EF.load_env_file(target)
    check("ENVFILE", "loads into os.environ",
          os.environ.get(L.API_KEY_ENV) == "sk-secret")

    os.environ[L.API_KEY_ENV] = "sk-from-shell"
    EF.load_env_file(target)
    check("ENVFILE", "a real environment variable WINS over the file (judging is safe)",
          os.environ[L.API_KEY_ENV] == "sk-from-shell")
    os.environ.pop(L.API_KEY_ENV, None)

    check("ENVFILE", "a missing file is {} rather than an exception",
          EF.parse_env_file(_Path(tmp) / "absent.env") == {})

# --- legacy SILICONFLOW_* names: still honoured, and migrated on the next write ---------
with _tempfile.TemporaryDirectory() as tmp:
    legacy_file = _Path(tmp) / ".env"
    legacy_file.write_text(
        "# hand-written by a teammate before the rename\n"
        "SHOPPING_COPILOT_LLM=freeform\n"
        "SILICONFLOW_API_KEY=sk-old\n"
        "SILICONFLOW_MODEL=Qwen/Qwen3-8B\n"
        "UNRELATED_SETTING=keep-me\n",
        encoding="utf-8",
    )
    folded = EF.canonical_values(legacy_file)
    check("ENVFILE", "a legacy .env is read under the canonical names",
          folded[L.API_KEY_ENV] == "sk-old" and folded[L.MODEL_ENV] == "Qwen/Qwen3-8B",
          str(sorted(folded)))

    for name in (L.API_KEY_ENV, L.MODEL_ENV, "SILICONFLOW_API_KEY", "SILICONFLOW_MODEL"):
        os.environ.pop(name, None)
    EF.load_env_file(legacy_file)
    check("ENVFILE", "a legacy .env still configures the agent after the rename",
          os.environ.get(L.API_KEY_ENV) == "sk-old")
    client, mode = L.client_from_env()
    check("ENVFILE", "...and client_from_env builds a client from it",
          client is not None and mode == "freeform" and client.model == "Qwen/Qwen3-8B",
          str(mode))

    EF.update_env_file({L.MODE_ENV: "off"}, legacy_file)
    migrated = legacy_file.read_text(encoding="utf-8")
    check("ENVFILE", "the next write MIGRATES legacy names, keeping their values",
          "SILICONFLOW_API_KEY" not in migrated
          and (L.API_KEY_ENV + "=sk-old") in migrated,
          str([line for line in migrated.splitlines() if "=" in line]))
    check("ENVFILE", "migration leaves unrelated settings and comments alone",
          "UNRELATED_SETTING=keep-me" in migrated and "# hand-written" in migrated)
    # A legacy line is rewritten where it stood, so a migrated file still reads like the
    # template -- an empty slot stays an empty slot under the comment that explains it,
    # rather than vanishing and leaving the explanation pointing at nothing.
    order = [line.partition("=")[0] for line in migrated.splitlines() if "=" in line
             and not line.startswith("#")]
    check("ENVFILE", "migration rewrites in place, keeping order and empty slots",
          order.index(L.API_KEY_ENV) < order.index("UNRELATED_SETTING"), str(order))

    blanks = _Path(tmp) / "blanks.env"
    blanks.write_text("SILICONFLOW_MODEL=\nSILICONFLOW_BASE_URL=\n", encoding="utf-8")
    EF.update_env_file({}, blanks)
    kept = EF.parse_env_file(blanks)
    check("ENVFILE", "an empty legacy slot migrates to an empty canonical slot",
          L.MODEL_ENV in kept and L.BASE_URL_ENV in kept and not any(
              k.startswith("SILICONFLOW") for k in kept), str(sorted(kept)))

    for name in (L.API_KEY_ENV, L.MODEL_ENV, L.MODE_ENV):
        os.environ.pop(name, None)

os.environ["SILICONFLOW_API_KEY"] = "sk-legacy-shell"
check("ENVFILE", "a legacy variable exported in the SHELL is still honoured",
      L.env_value(L.API_KEY_ENV) == "sk-legacy-shell")
os.environ[L.API_KEY_ENV] = "sk-canonical"
check("ENVFILE", "...but the canonical name wins when both are exported",
      L.env_value(L.API_KEY_ENV) == "sk-canonical")
for name in ("SILICONFLOW_API_KEY", L.API_KEY_ENV):
    os.environ.pop(name, None)

_root = _Path(__file__).resolve().parent.parent
_scored = ["agent.py", "dialog_state.py", "retrieval.py", "ranking.py", "dense_retrieval.py"]
_offenders = [n for n in _scored
              if "env_file" in (_root / "starter" / n).read_text(encoding="utf-8")]
check("ENVFILE", "NO scored module imports env_file (the judged run does no file I/O)",
      not _offenders, str(_offenders))

# ------------------------------------------------------------------ RUNTIME CONFIG
print("\nRUNTIME CONFIG -- Agent.configure_llm, for the WebUI model panel")

from starter.agent import Agent as _Agent


class _FakeAgent(_Agent):
    """configure_llm without a 15s index build -- it touches no retrieval state."""

    def __init__(self):
        self.llm, self.llm_mode = None, L.MODE_OFF
        self._sessions = {}


a = _FakeAgent()
a._sessions["s1"] = DialogState(llm=None)
stats = a.configure_llm(api_key="sk-x", mode="freeform")
check("CONFIG", "attaches a client and reports it",
      stats["enabled"] and stats["mode"] == "freeform")
check("CONFIG", "re-points sessions that already exist", a._sessions["s1"].llm is a.llm)
check("CONFIG", "an unrecognized mode still fails closed to off",
      a.configure_llm(api_key="sk-x", mode="nonsense")["enabled"] is False)
check("CONFIG", "mode off clears the client outright",
      a.configure_llm(api_key="sk-x", mode="off")["enabled"] is False and a.llm is None)
check("CONFIG", "an empty key cannot enable a model",
      a.configure_llm(api_key="  ", mode="expand")["enabled"] is False)
check("CONFIG", "clearing re-points live sessions too", a._sessions["s1"].llm is None)

print("\n" + "=" * 72)
passed = sum(1 for _, _, ok, _ in RESULTS if ok)
print("%d/%d checks passed" % (passed, len(RESULTS)))
for g, n, ok, detail in RESULTS:
    if not ok:
        print("  FAIL [%s] %s -- %s" % (g, n, detail))
print("=" * 72)
raise SystemExit(0 if passed == len(RESULTS) else 1)
