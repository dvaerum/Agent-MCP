"""Regression guard: dashboard fetches prompt catalog from the REST API.

The dashboard used to inline the prompt catalog as a 470-line
TypeScript array in `lib/prompt-book.ts`. PR #67 added the JSON
source of truth at `agent_mcp/prompts/catalog.json` and exposed it
via `GET /api/prompts/catalog`; an interim drift-detection test
(`test_typescript_and_json_catalogs_in_sync`) ensured the two
catalogues stayed aligned until this migration happened.

This test pins the post-migration shape:

* `prompt-book.ts` no longer carries an inlined `export const
  promptTemplates` array. The data comes from the REST endpoint via
  the zustand `useDataStore` slice instead.

* `data-store.ts` carries a `promptsCatalog` state field plus a
  `fetchPromptsCatalog` action so the rest of the dashboard reads
  the catalogue the same way it reads agents / tasks / context.

* `prompt-book-dashboard.tsx` no longer imports `promptTemplates`
  directly — it pulls the catalogue from the store.

* The notification listener (`lib/api.ts` or a sibling file) calls
  `invalidatePromptsCatalog` when an MCP `notifications/prompts/list_changed`
  arrives so other dashboard tabs see an admin-created custom prompt
  within seconds rather than on the next manual reload.

Python-side parse so the test stays dependency-free (no Node runtime
needed in CI) and surfaces drift the same way every other
test_dashboard_* file in this suite does.
"""

from __future__ import annotations

import re
from pathlib import Path

_DASHBOARD = Path(__file__).resolve().parents[1] / "agent_mcp" / "dashboard"


def _read(rel: str) -> str:
    return (_DASHBOARD / rel).read_text(encoding="utf-8")


def test_prompt_book_no_longer_inlines_prompttemplates() -> None:
    """`lib/prompt-book.ts` must not declare `export const promptTemplates`
    as a literal array. The catalogue comes from the store now."""
    src = _read("lib/prompt-book.ts")
    inlined = re.search(
        r"export\s+const\s+promptTemplates\s*:\s*PromptTemplate\[\]\s*=\s*\[",
        src,
    )
    assert inlined is None, (
        "lib/prompt-book.ts still has an inlined `export const "
        "promptTemplates: PromptTemplate[] = [...]` literal — the migration "
        "to fetch from /api/prompts/catalog should have removed it. "
        "Replace consumers with `useDataStore(s => s.promptsCatalog)`."
    )


def test_data_store_exposes_promptscatalog_slice() -> None:
    """`lib/stores/data-store.ts` declares the promptsCatalog state field
    and the fetchPromptsCatalog action."""
    src = _read("lib/stores/data-store.ts")
    assert re.search(r"\bpromptsCatalog\b", src), (
        "data-store.ts has no `promptsCatalog` field — add the slice "
        "alongside the existing agents/tasks slices."
    )
    assert re.search(r"\bfetchPromptsCatalog\b", src), (
        "data-store.ts has no `fetchPromptsCatalog` action — add an "
        "action that calls apiClient.getPromptsCatalog() and populates "
        "the slice."
    )
    assert re.search(r"\binvalidatePromptsCatalog\b", src), (
        "data-store.ts has no `invalidatePromptsCatalog` action — the "
        "notification listener needs a way to force-refresh."
    )


def test_prompt_book_dashboard_reads_from_store() -> None:
    """`prompt-book-dashboard.tsx` reads the catalogue from the zustand
    store, not from a direct `import { promptTemplates }` of the
    legacy inlined data."""
    src = _read("components/dashboard/prompt-book-dashboard.tsx")
    assert "useDataStore" in src, (
        "prompt-book-dashboard.tsx must read promptsCatalog from "
        "useDataStore (the zustand slice) — current imports still point "
        "at the inlined `promptTemplates` array."
    )
    # The legacy named import of `promptTemplates` from `@/lib/prompt-book`
    # must be gone. (A named import of `PromptTemplate` — the *type* — is
    # fine; we're guarding against the *value* import.)
    bad_imports = re.search(
        r"import\s*\{[^}]*\bpromptTemplates\b[^}]*\}\s*from\s*['\"]@/lib/prompt-book['\"]",
        src,
    )
    assert bad_imports is None, (
        "prompt-book-dashboard.tsx still imports `promptTemplates` from "
        "@/lib/prompt-book — pull from `useDataStore(s => s.promptsCatalog)` "
        "instead."
    )


def test_api_client_exposes_get_prompts_catalog() -> None:
    """`lib/api.ts` exposes a `getPromptsCatalog()` method on the
    apiClient so the store can fetch without each call site
    constructing the URL itself."""
    src = _read("lib/api/system.ts")
    assert re.search(r"\bgetPromptsCatalog\b", src), (
        "lib/api/system.ts has no getPromptsCatalog method — add it "
        "alongside getAllData / getTokens etc."
    )


def test_notification_listener_invalidates_prompts_catalog() -> None:
    """Somewhere in `lib/` the dashboard listens for
    `notifications/prompts/list_changed` and triggers
    `invalidatePromptsCatalog` so other tabs see admin-created
    prompts within seconds."""
    candidates = [
        "lib/mcp-notifications.ts",
        "lib/stores/data-store.ts",
    ]
    combined = "\n".join(_read(p) for p in candidates)
    assert "prompts/list_changed" in combined or "promptsListChanged" in combined, (
        "No reference to MCP `notifications/prompts/list_changed` "
        "found in lib/api.ts or lib/stores/data-store.ts — wire a "
        "listener that calls invalidatePromptsCatalog so dashboards "
        "in other tabs see admin-created prompts in real-time."
    )
    assert "invalidatePromptsCatalog" in combined, (
        "Listener doesn't call invalidatePromptsCatalog after a "
        "prompts/list_changed notification — without invalidation, "
        "subsequent reads return the stale cached value."
    )
