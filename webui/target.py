"""The "guess the hidden product" game piece.

A random catalog product is shown at the top of the page so the user has something to
hunt for. Two views of it are offered:

  * **intent card** -- the <=4 disclosures the simulated customer would actually reveal,
    built by the evaluator's own `intent_card()` so the game matches the real task;
  * **full details** -- the whole product row, for when the user just wants to look.

Nothing here is ever passed to the agent. `pick()` builds the payload, hands it to the
browser, and the server forgets it -- see the isolation note in `agent_bridge.py`.
"""

from __future__ import annotations

import random

# Read-only import of a pure function (no RNG, no I/O, no global state):
# evaluator/local_evaluator.py:52. Using the evaluator's own card builder means the
# disclosures the user sees are exactly the ones the scored simulator would hand over.
from evaluator.local_evaluator import intent_card

from webui.catalog import CatalogReader, card


# A product whose features/details are empty yields a card of one vague line, which makes
# for an unplayable round. Reroll a few times before giving up and showing it anyway.
_MIN_CONSTRAINTS = 2
_MAX_ATTEMPTS = 12


def disclosures(product: dict) -> list[str]:
    """The customer's whole disclosable pool, in the order they would reveal it.

    `intent_card` splits its cleaned constraints into `hard_constraints[:2]` and
    `soft_preferences[2:4]`; the customer never knows more than those four things
    (evaluator/local_evaluator.py:69-71). Flatten them back into one ordered list.
    """
    try:
        built = intent_card(product)
    except Exception:
        return []
    seen: list[str] = []
    for value in [*built.get("hard_constraints", []), *built.get("soft_preferences", [])]:
        text = str(value).strip()
        if text and text not in seen:
            seen.append(text)
    return seen


def pick(reader: CatalogReader, rng: random.Random | None = None) -> dict:
    """One random target, in both views. The server stores none of this."""
    rng = rng or random
    best: dict | None = None
    for _ in range(_MAX_ATTEMPTS):
        product = reader.get(reader.random_asin(rng))
        if product is None:
            continue
        candidate = _payload(product)
        if len(candidate["intent_card"]) >= _MIN_CONSTRAINTS:
            return candidate
        # Keep the richest near-miss so a run of sparse rows still returns something.
        if best is None or len(candidate["intent_card"]) > len(best["intent_card"]):
            best = candidate
    if best is None:
        raise RuntimeError("could not draw a target product from the catalog")
    return best


def _payload(product: dict) -> dict:
    return {
        "parent_asin": product.get("parent_asin", ""),
        "intent_card": disclosures(product),
        # feature_limit=None: the "full details" view shows everything the row has.
        "details": card(product, feature_limit=None),
    }
