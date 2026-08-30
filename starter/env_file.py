"""Read and write the repo's `.env`, using nothing but the standard library.

Why this exists
---------------
`starter/llm.py` is configured entirely from environment variables (a hard rule: no key
ever lands in the repo). That is correct, but until now nothing actually *loaded* a
`.env` file -- it was gitignored and then ignored, so a teammate who wrote one saw no
effect. This module closes that loop and gives the WebUI somewhere to persist a key the
operator typed in.

Two rules keep this safe:

1. **Nothing on the scored path imports this module.** `starter/agent.py`,
   `dialog_state.py`, `retrieval.py`, `ranking.py` and `dense_retrieval.py` never touch
   it, so the evaluator run performs no file I/O beyond the catalog and remains
   byte-identical. It is called by `webui/` and `tools/` only, and `tools/verify_llm.py`
   asserts that.
2. **A real environment variable always wins.** `load_env_file` defaults to
   `override=False`, so an export in the shell beats the file. Judging, CI and a
   deliberate `SHOPPING_COPILOT_LLM=off` in front of a command all keep working exactly
   as they did.

The file itself is gitignored (`.gitignore`), and the template written by
`ensure_env_file` contains no key -- only blank slots and the documentation for them.

Variable names are **provider-neutral** (`SHOPPING_COPILOT_*`), because the client is plain
OpenAI-compatible chat completions and naming the settings after one vendor was wrong the
moment the default moved off it. The `SILICONFLOW_*` names feature 13 shipped with are
still read (`LEGACY_ALIASES`), and are quietly rewritten to the canonical spelling the next
time this module writes the file -- so an existing `.env` keeps working and then migrates
itself, rather than breaking someone mid-demo.
"""

from __future__ import annotations

import os
from pathlib import Path

from starter import llm


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_PATH = REPO_ROOT / ".env"

# The keys we manage, canonical spelling. Anything else already in the file is preserved
# untouched -- this module rewrites lines it recognizes and appends ones it does not find,
# never truncates. Kept in sync with `starter/llm.py`; imported rather than re-typed so the
# two cannot drift.
MANAGED_KEYS = (
    llm.MODE_ENV,
    llm.API_KEY_ENV,
    llm.MODEL_ENV,
    llm.BASE_URL_ENV,
)

# Old spelling -> canonical. Read on load, rewritten on save. See the module docstring.
LEGACY_ALIASES = {legacy: canonical for canonical, legacy in llm.LEGACY_ENV.items()}

TEMPLATE = """\
# Shopping Copilot -- local configuration. Generated automatically; safe to edit.
#
# This file is gitignored. NEVER commit a real key (a hard rule in CLAUDE.md).
#
# The agent is fully offline by default. It makes a model call only when BOTH
# SHOPPING_COPILOT_LLM and SHOPPING_COPILOT_API_KEY are set -- neither alone does
# anything, and an unrecognized mode fails closed to `off`.
#
#   off       (default) no model call at all. This is the configuration the organizer
#             runs, and it reproduces TechnicalScore 0.912205 exactly.
#   freeform  the model helps interpret a HUMAN typing prose into the WebUI. That branch
#             is unreachable while scoring, so this mode cannot move the score.
#   expand    additionally adds a low-weight keyword route to retrieval. Experimental,
#             unmeasured against a live model, and NOT reproducible run to run.
SHOPPING_COPILOT_LLM=off

# Your API key, for whichever endpoint SHOPPING_COPILOT_BASE_URL points at.
# Leave blank to stay fully offline. The default endpoint is OpenRouter, which needs no
# identity verification: https://openrouter.ai/keys
SHOPPING_COPILOT_API_KEY=

# Optional. Blank means the built-in defaults, which are
# google/gemma-4-26b-a4b-it:free on https://openrouter.ai/api/v1
#
# Any OpenAI-compatible endpoint works. Recipes in docs/LLM_SETUP.md:
#   Ollama, fully local:  http://localhost:11434/v1       + e.g. qwen3:8b
#                         (any non-empty API key; Ollama ignores it)
#   SiliconFlow:          https://api.siliconflow.cn/v1   + Qwen/Qwen3-8B
SHOPPING_COPILOT_MODEL=
SHOPPING_COPILOT_BASE_URL=
"""


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _quote(value: str) -> str:
    """Quote only when the value would otherwise not survive a round trip."""
    if value == "" or (value == value.strip() and "#" not in value and "\n" not in value):
        return value
    return '"%s"' % value.replace("\\", "\\\\").replace('"', '\\"')


def ensure_env_file(path: str | Path | None = None) -> tuple[Path, bool]:
    """Create `.env` from the template if it is missing. Returns `(path, created)`.

    Never overwrites an existing file, so re-running it can't clobber a key. Any failure
    (read-only checkout, permissions) is swallowed and reported as `created=False` --
    scaffolding a convenience file must never stop the program that asked for it.
    """
    target = Path(path) if path is not None else DEFAULT_ENV_PATH
    try:
        if target.exists():
            return target, False
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(TEMPLATE, encoding="utf-8")
        return target, True
    except OSError:
        return target, False


def parse_env_file(path: str | Path | None = None) -> dict[str, str]:
    """`.env` as a dict. `{}` if it is missing or unreadable -- never raises."""
    target = Path(path) if path is not None else DEFAULT_ENV_PATH
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}

    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        if stripped.lower().startswith("export "):
            stripped = stripped[len("export "):].lstrip()
        name, _, raw = stripped.partition("=")
        name = name.strip()
        if name:
            values[name] = _unquote(raw)
    return values


def canonical_values(path: str | Path | None = None) -> dict[str, str]:
    """`.env` as a dict with legacy names folded onto their canonical spelling.

    A file carrying both spellings keeps the canonical one -- the same precedence
    `llm.env_value` applies to the environment, so the file and the shell cannot disagree
    about which wins.
    """
    raw = parse_env_file(path)
    values = {name: value for name, value in raw.items() if name not in LEGACY_ALIASES}
    for legacy, canonical in LEGACY_ALIASES.items():
        if raw.get(legacy) and not values.get(canonical):
            values[canonical] = raw[legacy]
    return values


def load_env_file(path: str | Path | None = None, override: bool = False) -> dict[str, str]:
    """Load `.env` into `os.environ`. Returns the names actually applied.

    `override=False` (the default) means a variable already exported in the shell wins.
    Empty values are skipped rather than written as empty strings, so a blank
    `SHOPPING_COPILOT_API_KEY=` in the template leaves the agent offline instead of setting
    a falsy key that later code has to special-case.

    Legacy `SILICONFLOW_*` entries are applied under their canonical name, so an old `.env`
    works without the rest of the codebase knowing the old spelling exists.
    """
    applied: dict[str, str] = {}
    for name, value in canonical_values(path).items():
        if not value:
            continue
        if not override and os.environ.get(name):
            continue
        os.environ[name] = value
        applied[name] = value
    return applied


def update_env_file(values: dict[str, str], path: str | Path | None = None) -> Path:
    """Write `values` into `.env`, preserving every comment and unmanaged line.

    A key already present is rewritten in place, so the template's documentation stays
    where the operator can read it. A key not present is appended. Raises `OSError` if the
    file cannot be written -- callers that persist on a user's behalf should say so.

    **This is also where an old file migrates.** A legacy `SILICONFLOW_*` line is rewritten
    to its canonical spelling rather than left behind as a stale duplicate that would
    quietly lose to the new one. Migration happens on any write, so a file only carries the
    old names until the next time anything saves.
    """
    target = Path(path) if path is not None else DEFAULT_ENV_PATH
    ensure_env_file(target)
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        lines = TEMPLATE.splitlines()

    # A legacy line with nothing new to say still carries the operator's value, so migrate
    # it rather than dropping it. An explicit `values` entry always wins over the file.
    existing = parse_env_file(target)
    remaining = dict(values)
    for legacy, canonical in LEGACY_ALIASES.items():
        if existing.get(legacy) and canonical not in remaining:
            remaining[canonical] = existing[legacy]

    output: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            output.append(line)
            continue
        name = stripped.partition("=")[0].strip()
        # Rewrite a legacy line to its canonical spelling *in place*, rather than dropping
        # it and appending the replacement at the end. Position matters: these lines sit
        # under the comment block that documents them, and a migrated file should still
        # read like the template -- including keeping an empty slot empty rather than
        # deleting it and leaving the explanation pointing at nothing.
        if name in LEGACY_ALIASES:
            canonical = LEGACY_ALIASES[name]
            if canonical in remaining:
                output.append("%s=%s" % (canonical, _quote(remaining.pop(canonical))))
            else:
                output.append("%s=%s" % (canonical, _quote(existing.get(name, ""))))
            continue
        if name in remaining:
            output.append("%s=%s" % (name, _quote(remaining.pop(name))))
        else:
            output.append(line)

    for name, value in remaining.items():
        output.append("%s=%s" % (name, _quote(value)))

    target.write_text("\n".join(output).rstrip("\n") + "\n", encoding="utf-8")
    return target


def bootstrap(path: str | Path | None = None) -> dict:
    """Scaffold `.env` if absent, then load it. What every entry point outside the agent wants.

    Returns a small record for the caller to print -- never the values themselves, so a
    key cannot end up in a log or a terminal screenshot.
    """
    target, created = ensure_env_file(path)
    applied = load_env_file(target)
    return {
        "path": str(target),
        "created": created,
        "loaded": sorted(applied),
        "exists": target.exists(),
    }
