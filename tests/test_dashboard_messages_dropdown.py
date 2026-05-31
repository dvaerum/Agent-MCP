"""Regression guards for the Messages tab Compose recipient dropdown.

The original PR #21 shipped a free-text Input for the recipient. Dennis
asked for a dropdown of existing agents (with admin pinned at the top)
so admins don't have to type agent_id by hand. This test parses the
.tsx so we catch silent regressions without needing jsdom.

Also asserts the Messages tab uses POST /api/messages/query for
listing — the original GET-with-body call failed in the browser
because the Fetch spec strips bodies from GET requests.
"""

from __future__ import annotations

from pathlib import Path

DASHBOARD = Path("agent_mcp/dashboard")


def _read(rel: str) -> str:
    return (DASHBOARD / rel).read_text()


def test_recipient_field_uses_select_not_input() -> None:
    src = _read("components/dashboard/messages-dashboard.tsx")
    # The "Recipient" label must be followed by a <Select>, not <Input>,
    # before the next label/section starts. Find the JSX label (not the
    # state variable name) by anchoring on the Label tag.
    idx = src.find("Recipient agent_id")
    assert idx > 0, "Recipient label not found in messages-dashboard.tsx"
    # Look at the ~600 chars after the label — covers the JSX block.
    nearby = src[idx : idx + 600]
    assert "<Select" in nearby, (
        "expected the Recipient field to be a <Select> dropdown, "
        f"but no <Select> appeared near the label:\n{nearby!r}"
    )
    assert "<Input" not in nearby, (
        "expected the Recipient field to be a <Select> dropdown — "
        f"<Input> still present near the label:\n{nearby!r}"
    )


def test_recipient_dropdown_populated_from_participants_endpoint() -> None:
    """Originally asserted apiClient.getAgents() was the dropdown source.

    Dennis flagged ghost agents — /api/agents returns every row
    including status='terminated' — so the source changed to
    /api/messages/participants, which returns {live, tombstones}
    (terminated agents excluded). The Compose recipient renders the
    `live` list only; the From/To filters render live + tombstones.
    """
    src = _read("components/dashboard/messages-dashboard.tsx")
    assert "/participants" in src or "/messages/participants" in src, (
        "expected the Compose form to populate recipient options from "
        "the /api/messages/participants endpoint (live agents only)"
    )


def test_recipient_dropdown_includes_admin() -> None:
    src = _read("components/dashboard/messages-dashboard.tsx")
    # "admin" must be present as a string literal (hardcoded option,
    # because /api/agents may or may not include the admin agent).
    assert '"admin"' in src or "'admin'" in src, (
        "expected admin to be a hardcoded recipient option"
    )


def test_listing_uses_post_query_not_get() -> None:
    src = _read("components/dashboard/messages-dashboard.tsx")
    # The previous bug was a GET with a JSON body — browsers strip
    # GET bodies per the Fetch spec, so the listing silently failed.
    assert '"POST", "/query"' in src or "'POST', '/query'" in src, (
        "expected listing to call POST /api/messages/query (POST + /query "
        "suffix in callMessages)"
    )
    # And the original buggy call must be gone.
    assert '"GET", ""' not in src and "'GET', ''" not in src, (
        "expected the GET-with-empty-suffix call to be removed; it was "
        "the original bug (browsers strip GET bodies)"
    )


# ---------- Filter dropdowns (from / to) -----------------------
# The original Filters card used <Input> text boxes for "from" and "to".
# Dennis asked for dropdowns populated from /api/agents so admins can
# pick known sender/recipient ids without typing.


def test_from_filter_uses_select_not_input() -> None:
    src = _read("components/dashboard/messages-dashboard.tsx")
    # Free-text Input for from/to must be gone.
    assert 'placeholder="from (sender_id)"' not in src, (
        "from-filter still uses an <Input> with the old placeholder"
    )
    # The replacement Select must reference the filters.from state.
    assert "filters.from" in src and "filters.to" in src, (
        "expected the from/to filter state names to remain"
    )


def test_to_filter_uses_select_not_input() -> None:
    src = _read("components/dashboard/messages-dashboard.tsx")
    assert 'placeholder="to (recipient_id)"' not in src, (
        "to-filter still uses an <Input> with the old placeholder"
    )


def test_filter_dropdowns_have_any_sender_recipient_options() -> None:
    src = _read("components/dashboard/messages-dashboard.tsx")
    # Must include an "any sender" / "any recipient" sentinel option so
    # the dropdowns can clear the filter — same __all pattern used for
    # type/priority/read filters.
    assert "any sender" in src.lower(), (
        "expected from filter to expose an 'any sender' option"
    )
    assert "any recipient" in src.lower(), (
        "expected to filter to expose an 'any recipient' option"
    )


# ---------- Compose: broadcast option --------------------------


def test_compose_recipient_includes_broadcast_option() -> None:
    src = _read("components/dashboard/messages-dashboard.tsx")
    # Broadcast is wired via recipient_id="*" — the API sentinel.
    assert '"*"' in src or "'*'" in src, (
        "expected a recipient_id='*' broadcast option in Compose"
    )
    assert "broadcast" in src.lower(), (
        "expected the Compose recipient dropdown to mention broadcast"
    )


# ---------- Bulk selection toolbar -----------------------------


def test_table_has_select_all_checkbox() -> None:
    src = _read("components/dashboard/messages-dashboard.tsx")
    # Header checkbox toggles every currently-rendered (filtered) row.
    # We're text-parsing, so look for the structural marker rather than
    # the visual one.
    assert "selectAllVisible" in src or "toggleAllVisible" in src, (
        "expected a select-all-visible handler in the table header"
    )


def test_table_has_per_row_checkbox() -> None:
    src = _read("components/dashboard/messages-dashboard.tsx")
    assert "selectedIds" in src, (
        "expected selectedIds state to track per-row checkbox selection"
    )


def test_bulk_actions_toolbar_present_when_selected() -> None:
    src = _read("components/dashboard/messages-dashboard.tsx")
    # Toolbar buttons: mark read / mark unread / delete.
    assert "Mark read" in src, "missing 'Mark read' bulk action button"
    assert "Mark unread" in src, "missing 'Mark unread' bulk action button"
    assert "Delete" in src, "missing 'Delete' bulk action button"


def test_bulk_delete_calls_delete_endpoint() -> None:
    src = _read("components/dashboard/messages-dashboard.tsx")
    # callMessages must support the new DELETE verb.
    assert '"DELETE"' in src or "'DELETE'" in src, (
        "expected the component to issue DELETE /api/messages/<id> for "
        "bulk + row-level delete"
    )


def test_row_has_inline_delete_button() -> None:
    src = _read("components/dashboard/messages-dashboard.tsx")
    # Lucide Trash2 icon is the convention used elsewhere in the
    # dashboard for delete affordances.
    assert "Trash2" in src or "Trash " in src or 'icon="trash"' in src, (
        "expected a Trash2 icon import for the per-row delete button"
    )


# ---------- Filter dropdown participant source ------------------
# Original bug: From/To dropdowns sourced from apiClient.getAgents(),
# which returns EVERY agent including status='terminated'. The
# replacement is a new /api/messages/participants endpoint that returns
# live agents only + DISTINCT tombstone strings (sender_id /
# recipient_id beginning with ``[deleted-``). The Compose recipient
# stays live-only (you cannot message a deleted agent).


def test_filter_dropdowns_call_participants_endpoint() -> None:
    src = _read("components/dashboard/messages-dashboard.tsx")
    # The filter dropdowns must be populated from /messages/participants
    # (admin-token POST), not from apiClient.getAgents(), so terminated
    # agents are excluded.
    assert "/messages/participants" in src or "/participants" in src, (
        "expected the Messages tab to call the new "
        "/api/messages/participants endpoint to populate the "
        "Sender/Recipient filter dropdowns (live agents + tombstones)"
    )


def test_filter_dropdowns_exclude_terminated_agents() -> None:
    src = _read("components/dashboard/messages-dashboard.tsx")
    # Defensive client-side filter even if a stale getAgents() call
    # remains; the symbolic check is that the terminated status is
    # explicitly excluded somewhere in the participant pipeline.
    # The new flow drops getAgents() entirely from the *filter* dropdown
    # source, so the only call to apiClient.getAgents() that may remain
    # is the Compose recipient list. Either path must not include
    # terminated agents in the From/To dropdowns.
    if "apiClient.getAgents()" in src:
        # If getAgents is still used (e.g., to feed the Compose
        # recipient), there must be an explicit terminated filter.
        assert "terminated" in src, (
            "Compose recipient still uses apiClient.getAgents(); must "
            "filter status='terminated' out before rendering options"
        )


def test_filter_dropdowns_render_tombstone_values() -> None:
    src = _read("components/dashboard/messages-dashboard.tsx")
    # The participants endpoint returns {live, tombstones}. The From/To
    # dropdowns must render BOTH so admins can grep history for purged
    # agents. Look for evidence of the tombstones key being read.
    assert "tombstones" in src, (
        "expected the Messages tab to read the `tombstones` array from "
        "the participants endpoint and render those values in the "
        "Sender/Recipient filter dropdowns"
    )


def test_compose_recipient_excludes_tombstones() -> None:
    src = _read("components/dashboard/messages-dashboard.tsx")
    # Compose recipient must remain live-only — you cannot message a
    # deleted agent. The simplest evidence is that the compose flow
    # does not iterate `tombstones` into its <SelectItem>s.
    # Find the Recipient label and inspect ~1200 chars after.
    idx = src.find("Recipient agent_id")
    assert idx > 0, "Recipient label not found"
    block = src[idx : idx + 1200]
    assert "tombstones" not in block, (
        "Compose recipient block should not render tombstone agent ids "
        "(you cannot message a deleted agent); render live agents only"
    )
