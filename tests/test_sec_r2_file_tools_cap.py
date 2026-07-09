"""SEC round-2 (defense-in-depth): file tools gate on the ``files.use``
capability, not the bare principal ``kind``.

Finding (round-2 authz sweep, LOW / defense-in-depth): the file-claim
and file-metadata *read* tools gated on ``principal.kind ==
"agent_bearer"`` alone. That admits a bearer whose ``agent_role`` is
``None`` — an empty-capability token that carries no ``files.use`` cap
could still claim / inspect files. It's inconsistent with the Wave-9
capability model (the single authorization vocabulary).

Fix (mirrors ``rag_tools.py`` under SEC Wave-B / Finding 2): keep the
``kind == "agent_bearer"`` structural check — these tools are
agent-keyed by design (the in-memory file map and metadata working-dir
resolution key on ``agent_id``, which operator sessions don't carry) —
AND add ``principal.has_capability("files.use")`` so an empty-caps
bearer is denied.

These tests pin the capability gate directly against each tool impl:

* a worker bearer (``files.use`` present) is admitted,
* an empty-caps bearer (``agent_role=None``, no ``files.use``) is
  denied — the RED case against the pre-fix ``kind``-only gate,
* a viewer operator (no ``files.use``) is denied.
"""

from __future__ import annotations

import pytest

from agent_mcp.core.principal import Principal
from agent_mcp.core.tool_result import Ok, PermissionDenied
from agent_mcp.tools.registry import dispatch_tool_call
from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio


# ── Principal builders ───────────────────────────────────────────


def _bearer(agent_id: str, *, role: str | None) -> Principal:
    """agent_bearer Principal. ``role=None`` yields an empty cap set
    (no ``files.use``); ``role="worker"`` carries ``files.use`` via the
    worker bundle."""
    return Principal(
        kind="agent_bearer",
        user_id=None,
        agent_id=agent_id,
        sysadmin=False,
        project_name=None,
        project_role=None,
        agent_role=role,  # type: ignore[arg-type]
        can_wake_loop=False,
        source_token="tok-" + agent_id,
    )


def _viewer_operator(user_id: str = "vic") -> Principal:
    """operator_session with viewer role — read-only bundle, no
    ``files.use``."""
    return Principal(
        kind="operator_session",
        user_id=user_id,
        agent_id=None,
        sysadmin=False,
        project_name="demo",
        project_role="viewer",
        agent_role=None,
        can_wake_loop=False,
        source_token=None,
    )


# The three gates under test: (tool_name, minimal valid arguments).
_FILE_TOOLS = [
    ("check_file_status", {"filepath": "/tmp/sec-r2-cap.txt"}),
    (
        "update_file_status",
        {"filepath": "/tmp/sec-r2-cap.txt", "status": "editing"},
    ),
    ("view_file_metadata", {"filepath": "/tmp/sec-r2-cap.txt"}),
]


@pytest.mark.parametrize("tool_name,arguments", _FILE_TOOLS)
async def test_file_tool_admits_bearer_with_files_use(
    tmp_path, tool_name, arguments
) -> None:
    """A worker bearer carries ``files.use`` and is admitted past the
    gate (result is not a PermissionDenied)."""
    async with mcp_session(tmp_path):
        p = _bearer("alice", role="worker")
        assert p.has_capability("files.use")  # precondition
        result = await dispatch_tool_call(tool_name, arguments, principal=p)
        assert not isinstance(result, PermissionDenied), (
            f"{tool_name}: worker with files.use was denied: {result!r}"
        )


@pytest.mark.parametrize("tool_name,arguments", _FILE_TOOLS)
async def test_file_tool_denies_empty_caps_bearer(
    tmp_path, tool_name, arguments
) -> None:
    """An agent_bearer whose role is None carries NO caps, so lacks
    ``files.use`` — it must be denied even though its ``kind`` is
    ``agent_bearer``.

    This is the RED case: the pre-fix ``kind``-only gate admitted this
    caller.
    """
    async with mcp_session(tmp_path):
        p = _bearer("nobody", role=None)
        assert not p.has_capability("files.use")  # precondition
        result = await dispatch_tool_call(tool_name, arguments, principal=p)
        assert isinstance(result, PermissionDenied), (
            f"{tool_name}: empty-caps bearer should be denied, got {result!r}"
        )


@pytest.mark.parametrize("tool_name,arguments", _FILE_TOOLS)
async def test_file_tool_denies_viewer_operator(
    tmp_path, tool_name, arguments
) -> None:
    """A viewer operator lacks ``files.use`` and is denied (and these
    agent-keyed tools reject operator sessions regardless)."""
    async with mcp_session(tmp_path):
        p = _viewer_operator()
        assert not p.has_capability("files.use")  # precondition
        result = await dispatch_tool_call(tool_name, arguments, principal=p)
        assert isinstance(result, PermissionDenied), (
            f"{tool_name}: viewer operator should be denied, got {result!r}"
        )


async def test_worker_can_round_trip_after_cap_gate(tmp_path) -> None:
    """End-to-end: a worker (files.use) claims and reads a file through
    the capability gate, proving the gate admits the intended caller."""
    async with mcp_session(tmp_path):
        p = _bearer("alice", role="worker")
        claim = await dispatch_tool_call(
            "update_file_status",
            {"filepath": "/tmp/sec-r2-roundtrip.txt", "status": "editing"},
            principal=p,
        )
        assert isinstance(claim, Ok), claim
        assert claim.data["agent_id"] == "alice"

        check = await dispatch_tool_call(
            "check_file_status",
            {"filepath": "/tmp/sec-r2-roundtrip.txt"},
            principal=p,
        )
        assert isinstance(check, Ok), check
        assert check.data["in_use"] is True
