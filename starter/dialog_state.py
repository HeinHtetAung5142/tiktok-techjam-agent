"""Per-session conversation state.

Owns what the customer has told us and what we should ask next. Knows nothing about
the catalog or about FTS5 — see retrieval.py for that.

Two jobs:

1. **Remember.** The customer reveals target-derived text one turn at a time. Every
   scrap of it is accumulated as retrieval evidence, because the words a customer uses
   to describe their requirement are frequently lifted straight from the product's own
   metadata.
2. **Ask.** The simulated customer only discloses a constraint when we name the
   attribute it belongs to. Asking nothing reveals nothing, so we always ask something.
"""

from __future__ import annotations

import re


COLOR_RE = re.compile(
    r"\b(black|white|blue|red|pink|green|brown|gray|grey|purple|yellow|orange)\b", re.IGNORECASE
)
MATERIAL_RE = re.compile(
    r"\b(cotton|polyester|nylon|leather|wool|spandex|silk|rayon|fabric)\b", re.IGNORECASE
)
PRICE_DOLLAR_RE = re.compile(r"\$\s*(\d+(?:\.\d{1,2})?)")
PRICE_PHRASE_RE = re.compile(
    r"(?:under|below|less than|no more than|up to|at most)\s+\$?\s*(\d+(?:\.\d{1,2})?)",
    re.IGNORECASE,
)

# The customer's reply shapes. Matching these is how a question turns into evidence.
DISCLOSURE_RE = re.compile(r"what matters is:\s*(.+?)\s*$", re.IGNORECASE)
OVERRIDE_RE = re.compile(r"what I need is:\s*(.+?)\s*$", re.IGNORECASE)
# "an additional preference" means the attribute is genuinely empty -- stop asking it.
EXHAUSTED_RE = re.compile(r"don't have an additional preference for (\w+)", re.IGNORECASE)
# "a preference" (no "additional") is the boundary customer deferring to us once. That is
# a one-off deflection, not evidence the attribute is empty, so it must NOT retire it.
DECLINE_RE = re.compile(r"don't have a preference for (\w+)", re.IGNORECASE)

SLOTS = ("price_max", "color", "material")
HARD_FILTER_SLOTS = ("color", "material")

# Order we work through when choosing what to ask about.
#
# "other" leads because it is the only attribute that cannot whiff: it is the catch-all
# "is there anything else that matters?" question, so it matches any constraint the
# customer has not yet volunteered rather than only one bucket. Asking a specific
# attribute the target has no constraint for burns the turn for nothing. The specific
# attributes follow so the conversation still reads naturally once the broad ask is spent,
# and so we degrade sensibly if a future simulator treats "other" more strictly.
ASK_ORDER = (
    "other", "feature", "material", "color", "style",
    "size", "budget", "use_case", "brand", "category",
)

QUESTIONS = {
    # Plain ASCII: this text is read aloud in the demo and shown in terminals whose
    # codepage mangles dashes and quotes.
    "other": "Is there anything else that matters, like features, fit, or how you'll use it?",
    "feature": "Are there any specific features you need?",
    "material": "Do you have a material preference?",
    "color": "Any particular colour you're after?",
    "style": "What style or fit works best for you?",
    "size": "What size should I filter to?",
    "budget": "What's your budget?",
    "use_case": "What will you mainly be using it for?",
    "brand": "Any brand you prefer, or would rather avoid?",
    "category": "Which category should I focus on?",
}


def detect_constraints(message: str) -> dict[str, float | str | None]:
    color_match = COLOR_RE.search(message)
    material_match = MATERIAL_RE.search(message)
    price_match = PRICE_DOLLAR_RE.search(message) or PRICE_PHRASE_RE.search(message)
    return {
        "color": color_match.group(1).lower() if color_match else None,
        "material": material_match.group(1).lower() if material_match else None,
        "price_max": float(price_match.group(1)) if price_match else None,
    }


class DialogState:
    """Everything we have learned in one session."""

    def __init__(self) -> None:
        self.slots: dict[str, float | str | None] = {slot: None for slot in SLOTS}
        self.evidence: list[str] = []
        self.exhausted: set[str] = set()

    def observe(self, message: str, turn: int) -> None:
        """Absorb one customer message into state."""
        detected = detect_constraints(message)
        for key, value in detected.items():
            # First-write-wins. This is why intent override is still unhandled: a later
            # contradicting value lands in an already-filled slot and is dropped.
            if value is not None and self.slots[key] is None:
                self.slots[key] = value

        if turn == 1:
            # The opener carries the product category, which is the single most useful
            # retrieval signal in the whole session. Keep all of it.
            self.evidence.append(message)
            return

        exhausted = EXHAUSTED_RE.search(message)
        if exhausted:
            self.exhausted.add(exhausted.group(1).lower())
            return

        if DECLINE_RE.search(message):
            # Boundary customer deferring to our judgment. No evidence, but keep the
            # attribute in rotation -- they answer normally from here on.
            return

        disclosure = DISCLOSURE_RE.search(message) or OVERRIDE_RE.search(message)
        if disclosure:
            self.evidence.append(disclosure.group(1))

        # Anything else (e.g. the "ask me about one specific attribute" nudge) carries no
        # information about the target and is deliberately not accumulated.

    def next_attribute(self) -> str | None:
        """The next attribute worth asking about, or None once all are spent."""
        for attribute in ASK_ORDER:
            if attribute not in self.exhausted:
                return attribute
        return None

    def evidence_text(self) -> str:
        """Everything the customer has revealed, oldest first."""
        return " ".join(self.evidence)

    @property
    def is_buying(self) -> bool:
        """A disclosed hard constraint puts the session on the buying track."""
        return any(self.slots[slot] is not None for slot in SLOTS)

    def and_terms(self) -> list[str]:
        """Constraints strict enough to require as FTS5 AND terms."""
        return [str(self.slots[slot]) for slot in HARD_FILTER_SLOTS if self.slots[slot]]

    def price_max(self) -> float | None:
        value = self.slots["price_max"]
        return float(value) if value is not None else None

    def message(self, attribute: str | None) -> str:
        """Customer-facing text: what we did, then what we still need."""
        if self.is_buying:
            parts = [str(self.slots[slot]) for slot in HARD_FILTER_SLOTS if self.slots[slot]]
            if self.slots["price_max"] is not None:
                parts.append(f"under ${float(self.slots['price_max']):.2f}")
            detail = ", ".join(parts) if parts else "your requirement"
            said = f"Narrowed to items matching {detail}."
        else:
            said = "Here are the closest matches I found."

        question = QUESTIONS.get(attribute or "", "")
        return f"{said} {question}".strip()
