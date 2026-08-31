"""Refuse to accept a change that lowers the score.

The rule this enforces: **TechnicalScore may rise or stay level, never fall.** Run it
after any change to `starter/`. Exit code 0 means the change is acceptable; non-zero
means revert it.

    py tools/score_ratchet.py
    py tools/score_ratchet.py --update      # after a genuine, reviewed improvement

It reports two different things, and the distinction matters:

- **Byte-identical** -- the sessions array matches the reference exactly. This is the
  strong result, and the only honest way to claim "no effect on scoring". It is what a
  free-form-only feature must produce, because those code paths are unreachable while
  scoring (see the 0-free-form-calls invariant in `tools/verify_features.py`).
- **Score-equal but not byte-identical** -- the aggregate matches while individual
  sessions moved. That is *not* a no-op: offsetting movements can hide a real regression
  that the private set, four times larger, would not forgive. Treated as a warning.

The reference is `results_after_fieldfactors.json`, the committed score of record.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
REFERENCE = REPO / "results_after_fieldfactors.json"

# The score of record. Raise it only through --update, and only with a feature doc
# explaining the movement.
BASELINE = 0.912205

# One session out of 200 moves HitRate@10 by 0.005, so anything under ~0.01 is noise
# rather than improvement. Used for reporting, never to excuse a decrease.
NOISE_FLOOR = 0.01


def run_evaluator(output: Path) -> dict:
    result = subprocess.run(
        [sys.executable if os.environ.get("VIRTUAL_ENV") else "py",
         "-m", "evaluator.local_evaluator", "--output", str(output)],
        cwd=REPO, capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(result.stdout[-2000:])
        print(result.stderr[-2000:], file=sys.stderr)
        raise SystemExit("evaluator failed to run -- fix that before trusting any number.")
    return json.loads(output.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--update", action="store_true",
        help="overwrite the reference snapshot after a reviewed improvement",
    )
    parser.add_argument("--output", default=None, help="keep the run's JSON at this path")
    args = parser.parse_args()

    if not REFERENCE.exists():
        raise SystemExit(f"reference snapshot missing: {REFERENCE}")
    reference = json.loads(REFERENCE.read_text(encoding="utf-8"))

    with tempfile.TemporaryDirectory() as tmp:
        output = Path(args.output) if args.output else Path(tmp) / "ratchet.json"
        current = run_evaluator(output)
        payload = json.dumps(current, indent=2) + "\n"

    score = float(current["recommended_technical_score"])
    delta = score - BASELINE
    identical = current["sessions"] == reference["sessions"]

    print("=" * 68)
    print(f"  TechnicalScore   {score:.6f}   (baseline {BASELINE:.6f}, delta {delta:+.6f})")
    print(f"  HitRate@10       {current['hit_rate_at_10']}")
    print(f"  MRR              {current['mrr']}")
    print(f"  MTTC             {current['mttc']}")
    print(f"  sessions         {'BYTE-IDENTICAL to reference' if identical else 'CHANGED'}")
    print("=" * 68)

    if not identical:
        moved = sum(
            1 for a, b in zip(current["sessions"], reference["sessions"]) if a != b
        )
        print(f"  {moved} of {len(reference['sessions'])} sessions differ from the reference.")
        for scenario, metrics in sorted(current["scenario_metrics"].items()):
            was = reference["scenario_metrics"].get(scenario, {})
            for key in ("hit_rate_at_10", "mrr", "mttc"):
                if metrics.get(key) != was.get(key):
                    print(f"    {scenario:<16} {key:<14} {was.get(key)} -> {metrics.get(key)}")

    if delta < -1e-9:
        print("\nFAIL: the score went DOWN. Revert this change.")
        print("  A decrease is never acceptable, regardless of size -- the public set is")
        print("  200 sessions and the private set is 800, so a loss here is a bigger loss")
        print("  there. If the feature is worth keeping, gate it behind a path the scored")
        print("  run cannot reach (see starter/facets.py for the pattern).")
        return 1

    if args.update:
        if delta <= 0:
            print("\n--update refused: nothing improved, so there is nothing to record.")
            return 1
        REFERENCE.write_text(payload, encoding="utf-8")
        print(f"\nReference updated. Now set BASELINE = {score:.6f} in this file and in")
        print("tools/sweep_constants.py, and write the feature doc.")
        return 0

    if identical:
        print("\nPASS: byte-identical. This change provably did not touch scoring.")
    elif abs(delta) < 1e-9:
        print("\nPASS (with a caveat): the aggregate is unchanged but individual sessions")
        print("  moved. Offsetting movements can hide a regression the private set would")
        print("  expose. Read the per-scenario lines above before calling this a no-op.")
    elif delta < NOISE_FLOOR:
        print(f"\nPASS: +{delta:.6f}, but that is inside the ~{NOISE_FLOOR} noise floor of a")
        print("  200-session set. Report it as flat, not as a win.")
    else:
        print(f"\nPASS: genuine improvement, +{delta:.6f}. Run with --update to record it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
