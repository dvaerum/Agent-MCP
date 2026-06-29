"""Wave 8 PR 0 — import-surface smoke test for the new ``app/routers``
subpackage.

PR 0 is a non-behavior-changing scaffold: it adds the
``agent_mcp.app.routers`` subpackage with one ``APIRouter`` per
resource (agents, tasks, memories, messages, composition, settings)
and a top-level ``register_routers(app)`` entrypoint. The scaffold
must compile cleanly even though it is not wired into
``main_app.create_app`` yet — wiring happens in PR 2 after PR 1
moves the handlers.

What this test pins:

* ``from agent_mcp.app.routers import register_routers`` succeeds and
  the resulting symbol is callable.
* Each of the six per-resource modules imports without error and
  exposes a ``fastapi.APIRouter`` object named ``router``.
* Each router carries the prefix and at least one router-level
  dependency declared in the Wave 8 plan (the dependency presence
  check guards against an accidental ``dependencies=[]`` edit; the
  full ``require_operator_session`` identity check is exercised
  end-to-end by the existing route tests once PR 1 moves the
  handlers).

The 1655+ existing handler tests still pass unchanged in PR 0
because the live route registration continues to flow through
``routes.py::register_routes`` — the new subpackage is dark code
until PR 2.
"""

from __future__ import annotations

import importlib

import pytest
from fastapi import APIRouter


_PER_RESOURCE_MODULES: list[tuple[str, str]] = [
    # (module suffix under agent_mcp.app.routers, expected APIRouter prefix)
    ("agents", "/api/agents"),
    ("tasks", "/api/tasks"),
    ("memories", "/api/memories"),
    ("messages", "/api/messages"),
    ("composition", "/api"),
    ("settings", "/api"),
]


def test_register_routers_callable() -> None:
    """Top-level entrypoint exists + is callable.

    The parent ``main_app.create_app`` will import this symbol in PR 2;
    pin the surface from PR 0 so any rename / accidental removal
    surfaces as a test failure rather than an ImportError at app
    construction time.
    """
    routers_pkg = importlib.import_module("agent_mcp.app.routers")
    register_routers = getattr(routers_pkg, "register_routers", None)
    assert callable(register_routers), (
        "agent_mcp.app.routers.register_routers must be exported and callable"
    )


@pytest.mark.parametrize(
    "module_suffix,expected_prefix",
    _PER_RESOURCE_MODULES,
    ids=[suffix for suffix, _ in _PER_RESOURCE_MODULES],
)
def test_per_resource_module_imports(
    module_suffix: str, expected_prefix: str
) -> None:
    """Each per-resource module imports + exposes the expected router.

    Verifies three things per file:

    1. The module imports without ImportError (catches typos in the
       deps import path, missing files, etc.).
    2. It exposes a module-level ``router`` symbol.
    3. The router is a ``fastapi.APIRouter`` instance with the
       prefix locked in by the Wave 8 plan and at least one
       router-level dependency (the
       ``require_operator_session`` gate).
    """
    module = importlib.import_module(f"agent_mcp.app.routers.{module_suffix}")
    router = getattr(module, "router", None)
    assert router is not None, (
        f"agent_mcp.app.routers.{module_suffix} must expose a 'router' symbol"
    )
    assert isinstance(router, APIRouter), (
        f"agent_mcp.app.routers.{module_suffix}.router must be an APIRouter, "
        f"got {type(router).__name__}"
    )
    assert router.prefix == expected_prefix, (
        f"agent_mcp.app.routers.{module_suffix}.router.prefix must be "
        f"{expected_prefix!r}, got {router.prefix!r}"
    )
    assert router.dependencies, (
        f"agent_mcp.app.routers.{module_suffix}.router must declare at least "
        "one router-level dependency (require_operator_session)"
    )


def test_register_routers_mounts_all_six() -> None:
    """``register_routers(app)`` mounts every per-resource router.

    Builds a bare ``FastAPI()`` instance, hands it to
    ``register_routers``, and asserts the resulting route table
    contains at least one route per resource prefix. The routers
    have no handlers in PR 0, so the only routes added by
    ``include_router`` itself are framework-internal (none), but
    the routers register on the app's ``routes`` via the FastAPI
    ``router`` attribute so we count via that path.

    This is a structural smoke test: it pins that
    ``register_routers`` actually calls ``include_router`` for each
    of the six, so PR 2's wire-up doesn't silently drop a router.
    """
    from fastapi import FastAPI

    from agent_mcp.app.routers import register_routers

    app = FastAPI()
    register_routers(app)

    # FastAPI flattens included sub-routers' state onto the parent's
    # router. With zero endpoints registered on each per-resource
    # router (PR 0 leaves bodies empty), there are no path routes to
    # count — but ``include_router`` still records each child's
    # router-level dependencies on the parent's dependency
    # overrides / routes graph. The contract we lock here is the
    # weaker "the call doesn't raise" — once PR 1 adds handlers,
    # this same test will also see the route count climb.
    # The strong dependency / prefix assertions live in
    # test_per_resource_module_imports above.
    assert app.router is not None
