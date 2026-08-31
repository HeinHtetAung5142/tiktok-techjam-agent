"""Compatibility shim: `from starter.agent import Agent` against the bundle.

The official harness we were given imports the agent by that path
(`evaluator/local_evaluator.py:12`). The bundle's canonical entry point is `agent.py` at
its root, per `docs/submission_rules.md`, so this package exists purely so a harness
expecting the starter-kit path finds the same object rather than an ImportError.

Both names resolve to one implementation in `src/` -- there is no second copy.
"""

try:
    from src.agent import Agent
except ImportError:  # pragma: no cover -- depends on how the harness was launched
    # Same fallback as the bundle's root `agent.py`, but this module sits one level down,
    # so the bundle root is our parent directory. See that file for why it is needed.
    import os
    import sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from src.agent import Agent

__all__ = ["Agent"]
