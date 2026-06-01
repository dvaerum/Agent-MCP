"""Regression guards for the Messages tab row-click detail popup.

Today rows are truncated and the only way to see a full message is to
query the SQLite DB directly. This PR adds a click-on-the-row → modal
that shows every field of the message in a readable layout, plus
inline actions (Mark read/unread, Delete, Close).

Text-parse regression guards (same convention as
test_dashboard_messages_tab.py / _dropdown.py); we don't have jsdom
infrastructure and behavior is verified by `npm run build` plus
manual click-through in the live dashboard.
"""

from __future__ import annotations

from pathlib import Path

DASHBOARD = Path("agent_mcp/dashboard")


def _read(rel: str) -> str:
    return (DASHBOARD / rel).read_text()


# ---------- Modal primitives ------------------------------------


def test_detail_popup_imports_dialog_primitives() -> None:
    src = _read("components/dashboard/messages-dashboard.tsx")
    # We reuse the existing shadcn Dialog (already in
    # components/ui/dialog.tsx) so we don't add a new modal stack.
    for name in (
        "Dialog",
        "DialogContent",
        "DialogHeader",
        "DialogFooter",
        "DialogTitle",
    ):
        assert name in src, f"expected Dialog primitive '{name}' to be imported"
    assert '@/components/ui/dialog' in src, (
        "expected the import to come from @/components/ui/dialog"
    )


# ---------- Row-click opens the modal ---------------------------


def test_row_has_click_handler_opening_detail() -> None:
    src = _read("components/dashboard/messages-dashboard.tsx")
    # State that drives the detail modal. After the useDialog<T>()
    # migration (Candidate F1) the state lives on a hook named
    # detailDialog rather than a useState pair, but the substring
    # "detail" is still present as the hook name.
    assert "detailDialog" in src, (
        "expected detailDialog (useDialog<Message>()) to back the modal"
    )
    # The TableRow itself must carry an onClick that opens the modal
    # — that's how clicking on the row content area opens it.
    assert "<TableRow" in src
    # The handler must reference the hook's .open(...) method (the
    # post-migration replacement for setDetailMessage).
    assert "detailDialog.open" in src, (
        "expected detailDialog.open(m) to be wired onto the row's onClick"
    )


def test_checkbox_cell_stops_propagation() -> None:
    """Clicking the checkbox column must NOT open the modal."""
    src = _read("components/dashboard/messages-dashboard.tsx")
    # The existing checkbox + per-row delete cells already wrap their
    # onClick with stopPropagation; this PR keeps that contract so the
    # bulk-select / per-row delete behaviour is preserved.
    assert "stopPropagation" in src, (
        "expected stopPropagation on the checkbox / action cells so "
        "they don't bubble up and open the detail modal"
    )


# ---------- Modal content ---------------------------------------


def test_detail_popup_shows_full_content_block() -> None:
    src = _read("components/dashboard/messages-dashboard.tsx")
    # Full content must be rendered in a pre-wrap / monospace block —
    # not truncated. We accept whitespace-pre-wrap (Tailwind) as the
    # marker; the row table cell still uses `truncate`.
    assert "whitespace-pre-wrap" in src, (
        "expected the detail modal to render message_content with "
        "whitespace-pre-wrap so newlines are preserved"
    )


def test_detail_popup_renders_all_fields() -> None:
    src = _read("components/dashboard/messages-dashboard.tsx")
    # The modal labels every field — these are user-facing strings.
    for label in (
        "Message ID",
        "Sender",
        "Recipient",
        "Type",
        "Priority",
        "Delivered",
        "Read",
        "Content",
    ):
        assert label in src, f"expected the detail modal to label '{label}'"


# ---------- Modal actions ---------------------------------------


def test_detail_popup_has_mark_read_toggle() -> None:
    src = _read("components/dashboard/messages-dashboard.tsx")
    # The single button toggles between "Mark read" and "Mark unread"
    # based on the current row's read flag — both strings must appear
    # so either branch can render.
    assert "Mark read" in src and "Mark unread" in src, (
        "expected the detail modal footer to expose a Mark read / "
        "Mark unread toggle button (also reused by bulk actions)"
    )


def test_detail_popup_has_delete_button() -> None:
    src = _read("components/dashboard/messages-dashboard.tsx")
    # Delete from the modal calls the same DELETE endpoint and then
    # closes the modal + refreshes the list.
    assert "deleteOne" in src, (
        "expected the modal Delete button to call deleteOne (the "
        "existing per-row delete helper)"
    )


def test_detail_popup_has_close_button() -> None:
    src = _read("components/dashboard/messages-dashboard.tsx")
    # "Close" footer button is explicit (in addition to the Dialog's
    # built-in X close affordance). Tolerate whitespace between the
    # opening Button tag and the literal text.
    import re

    assert re.search(r"\bClose\b\s*</Button>", src), (
        "expected an explicit Close button in the modal footer"
    )
