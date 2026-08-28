"""The official competition entry point.

Keeps the published reset()/respond() contract and orchestrates the other modules:
dialog_state.py decides what we know and what to ask, retrieval.py turns that into
a ranked list of parent_asins.
"""

from __future__ import annotations

from pathlib import Path

from starter import retrieval
from starter.dialog_state import DialogState
from starter.retrieval import CatalogIndex


# Cap on how many distinct terms reach the FTS5 query. Long OR expressions get slow
# and start matching on incidental words. Higher than the single-message era needed,
# because the query now spans everything the customer has revealed all session.
MAX_QUERY_TERMS = 64


class Agent:
    """Multi-turn shopping agent: FTS5 retrieval, no LLM, no network."""

    def __init__(self, catalog_path: str | Path = "data/catalog.jsonl") -> None:
        self.index = CatalogIndex(catalog_path)
        self._sessions: dict[str, DialogState] = {}

    def reset(self, session_id: str, user_profile: dict) -> None:
        # The profile is anonymized and may be used for personalization.
        self._sessions[session_id] = DialogState()

    def respond(
        self,
        session_id: str,
        user_message: str,
        turn: int,
        top_k: int,
    ) -> dict:
        if session_id not in self._sessions:
            raise RuntimeError("reset must be called before respond")
        state = self._sessions[session_id]
        state.observe(user_message, turn)

        # Query against everything revealed so far, not just this turn's message.
        # Retrieving on the latest reply alone throws away the category from turn 1.
        query_terms = retrieval.terms(state.evidence_text())[:MAX_QUERY_TERMS]
        is_buying = state.is_buying

        recommendations = self.index.retrieve(
            query_terms,
            state.and_terms() if is_buying else [],
            state.price_max() if is_buying else None,
            top_k,
        )

        # Recommendations are scored every turn, so asking costs us nothing and is the
        # only way the customer ever discloses more.
        ask_attribute = state.next_attribute()

        return {
            "message": state.message(ask_attribute),
            "ask_attribute": ask_attribute,
            "recommendations": recommendations,
            "usage": {"prompt_tokens": 0, "completion_tokens": 0},
        }
