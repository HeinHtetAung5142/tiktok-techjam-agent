"""Print a before/after score table for a feature doc.

Usage:
    py tools/score_delta.py <before.json> <after.json>

Both arguments are evaluator outputs (``results*.json``). ``docs/baseline_results.json``
also works, even though the organizer wrote it with slightly different keys and no
scenario breakdown.

Output is markdown, ready to paste into ``docs/features/NN-<name>.md``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


# Windows consoles default to cp1252, which cannot encode the arrows and status
# marks below; without this the script dies on print rather than on any real error.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


# The organizer's baseline_results.json spells the composite "technical_score";
# the evaluator writes "recommended_technical_score". Accept either.
SCORE_KEYS = ("recommended_technical_score", "technical_score")

# label, json key, higher_is_better
AGGREGATE_METRICS = (
    ("HitRate@10", "hit_rate_at_10", True),
    ("MRR", "mrr", True),
    ("MTTC", "mttc", False),
    ("Efficiency", "efficiency", True),
)

SCENARIO_METRICS = (
    ("HitRate@10", "hit_rate_at_10", True),
    ("MRR", "mrr", True),
    ("MTTC", "mttc", False),
)

# One session out of 200 moves HitRate@10 by 0.005, so anything at that scale is
# a coin flip rather than a result worth claiming.
NOISE_FLOOR = 0.01


def technical_score(report: dict) -> float | None:
    for key in SCORE_KEYS:
        if key in report:
            return float(report[key])
    return None


def fmt(value: float | None) -> str:
    return "—" if value is None else f"{value:.6g}"


def fmt_delta(before: float | None, after: float | None, higher_is_better: bool) -> str:
    if before is None or after is None:
        return "—"
    delta = after - before
    if delta == 0:
        return "0"
    improved = delta > 0 if higher_is_better else delta < 0
    return f"{delta:+.6g} {'✅' if improved else '🔻'}"


def row(label: str, before: dict, after: dict, key: str, higher_is_better: bool) -> str:
    old, new = before.get(key), after.get(key)
    return f"| {label} | {fmt(old)} | {fmt(new)} | {fmt_delta(old, new, higher_is_better)} |"


def aggregate_table(before: dict, after: dict) -> list[str]:
    lines = [
        "| Metric | Before | After | Delta |",
        "|---|---|---|---|",
    ]
    lines += [row(*args) for args in ((l, before, after, k, h) for l, k, h in AGGREGATE_METRICS)]

    old_score, new_score = technical_score(before), technical_score(after)
    lines.append(
        f"| **TechnicalScore** | **{fmt(old_score)}** | **{fmt(new_score)}** | "
        f"**{fmt_delta(old_score, new_score, True)}** |"
    )
    return lines


def scenario_table(before: dict, after: dict) -> list[str]:
    old_scenarios = before.get("scenario_metrics") or {}
    new_scenarios = after.get("scenario_metrics") or {}
    if not new_scenarios:
        return []

    lines = ["", "### By scenario", ""]
    if not old_scenarios:
        lines.append("_No scenario breakdown in the 'before' file; showing after-values only._")
        lines.append("")

    lines += [
        "| Scenario | n | Metric | Before | After | Delta |",
        "|---|---|---|---|---|---|",
    ]
    for name in sorted(new_scenarios):
        new = new_scenarios[name]
        old = old_scenarios.get(name, {})
        count = new.get("sample_count", "?")
        for index, (label, key, higher_is_better) in enumerate(SCENARIO_METRICS):
            scenario_cell = name if index == 0 else ""
            count_cell = str(count) if index == 0 else ""
            lines.append(
                f"| {scenario_cell} | {count_cell} | {label} | {fmt(old.get(key))} | "
                f"{fmt(new.get(key))} | {fmt_delta(old.get(key), new.get(key), higher_is_better)} |"
            )
    return lines


def verdict(before: dict, after: dict) -> list[str]:
    old_score, new_score = technical_score(before), technical_score(after)
    if old_score is None or new_score is None:
        return []
    delta = new_score - old_score
    if abs(delta) < NOISE_FLOOR:
        note = (
            f"TechnicalScore moved {delta:+.6g}, which is within the ~{NOISE_FLOOR} noise floor of a "
            "200-session set. Treat as flat unless a single scenario moved clearly."
        )
    elif delta > 0:
        note = f"TechnicalScore improved by {delta:+.6g}."
    else:
        note = f"TechnicalScore regressed by {delta:+.6g}. Document why before moving on."
    return ["", f"**Verdict:** {note}"]


def load(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("before", help="baseline results json")
    parser.add_argument("after", help="new results json")
    args = parser.parse_args()

    before, after = load(args.before), load(args.after)

    lines = [f"_{Path(args.before).name} → {Path(args.after).name}_", ""]
    lines += aggregate_table(before, after)
    lines += scenario_table(before, after)
    lines += verdict(before, after)
    print("\n".join(lines))


if __name__ == "__main__":
    main()
