"""Benchmark several models against the two jobs this agent actually gives a model.

Why a dedicated tool
--------------------
`tools/llm_smoke.py` answers "does my key work". This answers the different question
"which model should we use, and is any of them worth enabling" -- which needs the same
probes run against every candidate, and needs the *retrieval* consequence measured, not
just the latency.

Two layers, because they fail differently:

1. **Probes.** Fixed prompts through `parse_freeform` and `expand_query`, the only two
   entry points the agent has. Scored on what we actually depend on: did valid JSON come
   back, were the extracted slots the right ones, were the expansion terms usable. A model
   that is fast and wrong is worse than no model, and a latency table alone hides that.
2. **Score (`--sessions N`).** Replays real public-set sessions in `expand` mode -- the
   only mode that can move the score -- and reports TechnicalScore against the offline
   control arm. This is the number that decides anything; the probes only explain it.

    py tools/benchmark_llms.py --offline                      # no key: stubs, for CI
    py tools/benchmark_llms.py --models google/gemma-4-26b-a4b-it:free,z-ai/glm-5.2:free
    py tools/benchmark_llms.py --models google/gemma-4-26b-a4b-it:free --sessions 50

**A model arm is not reproducible.** Greedy decoding is the closest this endpoint offers,
so re-running can move the score without anything having changed -- unlike every other
measurement in this repo. Treat a difference smaller than the ~0.01 noise floor as noise,
and never quote a model arm as a score of record.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from starter import env_file  # noqa: E402
from starter import llm as L  # noqa: E402


# Probe cases. `want_slots` are the values the agent depends on being extracted; a model
# that misses them is useless for `freeform` no matter how fast it is. `want_terms` is a
# floor, not an exact match -- expansion is allowed to be creative, but a term the shopper
# plainly implied should be in there somewhere.
FREEFORM_PROBES = [
    {
        "message": "I need a burgundy wool coat, nothing over about $120",
        "want_slots": {"color": "burgundy", "material": "wool"},
        "want_price": 120.0,
    },
    {
        "message": "something in a deep wine shade, cotton if possible",
        "want_slots": {"material": "cotton"},
        "want_price": None,
    },
    {
        "message": "just browsing for running shoes",
        "want_slots": {},
        "want_price": None,
    },
]

EXPAND_PROBES = [
    {
        "evidence": "I need a jacket. 100% Wool. Button closure.",
        "want_terms": ("wool", "jacket", "button"),
    },
    {
        "evidence": "Looking for hiking boots for wet weather",
        "want_terms": ("waterproof", "hiking", "boot"),
    },
]


def offline_transport(url: str, headers: dict, body: bytes, timeout: float):
    """A deterministic stand-in, so this tool is runnable and testable with no key.

    It answers the way a competent model would, which makes the harness exercisable end to
    end. It measures nothing about any real model -- `--offline` output is labelled as
    such for exactly that reason.
    """
    payload = json.loads(body.decode("utf-8"))
    prompt = payload["messages"][1]["content"].lower()
    system = payload["messages"][0]["content"]

    if system.startswith("You extract"):
        document = {"color": None, "material": None, "price_max": None, "keywords": []}
        for color in ("burgundy", "wine", "red", "navy"):
            if color in prompt:
                document["color"] = "burgundy" if color == "wine" else color
                break
        for material in ("wool", "cotton", "leather", "denim"):
            if material in prompt:
                document["material"] = material
                break
        if "120" in prompt:
            document["price_max"] = 120
        document["keywords"] = ["wool coat"] if "coat" in prompt else []
        content = json.dumps(document)
    else:
        seed = [
            term
            for term in ("wool", "jacket", "button", "waterproof", "hiking", "boot")
            if term in prompt or (term == "waterproof" and "wet weather" in prompt)
        ]
        content = json.dumps(seed or ["clothing"])

    return 200, json.dumps(
        {
            "choices": [{"message": {"content": content}}],
            "usage": {
                "prompt_tokens": len(prompt.split()),
                "completion_tokens": len(content.split()),
            },
        }
    ).encode("utf-8")


def probe_model(client: L.SiliconFlowClient) -> dict:
    """Run every probe once. Returns correctness and latency for this model."""
    slot_hits = slot_total = 0
    price_hits = price_total = 0
    term_hits = term_total = 0
    parse_ok = parse_total = 0

    for case in FREEFORM_PROBES:
        parse_total += 1
        parsed = L.parse_freeform(client, case["message"])
        if parsed is None:
            continue
        parse_ok += 1
        for slot, expected in case["want_slots"].items():
            slot_total += 1
            if parsed.get(slot) == expected:
                slot_hits += 1
        if case["want_price"] is not None:
            price_total += 1
            if parsed.get("price_max") == case["want_price"]:
                price_hits += 1

    for case in EXPAND_PROBES:
        parse_total += 1
        terms = L.expand_query(client, case["evidence"])
        if terms:
            parse_ok += 1
        joined = " ".join(terms)
        for wanted in case["want_terms"]:
            term_total += 1
            if wanted in joined:
                term_hits += 1

    def ratio(hits: int, total: int) -> float:
        return hits / total if total else 0.0

    latencies = [ms for ms in client.latencies_ms if ms > 0]
    return {
        "parse_rate": ratio(parse_ok, parse_total),
        "slot_accuracy": ratio(slot_hits, slot_total),
        "price_accuracy": ratio(price_hits, price_total),
        "term_recall": ratio(term_hits, term_total),
        "mean_ms": round(statistics.mean(latencies), 1) if latencies else 0.0,
        "p95_ms": round(sorted(latencies)[int(len(latencies) * 0.95) - 1], 1) if latencies else 0.0,
        "prompt_tokens": client.prompt_tokens,
        "completion_tokens": client.completion_tokens,
        "failures": client.failures,
        "disabled": client.disabled,
        "disabled_reason": client.disabled_reason,
    }


def score_model(agent, samples, catalog_ids, categories, products, client, mode: str) -> dict:
    """Replay `samples` with this client attached. Restores the agent's own config after."""
    from evaluator.local_evaluator import evaluate

    before_llm, before_mode = agent.llm, agent.llm_mode
    agent.llm, agent.llm_mode = client, mode
    try:
        started = time.perf_counter()
        result = evaluate(agent, samples, catalog_ids, categories, products)
        result["wall_seconds"] = round(time.perf_counter() - started, 1)
        return result
    finally:
        agent.llm, agent.llm_mode = before_llm, before_mode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--models",
        default=L.DEFAULT_MODEL,
        help="comma-separated model ids (default: the shipped default)",
    )
    parser.add_argument("--base-url", default=None, help="override the endpoint for every model")
    parser.add_argument(
        "--offline",
        action="store_true",
        help="use a deterministic stub instead of the network -- exercises the harness, "
        "measures no model",
    )
    parser.add_argument(
        "--sessions",
        type=int,
        default=0,
        help="also replay this many public-set sessions per model in `expand` mode (slow)",
    )
    parser.add_argument("--mode", default=L.MODE_EXPAND, choices=[L.MODE_FREEFORM, L.MODE_EXPAND])
    parser.add_argument("--dataset", default="data/public_set.jsonl")
    parser.add_argument("--catalog", default="data/catalog.jsonl")
    parser.add_argument("--json", default=None, help="also write the full results here")
    args = parser.parse_args(argv)

    env_file.load_env_file()
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    if not models:
        raise SystemExit("no models given")

    api_key = (os.environ.get(L.API_KEY_ENV) or "").strip()
    if args.offline:
        api_key = api_key or "offline-stub"
        transport = offline_transport
    else:
        transport = None
        if not api_key:
            raise SystemExit(
                "%s is not set. Export a key, put one in .env, or pass --offline to "
                "exercise the harness with a stub." % L.API_KEY_ENV
            )

    base_url = args.base_url or os.environ.get(L.BASE_URL_ENV) or L.DEFAULT_BASE_URL

    agent = samples = catalog_ids = categories = products = None
    if args.sessions:
        from evaluator.local_evaluator import catalog_index, load_jsonl
        from starter.agent import Agent

        print("Building the agent index (~15s)...", flush=True)
        samples = load_jsonl(args.dataset)[: args.sessions]
        catalog_ids, categories, products = catalog_index(args.catalog)
        agent = Agent(args.catalog)

    report = {"offline_stub": args.offline, "mode": args.mode, "models": {}}

    for model in models:
        print("\n=== %s ===" % model, flush=True)
        client = L.SiliconFlowClient(
            api_key=api_key, model=model, base_url=base_url, transport=transport
        )
        entry = {"probes": probe_model(client)}
        probes = entry["probes"]
        print(
            "  probes: parse %.0f%%  slots %.0f%%  price %.0f%%  terms %.0f%%  "
            "%.0f ms mean / %.0f ms p95  %d tok"
            % (
                probes["parse_rate"] * 100,
                probes["slot_accuracy"] * 100,
                probes["price_accuracy"] * 100,
                probes["term_recall"] * 100,
                probes["mean_ms"],
                probes["p95_ms"],
                probes["prompt_tokens"] + probes["completion_tokens"],
            ),
            flush=True,
        )
        if probes["disabled"]:
            print("  breaker tripped: %s" % probes["disabled_reason"], flush=True)

        if args.sessions:
            # A fresh client, so probe traffic does not pollute the session token counts.
            run_client = L.SiliconFlowClient(
                api_key=api_key, model=model, base_url=base_url, transport=transport
            )
            result = score_model(
                agent, samples, catalog_ids, categories, products, run_client, args.mode
            )
            entry["score"] = {
                "technical_score": result["recommended_technical_score"],
                "hit_rate_at_10": result["hit_rate_at_10"],
                "mrr": result["mrr"],
                "mttc": result["mttc"],
                "wall_seconds": result["wall_seconds"],
                "model_calls": run_client.calls,
                "model_failures": run_client.failures,
                "tokens": run_client.prompt_tokens + run_client.completion_tokens,
            }
            s = entry["score"]
            print(
                "  %d sessions: score %.6f  HR %.4f  MRR %.6f  MTTC %.3f  "
                "(%d calls, %d failed, %d tok, %.0fs)"
                % (
                    len(samples),
                    s["technical_score"],
                    s["hit_rate_at_10"],
                    s["mrr"],
                    s["mttc"],
                    s["model_calls"],
                    s["model_failures"],
                    s["tokens"],
                    s["wall_seconds"],
                ),
                flush=True,
            )
        report["models"][model] = entry

    if args.sessions:
        # The arm that matters: the same sessions with no model at all. Without it a model
        # arm is a number with nothing to beat.
        control = score_model(agent, samples, catalog_ids, categories, products, None, L.MODE_OFF)
        report["control"] = {
            "technical_score": control["recommended_technical_score"],
            "hit_rate_at_10": control["hit_rate_at_10"],
            "mrr": control["mrr"],
            "mttc": control["mttc"],
        }
        print(
            "\ncontrol (offline, no model): score %.6f  HR %.4f  MRR %.6f  MTTC %.3f"
            % (
                control["recommended_technical_score"],
                control["hit_rate_at_10"],
                control["mrr"],
                control["mttc"],
            )
        )
        print("\nDeltas vs control (one session = +/-0.005 HitRate; under ~0.01 is noise):")
        for model, entry in report["models"].items():
            if "score" in entry:
                delta = entry["score"]["technical_score"] - report["control"]["technical_score"]
                print("  %-40s %+.6f" % (model, delta))

    if args.offline:
        print("\n[--offline] These numbers come from a stub, not a model. Harness check only.")

    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print("\nWrote %s" % args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
