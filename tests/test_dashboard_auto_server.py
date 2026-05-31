"""Regression guards for the dashboard path-prefix auto-seed + cold-start retry.

These verify that ApiClientInitializer contains the auto-seed and
retry logic that path-prefixed deployments depend on. Regression
guards (parse .tsx as text) — not behavioral tests. Behavior is
verified by `npm run build` + manual click-through documented in
the PR body.
"""

from __future__ import annotations

from pathlib import Path

INIT_TSX = Path(
    "agent_mcp/dashboard/components/providers/api-client-initializer.tsx"
)


def _src() -> str:
    return INIT_TSX.read_text()


def test_auto_seed_uses_dashboard_path_regex() -> None:
    """ApiClientInitializer must inspect window.location.pathname for
    the deployment URL pattern so the dashboard self-bootstraps when
    mounted under /agent-mcp/__dashboard/<name>/."""
    src = _src()
    assert "/agent-mcp/__dashboard" in src, (
        "expected the path-prefix regex `/agent-mcp/__dashboard` in "
        "api-client-initializer.tsx; auto-seed only works when the "
        "deployment URL pattern is detected"
    )
    assert "window.location.pathname" in src, (
        "expected ApiClientInitializer to read window.location.pathname"
    )


def test_auto_seed_waits_for_persist_hydration() -> None:
    """Seeding must wait until zustand-persist hydrates, else the
    first render seeds a duplicate entry every reload."""
    src = _src()
    assert (
        "onFinishHydration" in src or "hasHydrated" in src
    ), (
        "expected the auto-seed effect to gate on persist hydration "
        "(`onFinishHydration` or `hasHydrated`); without it, the first "
        "render seeds before the persisted state arrives and creates "
        "a duplicate entry on every reload"
    )


def test_cold_start_retry_loop_present() -> None:
    """Cold-start retry: backend's lazy spawn takes 10-15s; without
    a retry the first health check fails and the user sees the
    'Connect to MCP Server' screen even though the URL identifies
    the project."""
    src = _src()
    assert "setInterval" in src, (
        "expected setInterval-based retry loop for the cold-start "
        "race against lazy backend spawn"
    )
