"""Measure the feasibility disclosures: latency, token usage, and model cost.

Usage:
    py tools/feasibility_report.py
    py tools/feasibility_report.py --dataset data/public_set.jsonl

``docs/submission_rules.md`` requires "a disclosure of latency, token usage, and
estimated model cost". Latency deliberately does *not* ride along in ``respond()``:
``turn_response`` and ``usage`` both set ``"additionalProperties": false`` in
``docs/agent_api_contract.json``, so an extra key there would be malformed output and
scored as a miss. The agent records it internally instead; this script replays the
public set and reads it back out.

Output is markdown, ready to paste into the README or the final report.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Run as a script (`py tools/feasibility_report.py`), so the repo root is not on the path
# the way it is for `py -m evaluator.local_evaluator`. Add it before importing either.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evaluator.local_evaluator import catalog_index, evaluate, load_jsonl  # noqa: E402
from starter.agent import Agent  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Latency / token / cost disclosure")
    parser.add_argument("--catalog", default="data/catalog.jsonl")
    parser.add_argument("--dataset", default="data/public_set.jsonl")
    parser.add_argument("--output", help="optional path to write the numbers as JSON")
    args = parser.parse_args()

    samples = load_jsonl(args.dataset)
    catalog_ids, categories, products = catalog_index(args.catalog)

    agent = Agent(args.catalog)
    result = evaluate(agent, samples, catalog_ids, categories, products)
    latency = agent.latency_stats()
    usage = result["reported_token_usage"]

    print("### Measured latency\n")
    print(f"Public set: {result['sample_count']} sessions, {latency['turns']} `respond()` calls.\n")
    print("| Stage | Time |")
    print("|---|---|")
    print(
        "| `Agent()` construction (FTS5 index + LSA embeddings) | "
        f"**{latency['construction_seconds']:.1f} s**, one-time at startup |"
    )
    for label, key in (
        ("mean", "mean_ms"),
        ("median", "median_ms"),
        ("p95", "p95_ms"),
        ("max", "max_ms"),
    ):
        print(f"| `respond()` -- {label} | **{latency[key]:.1f} ms** |")

    print("\n### Token usage and cost\n")
    print("| Item | Value |")
    print("|---|---|")
    print("| LLM / external API | **None** -- no model call of any kind |")
    print("| Network access required | **None** -- runs fully offline |")
    print("| API keys / environment variables | **None** |")
    print("| Estimated model cost | **$0.00** |")
    print(
        f"| Reported token usage | `{usage['prompt_tokens']}` prompt, "
        f"`{usage['completion_tokens']}` completion -- honestly zero, not unreported |"
    )

    if usage["total_tokens"] != 0:
        # A nonzero count here means someone wired in a model without updating the
        # disclosure. Fail loudly rather than shipping a stale "$0.00" claim.
        raise SystemExit(
            f"\nERROR: reported token usage is {usage['total_tokens']}, expected 0. "
            "The no-model cost disclosure above is no longer accurate -- update it."
        )

    if args.output:
        payload = {
            "sample_count": result["sample_count"],
            "latency": latency,
            "reported_token_usage": usage,
            "technical_score": result["recommended_technical_score"],
        }
        Path(args.output).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(f"\nWrote {args.output}")


if __name__ == "__main__":
    main()
