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

# Messages-page-parity PR: the detail popup was extracted out of
# messages-dashboard.tsx into its own <ViewMessageModal> (parity with
# memories' <ViewMemoryModal>). The modal-content guards below read the
# modal file; the row-interaction guards still read the dashboard.
MODAL = "components/dashboard/modals/view-message-modal.tsx"


def _read(rel: str) -> str:
    return (DASHBOARD / rel).read_text()


def _read_modal() -> str:
    return _read(MODAL)


# ---------- Modal primitives ------------------------------------


def test_detail_popup_imports_dialog_primitives() -> None:
    src = _read_modal()
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
    # The row body must carry a click handler that opens the modal.
    #
    # Pre-scaffold the page hand-rolled `<TableRow onClick=…>`. After
    # the <DataTablePage> migration the row element is owned by the
    # shared <ResponsiveDataTable>, which attaches the handler the page
    # passes as `onRowClick` (and does so for BOTH the desktop row and
    # the mobile card — strictly more coverage than the old inline
    # TableRow). Accept either spelling; the invariant is that the
    # row-body click opens the detail dialog.
    assert "<TableRow" in src or "onRowClick" in src, (
        "expected a row-body click handler — either an inline "
        "<TableRow onClick> or an onRowClick passed to the shared "
        "<DataTablePage> / <ResponsiveDataTable>"
    )
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
    src = _read_modal()
    # Full content must be rendered in a pre-wrap / monospace block —
    # not truncated. We accept whitespace-pre-wrap (Tailwind) as the
    # marker; the row table cell still uses `truncate`.
    assert "whitespace-pre-wrap" in src, (
        "expected the detail modal to render message_content with "
        "whitespace-pre-wrap so newlines are preserved"
    )


def test_detail_popup_renders_all_fields() -> None:
    src = _read_modal()
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
    src = _read_modal()
    # The single button toggles between "Mark read" and "Mark unread"
    # based on the current row's read flag — both strings must appear
    # so either branch can render. Mark-read stays inline (the modal
    # stays open — live-lookup re-renders it with the fresh row).
    assert "Mark read" in src and "Mark unread" in src, (
        "expected the detail modal footer to expose a Mark read / "
        "Mark unread toggle button"
    )


def test_detail_popup_delete_routes_through_confirm() -> None:
    # Messages-page-parity PR: the modal Delete no longer fires an
    # unconfirmed DELETE. It routes through a type-DELETE-to-confirm
    # dialog — closing the real no-confirm gap the audit flagged.
    #
    # Scaffold migration: that dialog is now the unified
    # <DeleteConfirmModal> (delete-message-modal.tsx was subsumed, the
    # same way delete-memory-modal.tsx was by PR #581) — single-row
    # delete passes the message preview as `details`, bulk delete
    # overrides title/description/warning with the count-aware copy.
    import re

    modal = _read_modal()
    assert re.search(r"\bDelete\b\s*</Button>", modal), (
        "expected a Delete button in the detail modal footer"
    )
    assert "onDelete" in modal, (
        "expected the modal Delete button to defer to the parent via "
        "an onDelete prop (parent opens the confirm dialog)"
    )
    dash = _read("components/dashboard/messages-dashboard.tsx")
    assert (
        "DeleteConfirmModal" in dash or "DeleteMessageModal" in dash
    ) and "deleteDialog.open" in dash, (
        "expected the dashboard to route deletes through the shared "
        "type-DELETE-to-confirm dialog (no unconfirmed delete)"
    )


def test_detail_popup_has_close_button() -> None:
    src = _read_modal()
    # "Close" footer button is explicit (in addition to the Dialog's
    # built-in X close affordance). Tolerate whitespace between the
    # opening Button tag and the literal text.
    import re

    assert re.search(r"\bClose\b\s*</Button>", src), (
        "expected an explicit Close button in the modal footer"
    )
