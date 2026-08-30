"""Re-sweep the agent's tuned constants against the current pipeline.

Why this exists: every constant in the agent was fitted against an *earlier* version
of the retrieval stack. The rerank weights and pool were tuned in feature 04 and the
disclosure schedule in feature 05 -- both before phrase routes (26d4b21) and the dense
route (1d41dee) landed. Feature 06's nineteen variants covered phrase parameters only.
Whenever the pipeline changes the old argmax is no longer known to be the argmax, and
this re-runs the axes by coordinate descent.

One `Agent` is built and reused across every variant, so the ~13.5 s index construction
is paid once instead of per variant. That is the whole reason this is a separate tool
rather than repeated `local_evaluator` runs: a 30-variant sweep takes minutes, not an
hour.

    py tools/sweep_constants.py                 # every axis
    py tools/sweep_constants.py --axis A B      # just these
    py tools/sweep_constants.py --list          # show axes without running

Constants are module globals read at call time, so patching them between runs is
sufficient -- with one exception: FIELD_FACTORS is baked into a per-product cache
(`CatalogIndex._profile_cache`) that must be cleared, or a field-factor variant
silently measures the previous variant's numbers.

Read-only with respect to the repo: nothing is written, and every constant is restored
after each variant.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Run as a script, so the repo root is not on the path the way it is for `-m`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evaluator.local_evaluator import catalog_index, evaluate, load_jsonl  # noqa: E402
from starter import agent as A  # noqa: E402
from starter import llm as L  # noqa: E402
from starter import ranking as R  # noqa: E402
from starter import retrieval as T  # noqa: E402
from starter.agent import Agent  # noqa: E402

# Score of record. The control arm must reproduce this exactly; if it does not, the
# harness is wrong and no variant below can be trusted. Bump this whenever a feature
# changes the shipped score, or every delta printed below is measured against a stale
# reference.
BASELINE = 0.912205

# One session is +/-0.005 HitRate, so anything under this is noise, not a result.
NOISE_FLOOR = 0.01

RANKING_KEYS = ("COVERAGE_WEIGHT", "PHRASE_WEIGHT", "PARTIAL_PHRASE_CREDIT")


def _set(module, **values):
    return lambda: [setattr(module, key, value) for key, value in values.items()]


def _fields(**values):
    def apply():
        T.FIELD_FACTORS.update(values)
    return apply


# Axis -> (name, list of (label, apply)). Add axes here as the pipeline grows.
AXES = {
    "A": ("coverage / phrase split", [
        ("coverage=%s phrase=%s" % (c, p), _set(R, COVERAGE_WEIGHT=c, PHRASE_WEIGHT=p))
        for c, p in [(0.3, 0.7), (0.4, 0.6), (0.6, 0.4), (0.7, 0.3)]
    ]),
    "B": ("rerank pool", [
        ("RERANK_POOL=%s" % n, _set(T, RERANK_POOL=n)) for n in [60, 90, 160, 200, 300]
    ]),
    "C": ("disclosure schedule", [
        ("schedule=%s" % (s,), _set(A, DISCLOSURE_SCHEDULE=s)) for s in [
            (1, 1, 3, 6, 10), (1, 1, 2, 5, 10), (1, 1, 1, 4, 8, 10),
            (1, 1, 5, 10), (1, 1, 6, 10), (1, 2, 4, 8, 10),
        ]
    ]),
    "D": ("route weights", [
        ("category=0.15", _set(T, CATEGORY_ROUTE_WEIGHT=0.15)),
        ("category=0.45", _set(T, CATEGORY_ROUTE_WEIGHT=0.45)),
        ("category=0.6", _set(T, CATEGORY_ROUTE_WEIGHT=0.6)),
        ("dense=0.15", _set(T, DENSE_ROUTE_WEIGHT=0.15)),
        ("dense=0.5", _set(T, DENSE_ROUTE_WEIGHT=0.5)),
        ("dense=0.0 (route off)", _set(T, DENSE_ROUTE_WEIGHT=0.0)),
        ("phrase_route=0.35", _set(T, PHRASE_ROUTE_WEIGHT=0.35)),
        ("phrase_route=0.75", _set(T, PHRASE_ROUTE_WEIGHT=0.75)),
        ("phrase_route=1.0", _set(T, PHRASE_ROUTE_WEIGHT=1.0)),
    ]),
    "E": ("phrase route reach", [
        ("MAX_PHRASE_ROUTES=6", _set(T, MAX_PHRASE_ROUTES=6)),
        ("MAX_PHRASE_ROUTES=20", _set(T, MAX_PHRASE_ROUTES=20)),
        ("PHRASE_DF_MAX=1000", _set(T, PHRASE_DF_MAX=1000)),
        ("PHRASE_DF_MAX=4000", _set(T, PHRASE_DF_MAX=4000)),
        ("PHRASE_DF_MAX=8000", _set(T, PHRASE_DF_MAX=8000)),
    ]),
    "F": ("field factors", [
        ("lighter tail", _fields(description=0.5, store=0.6)),
        ("flat 1.0 everywhere", _fields(title=1.0, categories=1.0, features=1.0,
                                        details=1.0, store=1.0, description=1.0)),
        ("steeper tail", _fields(description=0.4, store=0.5, features=0.8, details=0.8)),
        ("features-heavy", _fields(features=1.0, details=1.0)),
    ]),
    "G": ("partial phrase credit", [
        ("PARTIAL_PHRASE_CREDIT=%s" % v, _set(R, PARTIAL_PHRASE_CREDIT=v))
        for v in [0.25, 0.75, 1.0]
    ]),
    # Axis F found raising features+details from 0.85 to 1.0 worth +0.0054 on every
    # metric at once. This decomposes that: which field carries it, is the surface flat
    # near the top, and does pushing past parity with `title` start to hurt.
    "H": ("field factors, features/details detail", [
        ("features=1.0 only", _fields(features=1.0)),
        ("details=1.0 only", _fields(details=1.0)),
        ("both=0.9", _fields(features=0.9, details=0.9)),
        ("both=0.95", _fields(features=0.95, details=0.95)),
        ("both=1.0 (axis F winner)", _fields(features=1.0, details=1.0)),
        ("both=1.0 + categories=1.0", _fields(features=1.0, details=1.0, categories=1.0)),
        ("both=1.15 (past title)", _fields(features=1.15, details=1.15)),
    ]),
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Re-sweep tuned constants")
    parser.add_argument("--axis", nargs="*", help="axis letters to run (default: all)")
    parser.add_argument("--catalog", default="data/catalog.jsonl")
    parser.add_argument("--dataset", default="data/public_set.jsonl")
    parser.add_argument("--list", action="store_true", help="list axes and exit")
    args = parser.parse_args()

    if args.list:
        for key, (name, variants) in AXES.items():
            print("%s  %s  (%d variants)" % (key, name, len(variants)))
        return

    selected = [k.upper() for k in (args.axis or AXES)]
    unknown = [k for k in selected if k not in AXES]
    if unknown:
        raise SystemExit("unknown axis %s; valid: %s" % (unknown, sorted(AXES)))

    # `Agent()` reads the environment for an optional model (feature 13). In `expand` mode
    # the model joins retrieval and identical input is no longer guaranteed to produce
    # identical output -- which silently invalidates every delta below, and would surface
    # only as a confusing control-arm mismatch. Refuse the run instead.
    inherited_mode = L.resolve_mode(os.environ.get(L.MODE_ENV))
    if inherited_mode != L.MODE_OFF:
        raise SystemExit(
            "%s=%s is set in this shell. Sweeping requires the deterministic offline "
            "agent; unset it (or set it to 'off') and re-run." % (L.MODE_ENV, inherited_mode)
        )

    samples = load_jsonl(args.dataset)
    catalog_ids, categories, products = catalog_index(args.catalog)
    agent = Agent(args.catalog)

    pristine = {
        "COVERAGE_WEIGHT": R.COVERAGE_WEIGHT,
        "PHRASE_WEIGHT": R.PHRASE_WEIGHT,
        "PARTIAL_PHRASE_CREDIT": R.PARTIAL_PHRASE_CREDIT,
        "RERANK_POOL": T.RERANK_POOL,
        "CATEGORY_ROUTE_WEIGHT": T.CATEGORY_ROUTE_WEIGHT,
        "DENSE_ROUTE_WEIGHT": T.DENSE_ROUTE_WEIGHT,
        "PHRASE_ROUTE_WEIGHT": T.PHRASE_ROUTE_WEIGHT,
        "MAX_PHRASE_ROUTES": T.MAX_PHRASE_ROUTES,
        "PHRASE_DF_MAX": T.PHRASE_DF_MAX,
        "DISCLOSURE_SCHEDULE": A.DISCLOSURE_SCHEDULE,
    }
    pristine_fields = dict(T.FIELD_FACTORS)

    def restore() -> None:
        for key, value in pristine.items():
            if key == "DISCLOSURE_SCHEDULE":
                module = A
            elif key in RANKING_KEYS:
                module = R
            else:
                module = T
            setattr(module, key, value)
        T.FIELD_FACTORS.clear()
        T.FIELD_FACTORS.update(pristine_fields)
        # FIELD_FACTORS is cached per product; a stale cache measures the last variant.
        agent.index._profile_cache.clear()

    def run(label: str) -> float:
        result = evaluate(agent, samples, catalog_ids, categories, products)
        score = result["recommended_technical_score"]
        flag = "" if abs(score - BASELINE) < NOISE_FLOOR else "  <-- OUTSIDE NOISE FLOOR"
        print(
            "%-34s %.6f  %+.6f   HR %.4f  MRR %.6f  MTTC %.3f%s"
            % (label, score, score - BASELINE, result["hit_rate_at_10"],
               result["mrr"], result["mttc"], flag),
            flush=True,
        )
        return score

    restore()
    control = run("control (shipped)")
    if abs(control - BASELINE) > 1e-9:
        raise SystemExit(
            "control scored %.6f, expected %.6f -- the harness or the agent changed; "
            "fix that before trusting any variant." % (control, BASELINE)
        )

    for key in selected:
        name, variants = AXES[key]
        print("\n--- %s. %s ---" % (key, name), flush=True)
        for label, apply in variants:
            restore()
            apply()
            run(label)

    restore()
    print("\nDone. %d axes. All constants restored." % len(selected), flush=True)
    print("Deltas under %.2f are noise on a 200-session set -- say so rather than "
          "claiming a win." % NOISE_FLOOR, flush=True)


if __name__ == "__main__":
    main()
