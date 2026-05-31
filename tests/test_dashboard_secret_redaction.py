"""Regression guards for dashboard Memories UI secret redaction.

UPSTREAM_ISSUES.md issue B. Agent-MCP stores its own admin_token
under the project_context key `config_admin_token`; the unpatched
Memories UI renders the value in the cell and in the title tooltip,
and the Copy button copies it. Anyone the dashboard is shared with
sees admin-level credentials.

Both memories-dashboard.tsx and view-memory-modal.tsx need to mask
values whose key matches a secret-looking pattern.
"""

from __future__ import annotations

from pathlib import Path

ROW = Path("agent_mcp/dashboard/components/dashboard/memories-dashboard.tsx")
MODAL = Path("agent_mcp/dashboard/components/dashboard/modals/view-memory-modal.tsx")


def test_memories_dashboard_masks_secret_keys() -> None:
    src = ROW.read_text()
    assert "token" in src.lower() and "secret" in src.lower(), (
        "expected memories-dashboard.tsx to define a secret-key pattern "
        "(matching at least 'token' and 'secret')"
    )
    # the mask itself (bullets or "redacted" marker)
    assert "••••" in src or "redacted" in src.lower(), (
        "expected memories-dashboard.tsx to mask values with bullets or "
        "a 'redacted' marker"
    )


def test_view_memory_modal_masks_secret_keys() -> None:
    src = MODAL.read_text()
    assert "token" in src.lower() and "secret" in src.lower(), (
        "expected view-memory-modal.tsx to define a secret-key pattern"
    )
    assert "redacted" in src.lower() or "••••" in src, (
        "expected view-memory-modal.tsx to mask values for secret keys"
    )
