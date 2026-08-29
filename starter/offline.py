"""Offline-safe operation and the fail-soft response contract.

Two jobs, both insurance rather than optimization.

**1. Never return a malformed response, whatever fails inside.** The evaluator scores a
raised exception or a wrong-shaped payload as an outright miss -- it swaps in an empty
response and moves on (``evaluator/local_evaluator.py:239-244``) -- so one unhandled
fault costs the turn that might have been the hit. Everything here exists so
``Agent.respond`` can promise a contract-valid dict under any input and any internal
failure.

**2. Degrade rather than die when a dependency is missing.** Official judging may run
with network access disabled (``docs/competition_specification.md``), and may run on a
Python built without FTS5 or without the numpy/scipy/scikit-learn stack. This agent
makes no network call of any kind, so "offline" costs it exactly nothing -- but that
claim is only worth something if it is *enforced and tested*, which is what
``tools/offline_check.py`` and ``tests/test_offline_safety.py`` do.

Nothing here is load-bearing on the happy path: against the public set the coercions are
the identity function and the fallback slate is never reached. That is the design goal,
not an accident -- see ``docs/features/11-offline-safe-fallback.md``.
"""

from __future__ import annotations

import json
from pathlib import Path


# The ask_attribute enum from docs/agent_api_contract.json. Anything outside this set is
# malformed output; the evaluator's own copy is at evaluator/local_evaluator.py:17-20.
ALLOWED_ATTRIBUTES = frozenset(
    (
        "category", "material", "color", "size", "style",
        "brand", "budget", "feature", "use_case", "other",
    )
)

# `turn_request` pins top_k to a const 10, and `turn_response.recommendations` caps at
# maxItems 100. Both are enforced here so a harness that ignores its own contract still
# gets a legal answer instead of an exception.
DEFAULT_TOP_K = 10
MAX_RECOMMENDATIONS = 100

# How many products the catastrophic-failure slate holds. Exactly one scored page: only
# the first 10 recommendations are ever scored, so more would be dead weight.
FALLBACK_SLATE_SIZE = 10

# Reported honestly as zeros -- this agent makes no model call, so there is nothing to
# count. Explicit zeros ("we used none") rather than an absent key ("they didn't say").
NO_MODEL_USAGE = {"prompt_tokens": 0, "completion_tokens": 0}


def coerce_session_id(value: object) -> str:
    """Any session key, rendered as the string the session dict is keyed by."""
    try:
        return str(value)
    except Exception:  # noqa: BLE001 - a __str__ that raises must not cost the turn
        return ""


def coerce_user_message(value: object) -> str:
    """`None` and non-strings become text rather than a TypeError three frames down.

    ``DialogState.observe`` runs regexes straight over this (dialog_state.py:104); a
    ``None`` there raises and, without this, escapes ``respond()`` entirely.
    """
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    try:
        return str(value)
    except Exception:  # noqa: BLE001 - see coerce_session_id
        return ""


def coerce_turn(value: object, default: int = 1) -> int:
    """A turn number the disclosure schedule can be indexed with.

    Not clamped at the contract's maximum of 10: ``disclosure_limit`` already saturates
    past the end of ``DISCLOSURE_SCHEDULE``, and silently rewriting turn 12 into turn 10
    would hide a harness bug rather than survive it.
    """
    try:
        turn = int(value)
    except (TypeError, ValueError, OverflowError):
        # OverflowError is not paranoia: int(float("inf")) raises it, and float("nan")
        # raises ValueError. Both are things a sloppy harness can hand us.
        return default
    return turn if turn >= 1 else default


def coerce_top_k(value: object, default: int = DEFAULT_TOP_K) -> int:
    """How many recommendations we are allowed to return.

    A caller asking for zero gets zero -- returning more than was asked for is exactly
    the kind of over-delivery a strict harness would reject as malformed.
    """
    try:
        top_k = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(0, min(top_k, MAX_RECOMMENDATIONS))


def coerce_attribute(value: object) -> str | None:
    """An attribute from the contract enum, or `null`.

    An out-of-enum string is worse than no question at all: it is malformed output,
    while ``null`` merely wastes the turn (evaluator/local_evaluator.py:171).
    """
    if isinstance(value, str) and value in ALLOWED_ATTRIBUTES:
        return value
    return None


def coerce_recommendations(payload: object, top_k: int = DEFAULT_TOP_K) -> list[dict]:
    """A contract-shaped, deduplicated, length-capped recommendation list.

    Accepts what the pipeline produces (``[{"parent_asin": ...}]``) and also bare id
    strings, so a future route returning the simpler form cannot silently score zero.
    ``score`` is the only other key the contract permits, so it survives and everything
    else is dropped -- ``recommendations.items`` sets ``additionalProperties: false``.
    """
    # Checked before the loop, not only inside it: the cap below is tested *after* each
    # append, so a top_k of 0 would otherwise return one item.
    if not isinstance(payload, list) or top_k <= 0:
        return []
    result: list[dict] = []
    seen: set[str] = set()
    for item in payload:
        raw = item.get("parent_asin") if isinstance(item, dict) else item
        if raw is None:
            continue
        try:
            parent_asin = str(raw).strip()
        except Exception:  # noqa: BLE001 - see coerce_session_id
            continue
        # The evaluator drops blanks and repeats anyway (local_evaluator.py:103); doing
        # it here means the slot is spent on a product that can actually be scored.
        if not parent_asin or parent_asin in seen:
            continue
        seen.add(parent_asin)
        entry: dict = {"parent_asin": parent_asin}
        if isinstance(item, dict) and isinstance(item.get("score"), (int, float)):
            entry["score"] = item["score"]
        result.append(entry)
        if len(result) >= top_k:
            break
    return result


def coerce_usage(payload: object) -> dict:
    """The two non-negative integer counters the contract requires, and nothing else."""
    usage = dict(NO_MODEL_USAGE)
    if isinstance(payload, dict):
        for key in ("prompt_tokens", "completion_tokens"):
            value = payload.get(key)
            # bool is an int subclass; True as a token count is a bug, not a count.
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                usage[key] = value
    return usage


def safe_response(
    message: object,
    ask_attribute: object,
    recommendations: object,
    usage: object = None,
    top_k: int = DEFAULT_TOP_K,
) -> dict:
    """Build the response dict, guaranteeing every field satisfies `turn_response`.

    The single place the payload shape is decided, so the happy path and the fallback
    path cannot drift apart.
    """
    return {
        "message": message if isinstance(message, str) else "",
        "ask_attribute": coerce_attribute(ask_attribute),
        "recommendations": coerce_recommendations(recommendations, top_k),
        # Fresh dict per turn: the evaluator accumulates these, and handing out a shared
        # module-level object invites a caller mutating every turn's usage.
        "usage": coerce_usage(usage),
    }


def coerce_response(payload: dict, top_k: int = DEFAULT_TOP_K) -> dict:
    """Validate a response the pipeline already built. Identity on well-formed input."""
    return safe_response(
        payload.get("message"),
        payload.get("ask_attribute"),
        payload.get("recommendations"),
        payload.get("usage"),
        top_k,
    )


def catalog_fallback_asins(
    catalog_path: str | Path, limit: int = FALLBACK_SLATE_SIZE
) -> list[str]:
    """A valid slate of catalog ids, read straight from the file.

    Deliberately independent of sqlite, FTS5, and the dense stack: this is the path used
    when the index itself could not be built, so it must not depend on anything the
    index depends on. It stops after `limit` rows, so the cost is a few hundred
    microseconds rather than a second pass over 50k products.

    Relevance is not the point and cannot be -- with no query and no catalog statistics
    there is nothing to be relevant *to*. The point is that a totally broken agent still
    answers with real, scoreable product ids instead of an empty list.
    """
    identifiers: list[str] = []
    try:
        with Path(catalog_path).open(encoding="utf-8") as handle:
            for line in handle:
                if len(identifiers) >= limit:
                    break
                try:
                    parent_asin = str(json.loads(line)["parent_asin"]).strip()
                except Exception:  # noqa: BLE001 - one unreadable row is not fatal
                    continue
                if parent_asin:
                    identifiers.append(parent_asin)
    except Exception:  # noqa: BLE001 - no catalog at all: answer empty, do not raise
        return []
    return identifiers
