"""Run the bundle the way a judge will: alone, in its own directory, from nothing else.

`tools/build_submission.py` already re-runs all 200 sessions against `submission/`, but it
does so from the **repo root** with `PYTHONPATH=submission;repo` (see its `verify_score`).
That proves the `starter/` shim re-exports the right class. It does not prove the bundle
stands alone -- the whole development tree is still on `sys.path`, so a module missing from
`MODULES`, an import that quietly resolves back against `starter/`, or a dependency absent
from `requirements.txt` would all pass and then fail on the organizer's machine, where a
raised exception is scored as a miss for every session.

This stages the layout `docs/submission_rules.md` actually describes --

    <staging>/
      agent.py            from agent import Agent          (the rules layout)
      src/                the one implementation
      starter/agent.py    from starter.agent import Agent  (evaluator/local_evaluator.py:12)
      evaluator/          a copy of the organizer's harness
      data/               catalog.jsonl + public_set.jsonl

-- with nothing from this repo on the import path, and requires a byte-identical result.

    py tools/verify_clean_room.py            the check
    py tools/verify_clean_room.py --keep     leave .cleanroom/ behind to poke at
    py tools/verify_clean_room.py --venv     ...into a fresh venv built from requirements.txt

Nothing here writes to `starter/`, `submission/`, `evaluator/` or `data/`.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# The build script owns these; importing them keeps one definition of "where things are"
# and of the `py`-vs-venv launcher rule. It is `main()`-gated, so this import is inert.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_submission import BUNDLE, REFERENCE, REPO, check, interpreter  # noqa: E402

# Inside the repo, not the system temp dir, and for one reason: `data/catalog.jsonl` is
# 58 MB, and `os.link` cannot cross volumes. On this machine the repo is on D: and TEMP is
# on C:, so staging under TEMP would silently fall back to copying 58 MB on every run.
STAGING = REPO / ".cleanroom"

DATA_FILES = ("catalog.jsonl", "public_set.jsonl")

# The line `starter/dense_retrieval.py` prints when the scientific stack is missing. The
# import is inside a broad try/except so a missing numpy degrades to sparse-only retrieval
# instead of crashing -- which scores 0.909858 rather than 0.912205 and looks like a normal
# run. A clean-room run is the one place that failure would actually show up, so we treat
# the line as fatal rather than letting the score comparison report it as a mystery.
DENSE_DISABLED = "[dense_retrieval] disabled"


def scrubbed_env() -> dict[str, str]:
    """The judge's environment: no PYTHONPATH, no model.

    `PYTHONPATH` is the whole point -- leaving the repo on it would recreate exactly the
    blind spot this tool exists to close. The model vars go for the same reason
    `build_submission.verify_score` drops them: with the endpoint live, "byte-identical"
    would be measuring its mood.
    """
    env = dict(os.environ)
    for name in ("PYTHONPATH", "SHOPPING_COPILOT_LLM", "SILICONFLOW_LLM"):
        env.pop(name, None)
    return env


def copy_tree(source: Path, dest: Path) -> int:
    """Copy `source` into `dest`, skipping `__pycache__`. Returns the file count.

    Stale bytecode from the dev tree has no business in a clean room, and `.pyc` files
    carry absolute source paths that would make `--keep` confusing to read.
    """
    count = 0
    for path in sorted(source.rglob("*")):
        if "__pycache__" in path.parts or not path.is_file():
            continue
        target = dest / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        count += 1
    return count


def link_or_copy(source: Path, target: Path) -> str:
    """Hardlink the catalog if the filesystem allows it, else copy. Returns which."""
    try:
        os.link(source, target)
        return "linked"
    except OSError:
        shutil.copyfile(source, target)
        return "copied"


def stage(root: Path) -> None:
    """Build the tester's directory from `submission/`, `evaluator/` and `data/`."""
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)

    bundle_files = copy_tree(BUNDLE, root)
    evaluator_files = copy_tree(REPO / "evaluator", root / "evaluator")

    (root / "data").mkdir()
    how = set()
    for name in DATA_FILES:
        how.add(link_or_copy(REPO / "data" / name, root / "data" / name))

    print(f"staged {root.relative_to(REPO)}/ -- {bundle_files} bundle files, "
          f"{evaluator_files} evaluator files, data {'+'.join(sorted(how))}")


# Run inside the staging directory, as the tester's own Python would see it. Written as a
# module-level string rather than a temp file so the staging tree contains only what a real
# submission contains.
PROBE = r"""
import sys, json
from pathlib import Path

root, repo = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
problems = []

# `python -m` and `python -c` both put the working directory at sys.path[0], so this is the
# organizer's import path exactly -- nothing added, nothing borrowed from the repo.
import agent as entry
if not hasattr(entry, "Agent"):
    problems.append("agent.py does not export Agent")
else:
    A = entry.Agent
    for name in ("reset", "respond"):
        if not callable(getattr(A, name, None)):
            problems.append(f"Agent.{name} missing")

    import starter.agent as shim
    if shim.Agent is not A:
        problems.append("starter shim resolves to a different class")

    # The leak check. If anything resolved against the development repo's `starter/`
    # instead of the bundle's `src/`, this is where it shows.
    for name, module in sorted(sys.modules.items()):
        origin = getattr(module, "__file__", None)
        if not origin or not (name == "src" or name.startswith("src.")):
            continue
        if root not in Path(origin).resolve().parents:
            problems.append(f"{name} resolved outside the clean room: {origin}")

# The staging directory lives *inside* the repo (see STAGING), so "under the repo" is not
# on its own a leak -- the clean room itself and the venv it may contain are both under it.
# Anything else that is, is the development tree bleeding in.
for entry_path in sys.path:
    resolved = Path(entry_path or ".").resolve()
    if resolved == root or root in resolved.parents:
        continue
    if resolved == repo or repo in resolved.parents:
        problems.append(f"repo directory on sys.path: {entry_path or '<cwd>'}")

print(json.dumps(problems))
"""


def probe(root: Path, python: str) -> bool:
    """Import the bundle inside the clean room and assert both entry points agree."""
    result = subprocess.run(
        [python, "-c", PROBE, str(root), str(REPO)],
        cwd=root, env=scrubbed_env(), capture_output=True, text=True,
    )
    if result.returncode != 0:
        return check("bundle imports standalone (agent.py and starter/agent.py)", False,
                     (result.stderr.strip() or "<no stderr>")[-300:])

    try:
        problems = json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return check("bundle imports standalone (agent.py and starter/agent.py)", False,
                     f"unreadable probe output: {result.stdout.strip()[-200:]}")

    return check(
        "bundle imports standalone; no repo path, no repo module",
        not problems, "; ".join(problems),
    )


def score(root: Path, python: str) -> bool:
    """Run all 200 sessions from inside the clean room; require byte-identical output.

    No `--catalog` / `--dataset`: the defaults are `data/catalog.jsonl` and
    `data/public_set.jsonl` relative to the working directory, which is exactly the
    relative-path story `docs/submission_setup.md` step 3 tells the organizer to set up.
    """
    if not REFERENCE.exists():
        return check("byte-identical to the score of record", False,
                     f"reference missing: {REFERENCE.relative_to(REPO)}")

    with tempfile.TemporaryDirectory() as tmp:
        output = Path(tmp) / "clean.json"
        print("  ... running 200 sessions inside the clean room (~25-40 s)")
        result = subprocess.run(
            [python, "-m", "evaluator.local_evaluator", "--output", str(output)],
            cwd=root, env=scrubbed_env(), capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(result.stdout[-1500:])
            print(result.stderr[-1500:], file=sys.stderr)
            return check("byte-identical to the score of record", False, "evaluator failed")
        produced = output.read_text(encoding="utf-8")

    ok = check(
        "scientific stack present (dense route live)",
        DENSE_DISABLED not in result.stderr,
        "requirements.txt did not cover the dense route -- this run is sparse-only",
    )

    expected = REFERENCE.read_text(encoding="utf-8")
    current = json.loads(produced)
    if produced == expected:
        print(f"         TechnicalScore {current['recommended_technical_score']} "
              f"| HitRate {current['hit_rate_at_10']} | MRR {current['mrr']} "
              f"| MTTC {current['mttc']}")
        return check("byte-identical to the score of record", True) and ok

    reference = json.loads(expected)
    moved = sum(1 for a, b in zip(current["sessions"], reference["sessions"]) if a != b)
    return check(
        "byte-identical to the score of record", False,
        f"{moved} sessions differ; score {current['recommended_technical_score']} "
        f"vs {reference['recommended_technical_score']}",
    ) and ok


def build_venv(root: Path) -> str:
    """Install `requirements.txt` into a fresh venv and return its interpreter.

    The default run is satisfied by whatever is already installed on the dev machine, which
    is precisely what a judge does not have. This is the arm that actually tests the
    dependency manifest, so it needs network for pip and takes a couple of minutes.
    """
    venv = root / ".venv"
    print(f"  ... creating {venv.relative_to(REPO)} and installing requirements.txt")
    subprocess.run([interpreter(), "-m", "venv", str(venv)], check=True,
                   cwd=root, env=scrubbed_env())

    # Windows puts it in Scripts/, POSIX in bin/.
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    result = subprocess.run(
        [str(python), "-m", "pip", "install", "--quiet", "-r", "requirements.txt"],
        cwd=root, env=scrubbed_env(), capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(result.stdout[-1500:])
        print(result.stderr[-1500:], file=sys.stderr)
        raise SystemExit("pip install failed -- see above; requirements.txt may be wrong")
    return str(python)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run submission/ in a clean room laid out like the organizer's machine.")
    parser.add_argument(
        "--keep", action="store_true",
        help="leave the staging directory in place for inspection")
    parser.add_argument(
        "--venv", action="store_true",
        help="install requirements.txt into a fresh venv first (slow; needs network)")
    parser.add_argument(
        "--dir", type=Path, default=STAGING,
        help=f"staging directory (default: {STAGING.relative_to(REPO)}/)")
    args = parser.parse_args()

    if not BUNDLE.exists():
        raise SystemExit("submission/ does not exist -- run `py tools/build_submission.py` first")

    root = args.dir.resolve()
    stage(root)
    try:
        python = build_venv(root) if args.venv else interpreter()
        print("\nverifying")
        ok = probe(root, python)
        ok = score(root, python) and ok
    finally:
        if args.keep:
            print(f"\nstaging directory kept at {root}")
        else:
            shutil.rmtree(root, ignore_errors=True)

    if ok:
        print("\nPASS: the bundle stands alone and reproduces the score of record.")
        return 0
    print("\nFAIL: do not submit this bundle until the checks above are green.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
