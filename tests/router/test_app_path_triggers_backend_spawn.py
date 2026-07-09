"""The dashboard ``/app/<name>/`` path must warm-start the backend.

Background
----------
Opening a project's dashboard at ``/agent-mcp/app/<name>/`` serves the
static Next.js shell; the SPA then fires ``/agent-mcp/api/<name>/...``
XHRs which lazily spawn the per-project backend via ``_ensure``. But a
non-browser client (curl, a smoke test, a health probe) fetches only
the HTML and never runs the JS, so nothing triggers the spawn.

The router documents the contract as "``systemctl start
agent-mcp@<name>`` lazily on first request" — the HTML GET IS a first
request. So the ``/app/<name>/`` handler must ALSO kick the lazy-spawn,
consistent with how the ``/api/`` handler does it, warming the backend
while the shell paints instead of stalling the SPA's first XHR on a
cold start.

The warm-start is best-effort and non-blocking: the static shell is
served regardless of whether the backend comes up, so an unknown
project or a spawn failure never turns the dashboard into a 5xx (the
SPA-fallback contract in ``test_spa_fallback.py`` stays intact).

Regression guard for the ``nix/tests/no-auto-cleanup.nix`` VM test,
whose step 2 curls ``/agent-mcp/app/idle-test/`` expecting the backend
unit to come up.
"""

from __future__ import annotations

import asyncio

import pytest


pytestmark = pytest.mark.asyncio


async def _wait_for_start(stub, unit: str, timeout: float = 2.0) -> bool:
    """Poll the systemctl recorder until it records a start for ``unit``.

    The warm-start runs as a tracked background task, so it lands a
    beat after the HTML response returns; give the event loop a few
    ticks to run it.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if stub.counts.get(("start", unit)) or stub.counts.get(
            ("restart", unit)
        ):
            return True
        await asyncio.sleep(0.02)
    return False


async def test_app_path_warm_starts_registered_backend(
    aiohttp_client, router_app, register_project, systemctl_stub,
    write_dashboard_file,
) -> None:
    """GET ``/agent-mcp/app/<registered>/`` triggers a systemctl start
    for the project's backend unit."""
    write_dashboard_file("index.html", "<!doctype html><html></html>")
    register_project("warm-me")
    client = await aiohttp_client(router_app)

    resp = await client.get("/agent-mcp/app/warm-me/")

    assert resp.status == 200, await resp.text()
    unit = "agent-mcp@warm-me.service"
    assert await _wait_for_start(systemctl_stub, unit), (
        "GET /agent-mcp/app/warm-me/ did not warm-start the backend — "
        f"no `systemctl start {unit}` was recorded. The /app/ handler "
        "must kick the lazy-spawn like /api/ does."
    )


async def test_app_path_unknown_project_does_not_spawn(
    aiohttp_client, router_app, systemctl_stub, write_dashboard_file,
) -> None:
    """An unregistered project name serves the SPA shell (200) but must
    NOT invoke systemctl — no arbitrary unit start for a name that
    isn't in the registry."""
    write_dashboard_file("index.html", "<!doctype html><html></html>")
    client = await aiohttp_client(router_app)

    resp = await client.get("/agent-mcp/app/ghost/")

    assert resp.status == 200, await resp.text()
    # Give any (erroneous) background warm task a chance to fire.
    await asyncio.sleep(0.2)
    assert not systemctl_stub.counts.get(("start", "agent-mcp@ghost.service")), (
        "unregistered /app/<name>/ must not spawn a backend unit"
    )
