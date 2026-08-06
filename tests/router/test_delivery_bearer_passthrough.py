"""The front-door router must let agent-bearer delivery traffic through.

ADR-0021's delivery transport exposes ``/agent-mcp/api/<project>/delivery/
{stream,status}``, authenticated by the *agent bearer* at the backend
(``require_agent_bearer``). But ``require_operator_session_middleware`` gates
every ``/agent-mcp/api/<project>/...`` path on an *operator session cookie* and
only allow-lists ``/agent-mcp/mcp/`` for the agent-side bearer. So without a
carve-out, an agent's delivery request is rejected with ``login_required`` and
never reaches the backend — delivery can never work.

These tests pin the carve-out: the two delivery routes skip the operator gate
(the backend's bearer auth is the real gate), while the rest of
``/api/<project>/...`` stays operator-gated and the match is tight.
"""

import pytest

from agent_mcp.router import auth_middleware as am


@pytest.mark.parametrize(
    "path",
    [
        "/agent-mcp/api/washing/delivery/stream",
        "/agent-mcp/api/washing/delivery/status",
        "/agent-mcp/api/pikvm-on-nixos-with-mcp-support/delivery/stream",
        "/agent-mcp/api/washing/delivery/stream/",  # trailing slash tolerated
    ],
)
def test_delivery_paths_skip_operator_gate(path):
    assert am._path_is_delivery(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "/agent-mcp/api/washing/tokens",  # ordinary project route — still gated
        "/agent-mcp/api/washing/delivery",  # bare, not a route
        "/agent-mcp/api/washing/delivery/evil",  # tight: only stream|status
        "/agent-mcp/api/router/health",  # router admin, not delivery
        "/agent-mcp/app/washing/delivery/stream",  # /app, not /api
        "/agent-mcp/api//delivery/stream",  # empty project segment
        "/delivery/stream",  # not under the mount
    ],
)
def test_non_delivery_paths_do_not_skip(path):
    assert am._path_is_delivery(path) is False


def test_delivery_carveout_is_wired_into_the_gate():
    """The middleware's pass-through set must actually consult the
    delivery matcher — a matcher nothing calls is dead."""
    import inspect

    src = inspect.getsource(am.require_operator_session_middleware)
    assert "_path_is_delivery" in src, (
        "require_operator_session_middleware must let delivery paths through"
    )


# ── Integration: delivery clears the front-door gates ───────────────
# The backend isn't spawned in these tests, so a request that clears the
# operator-session gate AND the Accept-version gate lands on a 5xx (backend
# down) — NOT a 406 (version gate). The status POST carries NO versioned Accept
# header (the bridge doesn't send one), so WITHOUT the delivery version-gate
# exemption it would 406 before ever reaching the proxy — the 5xx proves the
# exemption. (Mirrors test_mcp_route_*_reaches_handler.)


@pytest.mark.asyncio
async def test_delivery_status_post_exempt_from_version_gate(
    aiohttp_client, router_app, register_project,
):
    register_project("delta")
    client = await aiohttp_client(router_app)
    resp = await client.post(
        "/agent-mcp/api/delta/delivery/status",  # no versioned Accept header
        json={"status": "idle"},
        allow_redirects=False,
    )
    body = await resp.text()
    assert resp.status != 406, f"version gate should exempt delivery/status: {body}"
    assert resp.status >= 500, f"expected to reach the down backend: {resp.status} {body}"


@pytest.mark.asyncio
async def test_delivery_stream_get_reaches_proxy(
    aiohttp_client, router_app, register_project,
):
    register_project("delta")
    client = await aiohttp_client(router_app)
    resp = await client.get(
        "/agent-mcp/api/delta/delivery/stream",
        headers={"Accept": "text/event-stream"},
        allow_redirects=False,
    )
    body = await resp.text()
    assert resp.status != 406, f"version gate should exempt delivery/stream: {body}"
    assert resp.status >= 500, f"expected to reach the down backend: {resp.status} {body}"


@pytest.mark.asyncio
async def test_version_gate_exemption_is_tight(
    aiohttp_client, router_app, register_project,
):
    """A non-delivery /api route with NO versioned Accept is still 406 — the
    delivery exemption must not open the version gate for other routes."""
    register_project("delta")
    client = await aiohttp_client(router_app)
    resp = await client.get(
        "/agent-mcp/api/delta/tokens",  # no versioned Accept header
        allow_redirects=False,
    )
    assert resp.status == 406, await resp.text()
