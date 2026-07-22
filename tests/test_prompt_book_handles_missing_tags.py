"""Regression guard: Prompt Book renders prompts that lack a ``tags`` key.

On 2026-06-17 a Firefox-MCP click-through on the Prompt Book tab
surfaced ``TypeError: s.tags is undefined`` inside
``fetchPromptsCatalog``'s consumer code. Root cause: one of the 12
shipped prompts in ``agent_mcp/prompts/catalog.json``
(``event-loop``) lacked the ``tags`` key entirely
while the dashboard read-sites dereferenced ``prompt.tags`` directly.

The fix is three layers of defense:

1. **Backfill** the missing ``tags: []`` in ``catalog.json`` (immediate).
2. **Normalize at fetch** in ``data-store.ts::fetchPromptsCatalog`` so
   anything that flows through the zustand slice always carries a
   ``tags`` array even if the catalog drifts again.
3. **Defensive ``?? []``** on every read site in
   ``prompt-book-dashboard.tsx`` (belt + suspenders if the store is
   ever bypassed) and ``prompt-book.ts::searchPrompts``.

Python-side parse so the test stays dependency-free (no Node runtime
needed in CI) and surfaces drift the same way every other
``test_dashboard_*`` file in this suite does.
"""

from __future__ import annotations

import json
import re
from pathlib import Path


_REPO = Path(__file__).resolve().parents[1]
_DASHBOARD = _REPO / "agent_mcp" / "dashboard"
_CATALOG = _REPO / "agent_mcp" / "prompts" / "catalog.json"


def _read(rel: str) -> str:
    return (_DASHBOARD / rel).read_text(encoding="utf-8")


def test_catalog_every_prompt_has_tags_key() -> None:
    """Layer 1: every entry in ``catalog.json`` must carry a ``tags``
    key (even if the value is an empty list). Without this, the
    dashboard's direct dereference of ``prompt.tags`` throws
    ``TypeError: s.tags is undefined`` the moment the catalogue is
    rendered.
    """
    catalog = json.loads(_CATALOG.read_text(encoding="utf-8"))
    missing = [p.get("id", "<no-id>") for p in catalog["prompts"] if "tags" not in p]
    assert missing == [], (
        "agent_mcp/prompts/catalog.json has prompts without a `tags` "
        f"key: {missing}. Every entry must have `\"tags\": [...]` "
        "(possibly empty) so the dashboard can render without "
        "tripping `TypeError: s.tags is undefined`."
    )


def test_catalog_tags_is_always_a_list() -> None:
    """And the value must actually be a JSON array — a string or
    null would still trip the dashboard's ``.slice`` / ``.some``
    calls."""
    catalog = json.loads(_CATALOG.read_text(encoding="utf-8"))
    bad = [
        (p.get("id", "<no-id>"), type(p.get("tags")).__name__)
        for p in catalog["prompts"]
        if not isinstance(p.get("tags"), list)
    ]
    assert bad == [], (
        "agent_mcp/prompts/catalog.json has prompts whose `tags` is "
        f"not a JSON array: {bad}."
    )


def test_data_store_normalizes_prompt_tags_at_fetch() -> None:
    """Layer 2: ``fetchPromptsCatalog`` in the zustand store must map
    each prompt to ``{...p, tags: p.tags ?? []}`` so any drift in the
    JSON catalogue is healed before it reaches the React tree."""
    src = _read("lib/stores/data-store.ts")
    # Normalize whitespace so the assertion isn't brittle to
    # formatting choices.
    flat = re.sub(r"\s+", " ", src)
    assert "tags: p.tags ?? []" in flat, (
        "lib/stores/data-store.ts::fetchPromptsCatalog should map "
        "each fetched prompt to `{ ...p, tags: p.tags ?? [] }` so "
        "downstream consumers never see `undefined.tags`. This is "
        "the layer-2 defense — even if catalog.json drifts again, "
        "the store heals it."
    )


def test_prompt_book_dashboard_uses_defensive_tags_reads() -> None:
    """Layer 3: ``prompt-book-dashboard.tsx`` must guard every
    ``prompt.tags`` / ``p.tags`` dereference with ``?? []`` so the
    component renders even if the store is bypassed (e.g. by future
    direct catalog imports or test harnesses)."""
    src = _read("components/dashboard/prompt-book-dashboard.tsx")
    # The unguarded patterns we are explicitly forbidding.
    forbidden = [
        r"\bprompt\.tags\.slice\b",
        r"\bprompt\.tags\.length\b",
        r"\bp\.tags\.some\b",
    ]
    for pattern in forbidden:
        assert not re.search(pattern, src), (
            f"prompt-book-dashboard.tsx still has an unguarded "
            f"dereference matching `{pattern}`. Wrap the access in "
            "`(prompt.tags ?? []).…` / `(p.tags ?? []).…` so a prompt "
            "without `tags` doesn't throw `TypeError: s.tags is "
            "undefined`."
        )
    # And the defensive form must actually be present (catches the
    # case where someone deletes the dereference outright and breaks
    # the UI a different way).
    assert "(prompt.tags ?? [])" in src, (
        "prompt-book-dashboard.tsx should use `(prompt.tags ?? [])` "
        "at the card-render sites."
    )
    assert "(p.tags ?? [])" in src, (
        "prompt-book-dashboard.tsx should use `(p.tags ?? [])` in "
        "the search-filter site."
    )


def test_prompt_book_search_guards_tags() -> None:
    """Layer 3b: ``searchPrompts`` in ``lib/prompt-book.ts`` is the
    other consumer of ``prompt.tags`` and gets the same guard so a
    bypass of the store (or a future caller passing a raw catalog
    array) still renders cleanly."""
    src = _read("lib/prompt-book.ts")
    assert not re.search(r"\bprompt\.tags\.some\b", src), (
        "lib/prompt-book.ts::searchPrompts still dereferences "
        "`prompt.tags.some` without a guard. Use "
        "`(prompt.tags ?? []).some(…)` so a tags-less prompt doesn't "
        "throw."
    )
    assert "(prompt.tags ?? [])" in src, (
        "lib/prompt-book.ts should use `(prompt.tags ?? []).some(…)` "
        "in searchPrompts."
    )
