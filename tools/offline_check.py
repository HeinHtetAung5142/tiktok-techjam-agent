"""Replay the full public set with network access hard-blocked, and prove nothing moved.

Usage:
    py tools/offline_check.py
    py tools/offline_check.py --reference results_after_fieldfactors.json
    py tools/offline_check.py --output results_offline.json

``docs/submission_rules.md`` requires the agent to work with network access disabled, and
the Tier 3 feature list calls for confirming it "degrades gracefully, or works fully,
without a live external API call". This is that confirmation, run as an experiment rather
than asserted in prose:

1. install ``tools/offline_guard`` -- every socket operation in this process now raises,
   *before* numpy/scipy/scikit-learn are imported, so even the dense stack is loaded
   under the block;
2. build the agent and replay all 200 sessions through the organizer's own evaluator;
3. compare the result against a reference run, session by session, not just in aggregate.

Exit status is 0 only when the scores match to six decimal places, every session's hit
turn and rank is identical, the agent did not degrade, and no turn was answered from the
fallback path. Anything else prints what differed and exits 1.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

# Run as a script (`py tools/offline_check.py`), so the repo root is not on the path the
# way it is for `py -m evaluator.local_evaluator`. Add it before importing anything else.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import offline_guard  # noqa: E402

# Installed before the agent is imported, so module-level imports are covered too. Every
# later import in this process -- numpy, scipy, scikit-learn -- happens under the block.
offline_guard.install()

from evaluator.local_evaluator import catalog_index, evaluate, load_jsonl  # noqa: E402
from starter.agent import Agent  # noqa: E402


# Six decimal places is what the evaluator itself rounds to, so an exact string-equal
# comparison at that precision is the strictest check the data supports.
COMPARED_METRICS = ("hit_rate_at_10", "mrr", "mttc", "efficiency", "recommended_technical_score")

# How often the session counter reprints when stdout is not a terminal. On a terminal it
# rewrites one line instead; piped into a file or `tail`, carriage returns would collapse
# the whole run onto one unreadable line.
REPORT_EVERY = 20


class Stage:
    """Prints each startup stage with how long it took.

    The run is otherwise silent for well over a minute before the first result appears,
    which is indistinguishable from a hang.
    """

    def __init__(self) -> None:
        self._last = time.perf_counter()

    def done(self, label: str) -> None:
        now = time.perf_counter()
        # flush, because stdout is block-buffered when piped -- exactly when progress
        # output matters most.
        print(f"  {label:<46}{now - self._last:6.1f}s", flush=True)
        self._last = now


class Progress:
    """A session counter with an ETA, driven by the evaluator's own per-session calls."""

    def __init__(self, total: int) -> None:
        self.total = total
        self.done = 0
        self._started: float | None = None
        self._terminal = sys.stdout.isatty()

    def tick(self) -> None:
        """One session is starting. The first tick only starts the clock."""
        if self._started is None:
            self._started = time.perf_counter()
            return
        # Session n starting means n-1 finished, so this counts completions, never
        # promising 200/200 while the last session is still running.
        self.done += 1
        self._render()

    def finish(self) -> None:
        self.done = self.total
        self._render(force=True)
        if self._terminal:
            print(flush=True)

    def _render(self, force: bool = False) -> None:
        if self._started is None or not self.total:
            return
        elapsed = time.perf_counter() - self._started
        rate = self.done / elapsed if elapsed > 0 and self.done else 0.0
        remaining = (self.total - self.done) / rate if rate else 0.0
        line = (
            f"  sessions {self.done:>4}/{self.total}"
            f"  {self.done * 100 // self.total:>3}%"
            f"  {elapsed:6.1f}s elapsed, ~{remaining:4.0f}s left"
        )
        if self._terminal:
            print(f"\r{line}", end="", flush=True)
        elif force or self.done % REPORT_EVERY == 0:
            print(line, flush=True)


class ProgressAgent(Agent):
    """The real agent, plus a session counter. It must stay behaviourally identical.

    `reset()` is the only per-session hook available: the evaluator calls it once before
    each session's first turn (`evaluator/local_evaluator.py:228`), and `evaluator/` is
    not ours to modify. Nothing here may do anything but count -- this is the object whose
    score the whole tool exists to verify.
    """

    def __init__(self, catalog_path: str, progress: Progress) -> None:
        super().__init__(catalog_path)
        self._progress = progress

    def reset(self, session_id: str, user_profile: dict) -> None:
        self._progress.tick()
        super().reset(session_id, user_profile)


def compare_sessions(reference: list[dict], observed: list[dict]) -> list[str]:
    """Per-session differences. Aggregate metrics can match while sessions swap places."""
    differences: list[str] = []
    by_id = {session["sample_id"]: session for session in reference}
    for session in observed:
        previous = by_id.get(session["sample_id"])
        if previous is None:
            differences.append(f"{session['sample_id']}: not present in reference")
            continue
        for field in ("hit", "first_hit_turn", "best_rank"):
            if previous.get(field) != session.get(field):
                differences.append(
                    f"{session['sample_id']}: {field} {previous.get(field)!r} -> {session.get(field)!r}"
                )
    return differences


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline-safety and score-parity check")
    parser.add_argument("--catalog", default="data/catalog.jsonl")
    parser.add_argument("--dataset", default="data/public_set.jsonl")
    parser.add_argument("--reference", default="results_after_fieldfactors.json")
    parser.add_argument("--output", help="optional path to write the offline run's results")
    args = parser.parse_args()

    print(f"network access: BLOCKED ({len(offline_guard.BLOCKED_EVENTS)} audit events + socket)")

    stage = Stage()
    samples = load_jsonl(args.dataset)
    stage.done(f"dataset loaded ({len(samples)} sessions)")

    catalog_ids, categories, products = catalog_index(args.catalog)
    stage.done(f"catalog loaded ({len(catalog_ids)} products)")

    progress = Progress(len(samples))
    agent = ProgressAgent(args.catalog, progress)
    stage.done("index built (FTS5 + LSA embeddings)")

    result = evaluate(agent, samples, catalog_ids, categories, products)
    progress.finish()
    stage.done(f"{len(result['sessions'])} sessions replayed")
    latency = agent.latency_stats()

    if args.output:
        Path(args.output).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")

    reference = json.loads(Path(args.reference).read_text(encoding="utf-8"))
    failures: list[str] = []

    print("\n| Metric | Reference | Offline run | |")
    print("|---|---|---|---|")
    for metric in COMPARED_METRICS:
        expected, actual = reference.get(metric), result.get(metric)
        same = expected == actual
        if not same:
            failures.append(f"{metric}: {expected!r} -> {actual!r}")
        print(f"| {metric} | {expected} | {actual} | {'ok' if same else 'CHANGED'} |")

    session_differences = compare_sessions(reference.get("sessions", []), result["sessions"])
    failures.extend(session_differences)

    # A run can score identically while quietly answering from the fallback slate, which
    # would mean retrieval broke and the safety net hid it. Both must be clean.
    if latency.get("degraded_reason") is not None:
        failures.append(f"agent constructed in degraded mode: {latency['degraded_reason']}")
    if latency.get("fallback_turns"):
        failures.append(f"{latency['fallback_turns']} turn(s) answered from the fallback path")

    print(f"\nsessions compared: {len(result['sessions'])}, differing: {len(session_differences)}")
    print(f"degraded: {latency.get('degraded_reason') is not None}, "
          f"fallback turns: {latency.get('fallback_turns')}")
    print(f"construction: {latency['construction_seconds']:.1f}s, "
          f"mean turn: {latency.get('mean_ms', 0.0):.1f}ms")

    if failures:
        print(f"\nFAIL -- {len(failures)} difference(s):")
        for failure in failures[:20]:
            print(f"  - {failure}")
        if len(failures) > 20:
            print(f"  ... and {len(failures) - 20} more")
        return 1

    print("\nPASS -- ran the full public set with no network access, "
          "no degradation, no fallback turns, and an identical score.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
