"""Arch invariant: a backend REST handler that dispatches an MCP tool
must map ``AuthRejected`` to **403**, never let it fall into the
generic 500 arm.

The recurrence
--------------
``dispatch_tool_call`` RE-RAISES two controlled exception types
(``agent_mcp/tools/registry.py``): ``ToolInputValidationError`` and
``AuthRejected``. Every REST adapter already catches the first (→ 400)
and then has a broad ``except Exception`` → static 500. A tool whose
capability gate lives in a ``@requires_*`` decorator raises
``AuthRejected``, so without an explicit arm the denial of a legitimate
authorization decision is reported to the client as a SERVER ERROR:

* wrong status (500 instead of 403) — a client cannot distinguish
  "you may not do this" from "the backend is broken", and any
  retry/alerting logic keyed on 5xx misfires on a routine denial;
* the denial reason is swallowed by SD-R7-1's static-message arm, so
  the caller is told nothing actionable;
* every such denial is logged at ERROR with a stack trace, burying
  real faults.

This was found and fixed TWICE already, per-site, without a sweep:
``app/routers/agents.py`` (AC-R5-1, forwarding VIEWER passes
``require_operator_session`` but lacks ``agents.terminate``) and
``app/routers/composition.py``'s ``update_task`` adapter (R21-F1).
Five sites were still missing the arm when this test was written —
three of them LIVE (``update_project_settings``,
``delete_project_settings``, ``create_task`` all dispatch tools that
already carried a ``@requires_capability`` gate), two latent
(``memories.py``'s three project-context adapters and
``composition.py``'s sample-memories adapter, whose tools were still
gated in-body and got their stamp in the Phase 2 / Finding A
migration).

Per-site fixes did not converge the class, so this is the structural
backstop: it discovers the call sites from source (no hand-maintained
list) and fails on any ``try`` block that dispatches a tool without an
``except AuthRejected`` arm.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

import agent_mcp.app.routers as _routers_pkg
from tests.harness import mcp_session

#: Callables that can raise ``AuthRejected`` at a REST adapter: the
#: dispatcher itself, its shared helper, and any direct call into a
#: ``@requires_*``-decorated tool implementation.
_DISPATCH_NAMES = frozenset({"dispatch_tool_call", "_dispatch_through_tool"})


def _router_modules() -> list[pathlib.Path]:
    """Every backend REST router module, discovered from the package."""
    pkg_dir = pathlib.Path(_routers_pkg.__file__).parent
    return sorted(p for p in pkg_dir.glob("*.py") if p.name != "__init__.py")


def _is_dispatch_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    name = (
        func.id
        if isinstance(func, ast.Name)
        else func.attr
        if isinstance(func, ast.Attribute)
        else ""
    )
    return name in _DISPATCH_NAMES or name.endswith("_tool_impl")


def _handles_auth_rejected(handlers: list[ast.ExceptHandler]) -> bool:
    for handler in handlers:
        exc_type = handler.type
        candidates = (
            exc_type.elts if isinstance(exc_type, ast.Tuple) else [exc_type]
        )
        for candidate in candidates:
            if isinstance(candidate, ast.Name) and candidate.id == "AuthRejected":
                return True
            if (
                isinstance(candidate, ast.Attribute)
                and candidate.attr == "AuthRejected"
            ):
                return True
    return False


def _dispatching_try_blocks():
    """Yield ``(module_name, lineno)`` for every ``try`` whose body calls
    a tool dispatcher."""
    for path in _router_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            if any(
                _is_dispatch_call(inner)
                for stmt in node.body
                for inner in ast.walk(stmt)
            ):
                yield path.name, node.lineno, node


_DISPATCH_SITES = list(_dispatching_try_blocks())


def test_discovery_found_dispatch_sites() -> None:
    """Guard the guard: a discovery bug would make the sweep vacuous."""
    assert len(_DISPATCH_SITES) >= 7, (
        "expected the backend REST routers to dispatch MCP tools from at "
        f"least 7 guarded try blocks; discovered {len(_DISPATCH_SITES)}"
    )


@pytest.mark.parametrize(
    "module_name,lineno,node",
    _DISPATCH_SITES,
    ids=[f"{m}:{ln}" for m, ln, _ in _DISPATCH_SITES],
)
def test_tool_dispatch_maps_auth_rejected_to_403(
    module_name: str, lineno: int, node: ast.Try
) -> None:
    """Every tool-dispatching ``try`` catches ``AuthRejected`` explicitly."""
    assert _handles_auth_rejected(node.handlers), (
        f"agent_mcp/app/routers/{module_name}:{lineno} dispatches an MCP "
        "tool without an `except AuthRejected` arm. A capability denial "
        "raised by the tool's @requires_* gate would fall into the generic "
        "`except Exception` arm and be reported as a 500 with a static "
        "message instead of a 403 with the reason. Add:\n"
        "    except AuthRejected as e:\n"
        "        return JSONResponse({\"error\": e.reason}, status_code=403)"
    )


# ── Behavioural companion: the three LIVE sites, over the wire ───────
#
# The static sweep above proves the arm EXISTS; these prove it produces
# the right status for a real caller. A signed forwarding VIEWER passes
# ``require_operator_session`` (any operator-tier session admits) but
# does not carry ``system.config.write`` / ``tasks.create`` /
# operator-tier for the scheduler policy gate, so each tool's decorator
# raises ``AuthRejected``. RED before the fix: HTTP 500.

@pytest.mark.asyncio
async def test_settings_put_viewer_gets_403_not_500(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        r = admin.request(
            "PUT",
            "/api/settings/config_allow_worker_to_worker",
            headers=admin.forwarding_header(role="viewer"),
            json={"context_value": True},
        )
        assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_settings_delete_viewer_gets_403_not_500(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        r = admin.request(
            "DELETE",
            "/api/settings/config_allow_worker_to_worker",
            headers=admin.forwarding_header(role="viewer"),
            json={},
        )
        assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_create_task_viewer_gets_403_not_500(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        r = admin.request(
            "POST",
            "/api/tasks",
            headers=admin.forwarding_header(role="viewer"),
            json={"task_title": "viewer probe", "task_description": "x"},
        )
        assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_create_schedule_viewer_gets_403_not_500(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        r = admin.request(
            "POST",
            "/api/schedules",
            headers=admin.forwarding_header(role="viewer"),
            json={
                "agent_id": "nobody",
                "prompt": "viewer probe",
                "interval_seconds": 3600,
            },
        )
        assert r.status_code == 403, r.text
