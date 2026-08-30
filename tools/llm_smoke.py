"""Check that a real API key works, without touching the evaluator.

This is the one test that cannot run in CI or on a machine without credentials, so it is
a separate command rather than part of `tools/verify_llm.py` (which stubs the transport
and needs no key at all).

    set SHOPPING_COPILOT_API_KEY=sk-...   # PowerShell: $env:SHOPPING_COPILOT_API_KEY="sk-..."
    set SHOPPING_COPILOT_LLM=freeform
    py tools/llm_smoke.py

A `.env` in the repo root works too and is read automatically; a real environment variable
wins over it. Any OpenAI-compatible endpoint can be tested by also setting
SHOPPING_COPILOT_BASE_URL and SHOPPING_COPILOT_MODEL -- see docs/LLM_SETUP.md.
The old SILICONFLOW_* names still work as aliases.

Exit code 0 means the key, the model id and the endpoint all work and the responses parse
into the shapes the agent expects. Anything else prints why. Nothing here writes to the
repo and no key is ever echoed back.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starter import env_file
from starter import llm as L


def main() -> int:
    # Load `.env` first, so a key saved from the WebUI's model panel is visible here.
    # Deliberately NOT done by `evaluator/` or `tools/feasibility_report.py`: those
    # describe the *judged* configuration, which reads real environment variables only --
    # which is why a full run stays byte-identical with a `.env` in the repo root.
    loaded = env_file.load_env_file()
    if loaded:
        print("loaded from .env: %s" % ", ".join(sorted(loaded)))

    mode = L.resolve_mode(os.environ.get(L.MODE_ENV))
    api_key = (os.environ.get(L.API_KEY_ENV) or "").strip()
    print(f"{L.MODE_ENV}={mode}")
    print(f"{L.API_KEY_ENV}={'set (%d chars)' % len(api_key) if api_key else 'NOT SET'}")
    if not api_key:
        print(f"\nNothing to test: set {L.API_KEY_ENV} to a SiliconFlow key.")
        return 2
    if mode == L.MODE_OFF:
        print(f"\nNothing to test: set {L.MODE_ENV} to 'freeform' or 'expand'.")
        return 2

    client, resolved = L.client_from_env()
    if client is None:
        print("\nclient_from_env() returned None despite a key and mode -- that is a bug.")
        return 1
    print(f"model={client.model}\nbase_url={client.base_url}\nmode={resolved}\n")

    raw = client.complete("Reply with exactly: ok", "ok")
    print(f"1. reachability      -> {raw!r}")
    if raw is None:
        # The provider's own words, not a guess. A 429 and a 401 need opposite responses.
        print(f"\nFAILED: {client.last_error or 'no usable response'}\n")
        error = (client.last_error or "").lower()
        if "429" in error or "rate limit" in error:
            print(
                "That is a quota/rate limit, NOT a bad key. On OpenRouter's free tier the\n"
                "cap is 50 requests per DAY across all models -- one benchmark run over\n"
                "five models spends about half of it. Wait for the reset, or add credits."
            )
        elif "401" in error or "403" in error or "user not found" in error:
            print(
                "The key was rejected. Check it was pasted whole and belongs to this\n"
                "endpoint; for SiliconFlow, real-name verification must also be complete."
            )
        elif "404" in error:
            print(
                "The model id was not found. Free slugs come and go -- pick another from\n"
                "the provider's model list and set SHOPPING_COPILOT_MODEL."
            )
        else:
            print("Check the endpoint printed above is reachable from this machine.")
        print("\nThe agent treats every one of these as 'no model' and still scores 0.912205.")
        return 1

    parsed = L.parse_freeform(client, "I want a burgundy linen shirt for a wedding, under $60")
    print(f"2. parse_freeform    -> {parsed}")

    expanded = L.expand_query(client, "waterproof hiking boots with ankle support")
    print(f"3. expand_query      -> {expanded}")

    print(f"\nstats: {client.stats()}")
    problems = []
    if not isinstance(parsed, dict):
        problems.append("parse_freeform did not return a dict")
    elif parsed.get("price_max") != 60.0:
        problems.append(f"expected price_max 60.0, got {parsed.get('price_max')!r}")
    if not expanded:
        problems.append("expand_query returned no usable terms")

    if problems:
        print("\nReachable, but the model's output was weaker than expected:")
        for problem in problems:
            print(f"  - {problem}")
        print("Every one of these degrades to the offline pipeline, so nothing breaks.")
        return 1
    print("\nOK -- key, model and endpoint all work, and responses parse as expected.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
