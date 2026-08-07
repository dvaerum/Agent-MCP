"""`_can_agents_communicate` admin-recipient check must be exact.

Security (LOW authz): the recipient check used
``recipient_id.lower().startswith("admin")`` which let a worker
message ANY agent whose id merely starts with "admin" (e.g.
"admin-impersonator"), side-stepping the worker→worker default-deny
toggle. The fix compares against the canonical ``"admin"`` identity
exactly. Legitimate worker→admin messaging is preserved.
"""

from __future__ import annotations

from agent_mcp.tools import agent_communication_tools as _mod
from agent_mcp.tools.agent_communication_tools import (
    _can_agents_communicate,
)


def test_worker_to_admin_lookalike_denied(monkeypatch) -> None:
    """A worker messaging "admin-<x>" (startswith, not equal) is denied."""
    # Default-deny toggle so the only thing that could allow this is the
    # admin-recipient shortcut. If the shortcut still wildcards, this
    # returns True (the bug).
    monkeypatch.setattr(
        _mod._access, "_get_config_bool", lambda *a, **k: False,
    )
    allowed, _reason = _can_agents_communicate(
        "worker1", "admin-impersonator", is_admin=False,
    )
    assert allowed is False


def test_worker_to_real_admin_allowed(monkeypatch) -> None:
    """A worker messaging the canonical "admin" is still allowed."""
    monkeypatch.setattr(
        _mod._access, "_get_config_bool", lambda *a, **k: False,
    )
    allowed, _reason = _can_agents_communicate(
        "worker1", "admin", is_admin=False,
    )
    assert allowed is True


def test_worker_to_worker_default_deny_unchanged(monkeypatch) -> None:
    """Worker→worker default-deny is untouched by the fix."""
    monkeypatch.setattr(
        _mod._access, "_get_config_bool", lambda *a, **k: False,
    )
    allowed, _reason = _can_agents_communicate(
        "worker1", "worker2", is_admin=False,
    )
    assert allowed is False
