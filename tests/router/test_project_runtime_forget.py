"""Drift-catcher for the single per-project clear path.

The seven per-project lifecycle maps used to be separate module globals,
each cleared by its own copy-pasted "on lifecycle end" block — blocks
that had DRIFTED to wipe different subsets (admin_api cleared 5,
``ProjectOrchestrator.stop`` cleared 3, the idle reaper 2). They are one
``ProjectRuntime`` value object now, and ``forget()`` is the single clear
path.

These tests pin the invariant that makes the drift structurally
impossible:

  * ``forget(name)`` leaves ZERO residual across EVERY field — so adding
    a new ``ProjectRuntime`` field and forgetting to clear it in
    ``forget`` fails here.
  * ``forget(name, keep_hmac=True)`` clears everything EXCEPT the cached
    HMAC key — the idle reaper's deliberate F015 v4 retention.
"""

from __future__ import annotations

import asyncio
import importlib
import sys

import pytest


@pytest.fixture
def po(router_env):
    """A freshly re-imported ``project_orchestrator`` with module-level
    ``runtime`` reset per test (mirrors the ``orchestrator_module``
    drop-and-reimport pattern; no systemctl stub needed — ``forget``
    never shells out)."""
    for mod_name in (
        "agent_mcp.router.app",
        "agent_mcp.router.project_orchestrator",
        "agent_mcp.router.project_registry",
    ):
        sys.modules.pop(mod_name, None)
    return importlib.import_module("agent_mcp.router.project_orchestrator")


def _seed(po, name: str) -> None:
    """Populate EVERY ``ProjectRuntime`` field for ``name``."""
    rt = po._rt(name)
    rt.last_active["backend"] = 123.0
    rt.active_conns = 2
    rt.unit_start_times["backend"] = 456.0
    rt.ensure_failures["backend"] = (789.0, "boom")
    rt.ensure_locks["backend"] = asyncio.Lock()
    rt.forwarding_hmac_key = b"k" * 32
    rt.warm_inflight = True


def _assert_non_hmac_state_cleared(po, name: str) -> None:
    """Every field EXCEPT the HMAC key must read as empty via the
    compat views."""
    assert (name, "backend") not in po.last_active
    assert po.active_conns.get(name, 0) == 0
    assert name not in po.active_conns
    assert (name, "backend") not in po.unit_start_times
    assert (name, "backend") not in po.ensure_failures
    assert (name, "backend") not in po.ensure_locks
    assert name not in po._warm_inflight


def test_forget_clears_every_field(po) -> None:
    """``forget(name)`` leaves ZERO residual across every field, and the
    now-empty row is dropped from ``runtime`` entirely."""
    _seed(po, "proj")
    assert "proj" in po.runtime

    po.forget("proj")

    _assert_non_hmac_state_cleared(po, "proj")
    assert "proj" not in po.forwarding_hmac_keys
    assert "proj" not in po.runtime, (
        "forget() must drop the fully-emptied ProjectRuntime row"
    )


def test_forget_keep_hmac_retains_only_the_hmac(po) -> None:
    """``forget(name, keep_hmac=True)`` mirrors the idle reaper: the
    cached HMAC key survives, everything else is cleared."""
    _seed(po, "proj")

    po.forget("proj", keep_hmac=True)

    assert po.forwarding_hmac_keys.get("proj") == b"k" * 32, (
        "keep_hmac=True must retain the cached HMAC key (F015 v4 retention)"
    )
    assert "proj" in po.forwarding_hmac_keys
    _assert_non_hmac_state_cleared(po, "proj")
    # The row survives (it still holds the HMAC key) but carries no other
    # live state.
    assert "proj" in po.runtime


def test_forget_keep_lock_retains_only_the_lock(po) -> None:
    """``forget(name, keep_lock=True)`` — the inside-the-lock clear used
    by delete/rename/stop — keeps the ``_ensure`` lock so it can be
    dropped separately after the lock is released."""
    _seed(po, "proj")

    po.forget("proj", keep_lock=True)

    proj_lock = po.runtime["proj"].ensure_locks.get("backend")
    assert proj_lock is not None
    assert isinstance(proj_lock, asyncio.Lock)
    assert ("proj", "backend") in po.ensure_locks
    # Everything else (including the HMAC key — keep_hmac defaults False)
    # is cleared.
    assert ("proj", "backend") not in po.last_active
    assert po.active_conns.get("proj", 0) == 0
    assert ("proj", "backend") not in po.unit_start_times
    assert ("proj", "backend") not in po.ensure_failures
    assert "proj" not in po._warm_inflight
    assert "proj" not in po.forwarding_hmac_keys


def test_forget_only_touches_the_named_project(po) -> None:
    """A sibling project's state must survive ``forget`` of another."""
    _seed(po, "gone")
    _seed(po, "stays")

    po.forget("gone")

    assert "gone" not in po.runtime
    assert (stays := po.runtime.get("stays")) is not None
    assert stays.last_active.get("backend") == 123.0
    assert (("stays", "backend")) in po.last_active


def test_forget_unknown_project_is_a_noop(po) -> None:
    """``forget`` of a name with no runtime row must not raise."""
    po.forget("never-seen")
    po.forget("never-seen", keep_hmac=True)
    assert "never-seen" not in po.runtime
