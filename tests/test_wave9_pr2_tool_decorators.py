"""Wave 9 PR 2 — per-tool ``@requires_capability(...)`` admit/deny.

Wave 9 PR 2 of 7 in ``prancy-napping-pie.md``. PR 2 migrated every
``@requires(...)`` and ``@requires_role(...)`` decorator at the tool
entry point in ``agent_mcp/tools/*.py`` to the new
``@requires_capability(...)`` decorator. This file pins the per-tool
admit/deny matrix the migration locked in, one parameterised test per
migrated tool.

For each migrated tool, two assertions:

* A principal that does NOT carry the gating cap is rejected at the
  decorator layer with :class:`AuthRejected` (capability X required).
* A principal that DOES carry the gating cap admits and the tool
  reaches its body (the test asserts only on "no auth rejection",
  not the body's return shape — the body may legitimately return
  ``Invalid`` / ``NotFound`` / ``Failed`` for the stub arguments).

The cap → tool map (locked by Wave 9 PR 2):

* ``mcp.connect`` → ``get_system_prompt``
* ``tasks.create`` → ``create_self_task``
* ``tasks.view`` → ``view_tasks``, ``search_tasks``
* ``tasks.update`` → ``bulk_task_operations``
* ``tasks.delete`` → ``delete_task``
* ``coordination.assist`` → ``request_assistance``

Existing E2E coverage (``tests/test_wave6_pr4_task_tools_e2e.py``,
``tests/test_tools_list_filter.py``) already pins the bigger-picture
contract: the harness admin (sysadmin wildcard) and the harness
worker (worker bundle) succeed at the tools they're expected to.
This file backstops the migration with the *minimal* gate behaviour
— a principal whose cap set is constructed to NOT include the
gating cap is rejected.
"""
from __future__ import annotations

from typing import Any, Dict

import pytest

from agent_mcp.core.authorize import AuthRejected
from agent_mcp.core.principal import Principal


pytestmark = pytest.mark.asyncio


# ── Helper principals ──────────────────────────────────────────────


def _operator_session_with_caps(*caps: str) -> Principal:
    """Build an ``operator_session`` Principal carrying exactly ``caps``.

    Mirrors :func:`tests.harness.with_capabilities` but constructs
    inline so each test's intent — "this principal carries cap X,
    nothing else" — reads next to the call.
    ``project_role="operator"`` so the per-cap project-membership
    gate in :meth:`Principal.has_capability` admits for the
    non-``system.*`` caps.
    """
    return Principal(
        kind="operator_session",
        user_id="pr2-test-operator",
        agent_id=None,
        sysadmin=False,
        project_name="harness",
        project_role="operator",
        agent_role=None,
        can_wake_loop=False,
        source_token=None,
        capabilities=frozenset(caps),
    )


def _empty_principal() -> Principal:
    """An operator-session Principal with no capabilities at all."""
    return _operator_session_with_caps()


# ── (tool_name, gating_cap, arguments) parameterisation ────────────


# The arguments below are deliberately minimal — just enough to reach
# the body. They are NOT expected to produce successful tool results;
# the only assertion is that the decorator either rejects or admits.
# When the cap admits, downstream the tool may return ``Invalid`` /
# ``NotFound`` / ``Failed`` (because we've supplied a stub task_id or
# similar) — that's still proof the decorator handed off correctly.
_MIGRATED_TOOLS = [
    # tool_name, gating_cap, minimal_arguments
    ("get_system_prompt", "mcp.connect", {"token": "stub"}),
    (
        "create_self_task",
        "tasks.create",
        {
            "task_title": "t",
            "task_description": "d",
            "parent_task_id": "task_stub",
        },
    ),
    ("view_tasks", "tasks.view", {}),
    ("search_tasks", "tasks.view", {"search_query": "needle"}),
    (
        "bulk_task_operations",
        "tasks.update",
        {"operations": [{"type": "update_status", "task_id": "task_x", "status": "in_progress"}]},
    ),
    ("delete_task", "tasks.delete", {"task_id": "task_x"}),
    (
        "request_assistance",
        "coordination.assist",
        {"task_id": "task_x", "description": "help"},
    ),
]


def _impl_for(tool_name: str):
    """Look up the registered impl for ``tool_name``.

    Goes through the live ``tool_registry`` so the decorator-wrapped
    callable is what we drive (same path the dispatcher takes). Lazy
    import keeps test collection cheap.
    """
    from agent_mcp.tools.registry import tool_registry

    entry = tool_registry.get(tool_name)
    assert entry is not None, (
        f"tool {tool_name!r} not in registry — register it before testing"
    )
    return entry.meta.implementation


# ── Reject path: missing cap → AuthRejected ────────────────────────


@pytest.mark.parametrize(
    "tool_name,gating_cap,arguments", _MIGRATED_TOOLS,
)
async def test_principal_without_cap_is_rejected(
    tool_name: str, gating_cap: str, arguments: Dict[str, Any],
) -> None:
    """A principal whose cap set does NOT contain the gating cap
    raises :class:`AuthRejected` mentioning the missing cap.

    Reaches the impl through the registered (decorated) callable so
    the gate fires at the same seam the dispatcher hits.
    """
    impl = _impl_for(tool_name)
    principal = _empty_principal()
    assert not principal.has_capability(gating_cap), (
        f"test setup bug: empty principal must not carry {gating_cap!r}"
    )

    with pytest.raises(AuthRejected) as excinfo:
        await impl(arguments, principal=principal)

    assert gating_cap in excinfo.value.reason, (
        f"expected AuthRejected to mention missing cap {gating_cap!r}; "
        f"got {excinfo.value.reason!r}"
    )


# ── Admit path: cap present → decorator hands off to body ──────────


@pytest.mark.parametrize(
    "tool_name,gating_cap,arguments", _MIGRATED_TOOLS,
)
async def test_principal_with_cap_admits_decorator(
    tool_name: str, gating_cap: str, arguments: Dict[str, Any],
    tmp_path,
) -> None:
    """A principal carrying ``gating_cap`` clears the decorator —
    the impl runs and returns whatever its body produces for the
    minimal inputs (Invalid / NotFound / Failed are all fine; the
    point is "no AuthRejected with the cap-required message").

    Uses :func:`tests.harness.mcp_session` so the DB schema + globals
    exist (some tool bodies touch the DB even on the not-found path).
    """
    from tests.harness import mcp_session

    impl = _impl_for(tool_name)
    principal = _operator_session_with_caps(gating_cap)
    assert principal.has_capability(gating_cap), (
        f"test setup bug: principal with {gating_cap!r} should carry it"
    )

    # Bring up the app so DB-touching tools can reach their tables.
    async with mcp_session(tmp_path):
        try:
            await impl(arguments, principal=principal)
        except AuthRejected as e:
            pytest.fail(
                f"{tool_name} rejected a principal carrying {gating_cap!r}: "
                f"{e.reason}"
            )
        # Any other exception is fine here — we're not asserting on
        # the body's success, only that the decorator handed off.
        except Exception:
            pass
