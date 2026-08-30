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
check("CLIENT", "posts to the SiliconFlow chat-completions endpoint",
      url == "https://api.siliconflow.cn/v1/chat/completions", url)
check("CLIENT", "sends Bearer auth from the key, never a literal in the repo",
      headers.get("Authorization") == "Bearer fake-key")
check("CLIENT", "defaults to the free model Qwen/Qwen3-8B",
      payload["model"] == "Qwen/Qwen3-8B", payload["model"])
check("CLIENT", "greedy decoding for reproducibility (temperature 0, top_p 1)",
      payload["temperature"] == 0.0 and payload["top_p"] == 1.0)
check("CLIENT", "disables Qwen3 thinking so we don't pay latency for a discarded trace",
      payload.get("enable_thinking") is False)
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

print("\n" + "=" * 72)
passed = sum(1 for _, _, ok, _ in RESULTS if ok)
print("%d/%d checks passed" % (passed, len(RESULTS)))
for g, n, ok, detail in RESULTS:
    if not ok:
        print("  FAIL [%s] %s -- %s" % (g, n, detail))
print("=" * 72)
raise SystemExit(0 if passed == len(RESULTS) else 1)
