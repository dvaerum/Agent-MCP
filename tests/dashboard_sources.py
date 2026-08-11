"""Source-text helpers for the grep-style dashboard regression guards.

Several dashboard pages have been split from a single god-file into a
page module plus a directory of satellites (the Agents page: PR
``feat/webui-migrate-agents``). The guards in ``test_dashboard_*.py``
assert properties of the *page* — "the row action buttons stop
propagation", "the detail dialog caps at 90vh" — not properties of a
particular file, so they read the page **and its satellites** as one
text blob.

Concatenation order is deliberate: the page first, then its satellites
in a fixed order, so the ``const AgentDetailDialog = … </DialogFooter>``
style slices used by ``test_dashboard_agents_popup_polish.py`` stay
well-defined.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD = REPO_ROOT / "agent_mcp" / "dashboard"

# The Agents page + every module it was split into. Keep this list in
# sync when an Agents satellite is added or removed — a missing entry
# silently narrows the audit surface.
AGENTS_SOURCES: tuple[str, ...] = (
    "components/dashboard/agents-dashboard.tsx",
    "components/dashboard/agents/agent-columns.tsx",
    "components/dashboard/agents/agent-detail-dialog.tsx",
    "components/dashboard/agents/agent-presence.tsx",
    "components/dashboard/agents/edit-agent-dialog.tsx",
    "components/dashboard/agents/purge-agent-dialog.tsx",
    "components/dashboard/agents/register-agent-modal.tsx",
    "components/dashboard/agents/terminate-agent-dialog.tsx",
    "components/dashboard/agents-mobile-list.tsx",
    "lib/mcp-snippets.ts",
)


# The Messages page + every module it was split into (Wave 5:
# refactor/w5-messages). Same rule as AGENTS_SOURCES — keep this list in
# sync when a Messages satellite is added or removed, or the source-grep
# guards silently narrow their audit surface. Order is deliberate: the
# page first, then satellites, so proximity slices (e.g. the "Recipient
# agent_id" → `<Select>` window) stay well-defined within one file.
MESSAGES_SOURCES: tuple[str, ...] = (
    "components/dashboard/messages-dashboard.tsx",
    "components/dashboard/messages/messages-api.ts",
    "components/dashboard/messages/use-messages-columns.tsx",
    "components/dashboard/messages/compose-message-modal.tsx",
    "components/dashboard/messages/view-message-modal.tsx",
    "components/dashboard/messages/message-delete-preview.tsx",
    "components/dashboard/messages/messages-pagination.tsx",
    "components/dashboard/messages-mobile-list.tsx",
)


def read_dashboard(rel: str) -> str:
    """Read one dashboard-relative source file."""
    return (DASHBOARD / rel).read_text(encoding="utf-8")


def agents_page_source() -> str:
    """The Agents page and all of its satellites, concatenated."""
    missing = [rel for rel in AGENTS_SOURCES if not (DASHBOARD / rel).is_file()]
    assert not missing, (
        "Agents page satellite(s) missing — the source-grep guards would "
        f"silently stop auditing them: {missing}"
    )
    return "\n".join(read_dashboard(rel) for rel in AGENTS_SOURCES)


def messages_page_source() -> str:
    """The Messages page and all of its satellites, concatenated."""
    missing = [rel for rel in MESSAGES_SOURCES if not (DASHBOARD / rel).is_file()]
    assert not missing, (
        "Messages page satellite(s) missing — the source-grep guards would "
        f"silently stop auditing them: {missing}"
    )
    return "\n".join(read_dashboard(rel) for rel in MESSAGES_SOURCES)
