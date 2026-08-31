"""Generic attribute facets for free-form (human) input.

Why this exists
---------------
`DialogState` has exactly three slots -- price, colour, material -- so a person who types
"round neck, blue, cotton, under 50 dollars, men tshirt" gets "Narrowed to items matching
blue, cotton, under $50.00" and a page of women's dresses. Two distinct failures:

1. **Gender was an ordinary keyword.** `men` has df 14,908 -> IDF 1.21, the *lowest*-
   weighted term in that query at 3% of its mass, in a catalog where 32,347 of 50,000
   products mention "women". Nothing penalised a product for matching the *opposite*
   value, so women's items swept the top 10.
2. **A hard `AND "men"` does not fix it.** 5,900 products contain "men" outside their
   title -- keyword spam like "gifts for men women teens" -- so women's listings satisfy
   the filter anyway. Measured: requiring "men" still returned women's items at #1.

What does work is scoping the value to the **title** and demoting titles that assert a
*sibling* value: 9,039 products have "men" in the title against 21,008 for "women", and
only 1,350 have both. That produced 8/8 men's crew-neck tees.

So this module generalises that one fix into a mechanism. A facet is a group of mutually
exclusive values; stating one implies rejecting its siblings. Adding a new parameter is a
dictionary entry here, not new code anywhere.

**Score safety.** Nothing here is reachable from a scored turn. `detect_facets` is called
only from `DialogState._observe_freeform`, which the simulated customer never reaches --
566 `observe()` calls on the public set, 0 of them free-form. `DialogState.facet_values()`
therefore returns `{}` on every scored turn, and `CatalogIndex.retrieve` skips the whole
facet block on a falsy value. This is the same empty-by-default pattern `avoid_terms`
already uses, and it is enforced by a test, not by convention -- see
`tools/verify_features.py`.
"""

from __future__ import annotations

import re


# group -> canonical value -> the surface forms a person might type, which are also the
# forms matched against product titles. Order within a group does not matter; values must
# be mutually exclusive *within* a group, because stating one demotes all the others.
#
# Multi-word forms become FTS5 phrase queries, single words become plain terms. Keep forms
# lowercase and free of punctuation: the index tokenizer drops apostrophes, so "men's"
# arrives as the two tokens "men" and "s" and the bare "men" form already covers it.
FACET_GROUPS: dict[str, dict[str, tuple[str, ...]]] = {
    "gender": {
        "men": ("men", "mens", "male", "guys", "gentlemen"),
        "women": ("women", "womens", "female", "ladies", "misses"),
        "boys": ("boys", "toddler boys"),
        "girls": ("girls", "toddler girls"),
    },
    "neckline": {
        "crew neck": ("crew neck", "crewneck", "round neck", "round collar"),
        "v neck": ("v neck", "vneck"),
        "scoop neck": ("scoop neck",),
        "turtleneck": ("turtleneck", "turtle neck", "mock neck"),
        "henley": ("henley",),
        "collared": ("collared", "polo collar", "button down collar"),
    },
    "sleeve": {
        "short sleeve": ("short sleeve", "short sleeved"),
        "long sleeve": ("long sleeve", "long sleeved"),
        "sleeveless": ("sleeveless", "tank top", "spaghetti strap"),
        "three quarter sleeve": ("3 4 sleeve", "three quarter sleeve"),
    },
    "fit": {
        "slim": ("slim fit", "slim", "fitted"),
        "regular": ("regular fit", "classic fit"),
        "relaxed": ("relaxed fit", "loose fit", "loose", "oversized"),
        "skinny": ("skinny",),
    },
    "rise": {
        "high rise": ("high rise", "high waisted", "high waist"),
        "mid rise": ("mid rise", "mid waist"),
        "low rise": ("low rise", "low waist"),
    },
    "length": {
        "mini": ("mini",),
        "midi": ("midi", "knee length"),
        "maxi": ("maxi", "ankle length", "full length"),
    },
    "closure": {
        "zipper": ("zipper", "zip up", "zip closure"),
        "button": ("button closure", "button up", "button down"),
        "pull on": ("pull on", "pullover", "elastic waist"),
        "lace up": ("lace up", "laced"),
        "buckle": ("buckle",),
        "slip on": ("slip on",),
    },
    "pattern": {
        "solid": ("solid color", "solid colour", "plain"),
        "striped": ("striped", "stripe"),
        "plaid": ("plaid", "checkered", "gingham"),
        "floral": ("floral", "flower print"),
        "polka dot": ("polka dot",),
        "camo": ("camo", "camouflage"),
        "tie dye": ("tie dye",),
        "graphic": ("graphic print", "graphic tee", "printed graphic"),
    },
    "occasion": {
        "casual": ("casual", "everyday", "leisure"),
        "formal": ("formal", "dressy", "evening"),
        "business": ("business casual", "office", "work wear"),
        "athletic": ("athletic", "workout", "gym", "running", "sports"),
        "wedding": ("wedding", "bridal"),
    },
    "season": {
        "winter": ("winter",),
        "summer": ("summer",),
        "spring": ("spring",),
        "fall": ("fall", "autumn"),
    },
}

# Precompiled matchers, longest form first so "long sleeve" wins over a bare "long" and
# "round neck" is not shadowed by some future "round" form in the same group.
_FORM_PATTERNS: list[tuple[str, str, str, re.Pattern]] = []
for _group, _values in FACET_GROUPS.items():
    for _value, _forms in _values.items():
        for _form in sorted(_forms, key=len, reverse=True):
            _FORM_PATTERNS.append(
                (_group, _value, _form, re.compile(rf"\b{re.escape(_form)}\b", re.IGNORECASE))
            )
_FORM_PATTERNS.sort(key=lambda row: len(row[2]), reverse=True)


def detect_facets(text: str) -> dict[str, str]:
    """`{group: canonical value}` for every facet this text states. Free-form path only.

    First statement wins within a group, so a later passing mention cannot flip a value
    the person led with. Returns `{}` for empty text, which is what keeps every caller's
    facet block inert by default.
    """
    if not text or not text.strip():
        return {}
    found: dict[str, str] = {}
    for group, value, _form, pattern in _FORM_PATTERNS:
        if group in found:
            continue
        if pattern.search(text):
            found[group] = value
    return found


def _form_expression(form: str, column: str = "title") -> str:
    """One surface form as a column-scoped FTS5 term or phrase."""
    words = [word for word in re.findall(r"[a-z0-9]+", form.lower()) if word]
    if not words:
        return ""
    body = f'"{words[0]}"' if len(words) == 1 else '"' + " ".join(words) + '"'
    return f"{column}:{body}"


def title_expression(facets: dict[str, str]) -> str:
    """An FTS5 query matching titles that assert *every* stated facet value.

    Title-scoped on purpose: matching anywhere is what let keyword spam
    ("gifts for men women teens") satisfy a gender requirement.
    """
    clauses = []
    for group, value in sorted(facets.items()):
        forms = FACET_GROUPS.get(group, {}).get(value, ())
        alternatives = [expression for expression in map(_form_expression, forms) if expression]
        if alternatives:
            clauses.append("(" + " OR ".join(alternatives) + ")")
    return " AND ".join(clauses)


def sibling_forms(facets: dict[str, str]) -> list[str]:
    """Surface forms of the values the person did *not* choose, per stated group.

    A title carrying one of these is asserting the opposite of what was asked for --
    "Women's" when they said men -- and gets demoted. Values from groups the person never
    mentioned are not included: silence is not a preference.
    """
    forms: list[str] = []
    for group, chosen in facets.items():
        for value, value_forms in FACET_GROUPS.get(group, {}).items():
            if value == chosen:
                continue
            for form in value_forms:
                if form not in forms:
                    forms.append(form)
    return forms


def describe(facets: dict[str, str]) -> list[str]:
    """Stated facet values, for the customer-facing message. Stable order."""
    return [facets[group] for group in FACET_GROUPS if group in facets]
