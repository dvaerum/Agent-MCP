"""MCP prompts subsystem (plan Phase 6).

Single source of truth for the Prompt Book catalogue is
`agent_mcp/prompts/catalog.json`. Both the MCP backend (via
`@server.list_prompts()` / `@server.get_prompt()` in main_app.py)
and the dashboard's REST consumer
(`GET /api/prompts/catalog`) read from this file.

The dashboard's TypeScript copy at
`agent_mcp/dashboard/lib/prompt-book.ts` currently inlines the
data; a sync test
(`tests/test_prompts.py::test_typescript_and_json_catalogs_in_sync`)
catches drift. Migrating the dashboard to fetch from the REST
endpoint at runtime is a follow-up.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict


_CATALOG_PATH = Path(__file__).resolve().parent / "catalog.json"


@lru_cache(maxsize=1)
def load_catalog() -> Dict[str, Any]:
    """Load and cache the prompt catalogue from disk.

    Returns the raw dict (not a deep copy) — callers MUST treat
    the structure as read-only. Wrap your access through
    `render_prompt` if you need a defensive copy.
    """
    with _CATALOG_PATH.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def get_prompt(prompt_id: str) -> Dict[str, Any] | None:
    """Return a single catalogue entry by id, or None if absent."""
    for entry in load_catalog().get("prompts", []):
        if entry.get("id") == prompt_id:
            return entry
    return None


def render_prompt(prompt_id: str, arguments: Dict[str, str]) -> str:
    """Render a catalogue entry's template with the supplied
    arguments substituted into `{{VARIABLE}}` placeholders.

    Missing arguments are substituted as the empty string (so the
    rendered prompt never contains a stray `{{VAR}}` even when
    the caller didn't supply every variable). Variable names are
    matched verbatim — the catalog's variable names are
    UPPER_SNAKE_CASE by convention.

    Raises KeyError if the prompt_id is unknown.
    """
    entry = get_prompt(prompt_id)
    if entry is None:
        raise KeyError(f"Unknown prompt id: {prompt_id}")

    template = entry.get("template", "")
    declared = {v["name"] for v in entry.get("variables", [])}
    rendered = template

    # Substitute declared variables in a stable order so the
    # output is deterministic.
    for var_name in sorted(declared):
        value = str(arguments.get(var_name, ""))
        rendered = rendered.replace("{{" + var_name + "}}", value)

    return rendered
