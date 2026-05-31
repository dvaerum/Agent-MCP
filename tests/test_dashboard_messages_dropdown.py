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


def test_recipient_dropdown_populated_from_get_agents() -> None:
    src = _read("components/dashboard/messages-dashboard.tsx")
    assert "apiClient.getAgents()" in src, (
        "expected the Compose form to populate recipient options from "
        "apiClient.getAgents()"
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
