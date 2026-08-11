"""Regression guards for the dashboard's API-error → toast surfacing.

Bug surfaced by Firefox-MCP click-through on 2026-06-17 against v5.0.47
(commit ``0ea1858``):

  1. User opens Agents tab → clicks Deploy.
  2. User fills ``agent_id="BadName!@#"`` (invalid per PR #163's
     server-side regex).
  3. User clicks Deploy.
  4. Server correctly returns 400 with
     ``{"message": "Error: invalid agent_id 'BadName!@#': must match
     ^[a-z][a-z0-9-]*[a-z0-9]$|^[a-z]$ ..."}``.
  5. Browser console logs 3 errors, the dialog **silently closes**,
     the Agent Fleet table renders unchanged.
  6. User sees nothing — no toast, no inline error, just disappearance.

The server's validation is correct; the dashboard simply swallowed the
response. Two failures stack on top of each other in the client:

  * ``api.ts`` ``ApiClient.request`` only used the response status line
    (``API Error: 400 Bad Request``); it discarded the parsed
    ``message`` field from the JSON body, so every caller's
    ``error.message`` was a generic "Bad Request" with no useful text.
  * ``handleCreateAgent`` (and similar mutation handlers across
    agents-dashboard / tasks-dashboard / memories-dashboard /
    prompt-book-dashboard / create-memory-modal / create-prompt-modal /
    edit-memory-modal / delete-memory-modal) caught the error and
    called ``console.error`` only; ``CreateAgentModal``'s submit
    handler then immediately closed the dialog & reset the form,
    losing the user's input.

This module pins the contract for the fix:

  * The shared ``ApiError`` class carries ``status``, ``message`` (the
    server's message verbatim), and ``body`` (the raw response text)
    so every caller has the same surface.
  * ``api.ts::ApiClient.request`` parses the JSON body on !ok responses
    and prefers ``body.message`` over the status line.
  * A shared toast primitive lives at
    ``agent_mcp/dashboard/components/ui/toast.tsx`` and is mounted via
    ``<Toaster />`` in ``app/layout.tsx`` so any module can
    ``toastError(err)`` without per-page wiring.
  * Mutation handlers in agents-dashboard.tsx call ``toastError`` on
    catch instead of the silent ``console.error`` pattern.
  * The Deploy modal awaits the create call and only closes / resets
    on success — on error the dialog stays open with the user's input
    intact.

The grep-style file inspection pattern matches
``test_dashboard_create_agent_endpoint.py`` / ``test_dashboard_no_auto_cleanup``
(no jsdom in this repo; behaviour verified via ``npm run build`` plus
Firefox MCP e2e).
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD = REPO_ROOT / "agent_mcp" / "dashboard"
API_FILE = DASHBOARD / "lib" / "api" / "client.ts"
AGENTS_TSX = DASHBOARD / "components" / "dashboard" / "agents-dashboard.tsx"
LAYOUT_TSX = DASHBOARD / "app" / "layout.tsx"
TOAST_TSX = DASHBOARD / "components" / "ui" / "toast.tsx"


def _read(p: Path) -> str:
    # The Agents page is a page module + a directory of satellites since
    # the <DataTablePage> migration (RegisterAgentModal now lives in
    # components/dashboard/agents/register-agent-modal.tsx); guards
    # about "the Agents page" read all of it.
    if p == AGENTS_TSX:
        from tests.dashboard_sources import agents_page_source

        return agents_page_source()
    return p.read_text(encoding="utf-8")


# ---------- ApiError class on the api.ts seam ---------------------------


def test_api_ts_exports_api_error_class() -> None:
    """``api.ts`` must export a named ``ApiError`` class so callers and
    the toast helper can ``instanceof``-check it (and so future TS
    consumers can ``import { ApiError } from '@/lib/api'``)."""
    src = _read(API_FILE)
    assert re.search(r"export\s+class\s+ApiError\b", src), (
        "api.ts must declare and export a named ``ApiError`` class so "
        "mutation handlers can distinguish API errors from generic "
        "network / abort failures"
    )


def test_api_error_carries_status_and_body_fields() -> None:
    """``ApiError`` must carry ``status`` (HTTP code) and ``body`` (raw
    response text) in addition to the standard Error ``message`` —
    callers and tests need both to format meaningful toasts."""
    src = _read(API_FILE)
    # Find the ApiError block (greedy until next class / top-level
    # export to keep the slice bounded).
    m = re.search(
        r"export\s+class\s+ApiError\b[^{]*\{(?P<body>.*?)\n\}\n",
        src,
        re.DOTALL,
    )
    assert m, "Could not locate ApiError class body in api.ts"
    body = m.group("body")
    assert "status" in body, "ApiError must expose a ``status`` field"
    assert "body" in body, (
        "ApiError must expose a ``body`` field (raw response text) so "
        "callers can inspect / log the full server response"
    )


# ---------- Request layer surfaces the server's message ----------------


def test_request_layer_parses_server_message_from_body() -> None:
    """``ApiClient.request`` must attempt to parse the response body as
    JSON on !ok responses and prefer ``body.message`` over the bare
    HTTP status line — otherwise the server's carefully-worded 400
    text (PR #163) never reaches the UI."""
    src = _read(API_FILE)
    # The fix must (1) JSON.parse the errorText and (2) use a
    # ``.message`` field off the parsed body. Match both signals.
    assert "JSON.parse" in src, (
        "api.ts must JSON.parse error response bodies so the server's "
        "{message: ...} payload can be surfaced to the UI"
    )
    assert re.search(r"\.message\b", src), (
        "api.ts must read the parsed body's ``message`` field"
    )


def test_request_layer_throws_api_error_not_generic_error() -> None:
    """On a !ok response, ``ApiClient.request`` must ``throw new
    ApiError(...)`` carrying the status + parsed message, not the
    generic ``throw new Error('API Error: 400 Bad Request')`` that
    silently dropped the body pre-fix."""
    src = _read(API_FILE)
    assert re.search(r"throw\s+new\s+ApiError\b", src), (
        "api.ts request layer must ``throw new ApiError(...)`` on !ok "
        "responses (was ``throw new Error('API Error: ...')`` which "
        "discarded the server message)"
    )


# ---------- Toast primitive lives at the shared seam -------------------


def test_toast_primitive_file_exists() -> None:
    """A shared toast primitive must live at
    ``components/ui/toast.tsx`` so every dashboard tab imports the
    same component (matches the shadcn convention used by the rest of
    components/ui/)."""
    assert TOAST_TSX.is_file(), (
        f"Missing shared toast primitive at {TOAST_TSX.relative_to(REPO_ROOT)} "
        "— mutation handlers need a single seam to render API errors"
    )


def test_toast_module_exports_toaster_and_helpers() -> None:
    """The toast module must export a ``Toaster`` component (mounted
    in layout.tsx) and a ``toastError(err)`` helper so callers don't
    need to know the toast store internals."""
    src = _read(TOAST_TSX)
    assert re.search(r"export\s+(function|const)\s+Toaster\b", src), (
        "toast.tsx must export a ``Toaster`` portal component"
    )
    assert re.search(r"export\s+(function|const)\s+toastError\b", src), (
        "toast.tsx must export a ``toastError`` helper that accepts "
        "an ApiError (or any Error) and surfaces it to the user"
    )


def test_layout_mounts_toaster() -> None:
    """``app/layout.tsx`` must mount the ``<Toaster />`` portal so the
    toast container exists in the React tree from the first render."""
    src = _read(LAYOUT_TSX)
    assert "Toaster" in src, (
        "app/layout.tsx must mount <Toaster /> so toasts surface "
        "across every dashboard tab"
    )


# ---------- agents-dashboard Deploy handler ----------------------------


def test_create_agent_handler_uses_toast_error() -> None:
    """The Deploy submit handler in ``agents-dashboard.tsx`` must call
    ``toastError`` on catch — silent ``console.error`` is exactly the
    bug the 2026-06-17 Firefox-MCP click-through caught."""
    src = _read(AGENTS_TSX)
    assert "toastError" in src, (
        "agents-dashboard.tsx must import and call ``toastError`` "
        "(was silent ``console.error`` only — server message never "
        "reached the user)"
    )


def test_register_agent_modal_keeps_dialog_open_on_error() -> None:
    """``RegisterAgentModal.handleSubmit`` (Wave 7 PR 3 made it the
    sole agent-creation surface; the legacy ``CreateAgentModal`` is
    gone) must await its submit and only call ``setOpen(false)``
    after a successful resolution — on error the dialog stays open
    so the user doesn't lose their typed input.
    """
    src = _read(AGENTS_TSX)
    # Locate the RegisterAgentModal block.
    m = re.search(
        r"const\s+RegisterAgentModal\s*=.*?\n\}\n", src, re.DOTALL,
    )
    assert m, "Could not locate RegisterAgentModal in agents-dashboard.tsx"
    modal = m.group(0)
    # The submit handler must be async so it can await the API call.
    assert re.search(r"const\s+handleSubmit\s*=\s*async\b", modal), (
        "RegisterAgentModal.handleSubmit must be async so it can await "
        "the register call and only close on success"
    )
    # The submit handler must await the apiClient.registerAgent call
    # (otherwise the success-pane render races the request).
    assert re.search(r"await\s+apiClient\.registerAgent\b", modal), (
        "RegisterAgentModal.handleSubmit must ``await "
        "apiClient.registerAgent(...)`` so dialog state can react to "
        "success vs failure"
    )
