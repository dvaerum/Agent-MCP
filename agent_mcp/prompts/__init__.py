"""MCP prompts subsystem (plan Phase 6).

Single source of truth for the Prompt Book catalogue is
`agent_mcp/prompts/catalog.json`. Both the MCP backend (via
`@server.list_prompts()` / `@server.get_prompt()` in main_app.py)
and the dashboard's REST consumer
(`GET /api/prompts/catalog`) read from this file.

Candidate B + G (architecture review 2026-06-02): prompts now live
in a `prompt_registry: PromptRegistry` that subclasses the shared
`Registry[T]`. The catalog gains an optional `"visibility"` field
(default `"any"`) so admin-only prompts hide from worker
`prompts/list` and `prompts/get` calls — the same role-based gating
tools have had since Phase 7g.

The dashboard's TypeScript copy at
`agent_mcp/dashboard/lib/prompt-book.ts` used to inline the data;
it now fetches from `GET /api/prompts/catalog` (the sync test that
caught drift was retired in the dashboard-prompts-from-rest
migration — see test_dashboard_prompts_from_rest.py).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Optional

from ..core.registry import Registry, RegistryEntry, Visibility


_CATALOG_PATH = Path(__file__).resolve().parent / "catalog.json"

# Test-only override for the catalog path. When set, `load_catalog`
# returns the override instead of the shipped `catalog.json`. Used by
# tests that need to register a temporary admin-only prompt without
# mutating the on-disk catalog.
_CATALOG_OVERRIDE: Optional[Path] = None


@dataclass
class PromptEntry:
    """Per-prompt payload stored in the shared registry."""

    title: str
    description: str
    template: str
    variables: list
    category: str
    raw: Dict[str, Any]  # full catalog entry for back-compat consumers


class PromptRegistry(Registry[PromptEntry]):
    """Prompts subsystem adapter for the shared Registry.

    Adds `render(name, arguments)` as the prompts' verb. Visibility
    is honored — `render` raises PermissionError if the calling role
    cannot see the prompt (defense in depth; `prompts/list` filtering
    alone is not enough if a worker guesses an admin-only id).
    """

    def render(
        self,
        name: str,
        arguments: Dict[str, str],
        role: str = "admin",
    ) -> str:
        """Render the named prompt's template with `arguments`.

        Raises KeyError if the prompt is unknown or PermissionError
        if `role` may not see it. Missing variables substitute as
        the empty string (so no `{{VAR}}` leaks through).
        """
        from ..core.registry import resolve_visibility

        entry = self.get(name)
        if entry is None:
            raise KeyError(f"Unknown prompt id: {name}")
        if not resolve_visibility(entry.visibility, role):
            raise PermissionError(
                f"Prompt {name!r} is not visible to role {role!r}"
            )
        template = entry.meta.template
        declared = {v["name"] for v in entry.meta.variables}
        rendered = template
        for var_name in sorted(declared):
            value = str(arguments.get(var_name, ""))
            rendered = rendered.replace("{{" + var_name + "}}", value)
        return rendered


#: Singleton PromptRegistry consumed by the MCP handlers in
#: `app/main_app.py`. Populated lazily on first access from
#: `catalog.json`.
prompt_registry: PromptRegistry = PromptRegistry()


def load_catalog() -> Dict[str, Any]:
    """Load the prompt catalogue from disk.

    Returns the raw dict (not a deep copy) — callers MUST treat
    the structure as read-only. Wrap your access through
    `render_prompt` if you need a defensive copy.

    Test-only `_reload_catalog_for_tests(path)` swaps the active
    catalog file; the cache is cleared accordingly so the registry
    rebuild picks the new contents up.
    """
    path = _CATALOG_OVERRIDE or _CATALOG_PATH
    return _load_catalog_cached(path)


@lru_cache(maxsize=4)
def _load_catalog_cached(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
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

    Backwards-compat shim — new callers should use
    `prompt_registry.render(prompt_id, arguments, role=...)` to
    get visibility enforcement.
    """
    return prompt_registry.render(prompt_id, arguments)


def _build_registry_from_catalog() -> None:
    """Populate `prompt_registry` from the active catalog. Idempotent —
    safe to call after `_reload_catalog_for_tests` swaps the source.

    Special case: the ``event-loop`` prompt's template
    is sourced from the ``WAKE_LOOP_INSTRUCTIONS`` constant in
    ``agent_mcp.app.event_loop_instructions`` rather than the catalog's
    serialised copy. This keeps the prompt and the ``serverInfo.instructions``
    injection literally identical even if a future edit only updates the
    Python constant (so the JSON catalog can't go stale).
    """
    prompt_registry.clear()
    # Resolve the wake-loop text once per build — defensive import in
    # case the prompts package is consumed by tooling that doesn't
    # have the app module on its path (unlikely but cheap).
    try:
        from ..app.event_loop_instructions import WAKE_LOOP_INSTRUCTIONS
        _wake_loop_text: Optional[str] = WAKE_LOOP_INSTRUCTIONS.lstrip()
    except Exception:
        _wake_loop_text = None

    for raw in load_catalog().get("prompts", []):
        visibility: Visibility = raw.get("visibility", "any")
        if visibility not in ("any", "admin"):
            # Be conservative on an unknown sentinel — log via the
            # core resolver's warning path at filter time.
            visibility = "admin"
        template = raw.get("template", "")
        if (
            raw.get("id") == "event-loop"
            and _wake_loop_text is not None
        ):
            template = _wake_loop_text
        prompt_registry.register(
            RegistryEntry(
                name=raw["id"],
                visibility=visibility,
                meta=PromptEntry(
                    title=raw.get("title", raw["id"]),
                    description=raw.get("description", ""),
                    template=template,
                    variables=list(raw.get("variables", []) or []),
                    category=raw.get("category", ""),
                    raw=raw,
                ),
            )
        )


def _reload_catalog_for_tests(path: Optional[Path]) -> None:
    """Test helper: point `load_catalog()` at `path` (or restore the
    shipped catalog when None) and rebuild the registry.

    Production code MUST NOT call this — it exists so the unification
    tests in `tests/test_registry_unification.py` can verify
    visibility filtering with a temp catalog without touching the
    on-disk one.
    """
    global _CATALOG_OVERRIDE
    _CATALOG_OVERRIDE = path
    # The lru_cache is keyed on path so the new path will miss
    # naturally, but a previous override might have cached stale
    # contents — clear unconditionally.
    _load_catalog_cached.cache_clear()
    _build_registry_from_catalog()


# Build the registry once at import time from the shipped catalog.
_build_registry_from_catalog()
