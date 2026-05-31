"""Regression guard for the dashboard-wrapper hydration gate (issue E).

zustand-persist hydrates after first paint. Reading `activeServerId`
at render time gives the default empty state during SSG and the
persisted state on the client → mismatch → React error #418. Gate
on a post-mount `hydrated` flag so first client paint matches SSG.
"""

from __future__ import annotations

from pathlib import Path

WRAPPER = Path(
    "agent_mcp/dashboard/components/dashboard/dashboard-wrapper.tsx"
)


def test_dashboard_wrapper_gates_on_hydration() -> None:
    """DashboardWrapper must gate `isConnected` on a hydration flag
    so SSG markup matches the first client render."""
    src = WRAPPER.read_text()
    # Must use a hydration signal (one of these strings)
    assert (
        "useState" in src and "useEffect" in src
    ), "expected useState + useEffect for the post-mount hydrated flag"
    assert (
        "onFinishHydration" in src or "hasHydrated" in src
    ), (
        "expected the hydration gate to use zustand persist API "
        "(`onFinishHydration` / `hasHydrated`)"
    )
