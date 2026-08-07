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
