"""Pentest R3-F3 (defense-in-depth half): ``_proxy_to_backend`` had no
exception handling around the Unix-socket connect.

The three ``active_conns`` TOCTOU sites (see
``test_sec_r3f3_active_conns_toctou.py``) are one way a backend can get
reaped out from under an in-flight/about-to-connect request, but they
aren't the only one — any backend crash/restart between ``_ensure``
resolving the socket path and ``_proxy_to_backend`` actually connecting
to it hits the same gap. ``sess.request(...)`` performs the UDS
``connect()`` lazily on ``__aenter__``, and a socket that's gone (file
removed) or refused (file present, nothing listening — the shape left
by a fresh ``systemctl stop``) both raise
``aiohttp.client_exceptions.UnixClientConnectorError`` (a
``ClientConnectorError`` subclass — confirmed directly against a real
UDS in both shapes). That was previously UNCAUGHT, so it fell through to
aiohttp's generic handler as a raw unhandled 500.

Fix: catch ``ClientConnectorError`` around the connect and answer with a
clean, retryable 502 instead.
"""

from __future__ import annotations

import socket

import pytest

pytestmark = pytest.mark.asyncio


async def test_proxy_backend_connect_refused_returns_clean_502(
    aiohttp_client, router_app, router_module, router_env, systemctl_stub,
) -> None:
    """Socket file present (survives a ``systemctl stop`` — see
    ``BL-R35-1`` in ``admin_api.py``: ``RuntimeDirectoryPreserve=yes``)
    but nothing listening — an actual connect attempt gets
    ``ECONNREFUSED``, exactly the shape left by a backend reaped moments
    earlier by a concurrent delete/rename/stop (R3-F3)."""
    name = "proj-refused"
    router_module._REGISTRY.register(name, str(router_env.root / "ws"))
    sock_path = router_env.sock_dir / name / "backend.sock"
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    sock_path.unlink(missing_ok=True)

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(sock_path))
    srv.listen(1)
    srv.close()  # path stays on disk; nothing is listening anymore

    # Mark the unit "active" so `_ensure()` doesn't try to (re)start it —
    # `needs_start` also checks `sock.exists()`, which is still True.
    systemctl_stub.active_units.add(f"agent-mcp@{name}.service")
    router_module._agent_token_cache[name] = (9.9e18, {"tok-1234": "Admin"})

    client = await aiohttp_client(router_app)
    resp = await client.post(
        f"/agent-mcp/mcp/{name}",
        data=b"{}",
        headers={"Authorization": "Bearer tok-1234"},
    )

    assert resp.status == 502, await resp.text()
    body = await resp.text()
    # Never reflect the raw connector exception (socket path, OS errno
    # text) into the client-visible body.
    assert str(sock_path) not in body
    assert "errno" not in body.lower()


async def test_proxy_backend_socket_missing_returns_clean_502(
    aiohttp_client, router_app, router_module, router_env, monkeypatch,
) -> None:
    """Socket file gone entirely (e.g. runtime dir purged, or reaped
    between ``_ensure`` resolving it and the proxy's connect) —
    ``UnixClientConnectorError`` from the OTHER OS-level cause
    (``ENOENT`` instead of ``ECONNREFUSED``); both must land on the same
    clean 502.

    ``_ensure()``'s own ``sock.exists()`` freshness check would normally
    catch a missing socket and try to (re)start the unit first (a
    DIFFERENT, already-correct 504 path — not what this test is
    isolating). Stub ``_ensure`` itself to hand back the socket path
    unconditionally, mirroring the real race: ``_ensure`` resolved the
    path while the socket was still there; it's gone by the time
    ``_proxy_to_backend`` actually connects.
    """
    name = "proj-missing-sock"
    router_module._REGISTRY.register(name, str(router_env.root / "ws"))
    sock_path = router_env.sock_dir / name / "backend.sock"
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    sock_path.unlink(missing_ok=True)  # never existed for this test

    async def _fake_ensure(name_arg: str, role: str):
        return sock_path

    monkeypatch.setattr(router_module, "_ensure", _fake_ensure)
    router_module._agent_token_cache[name] = (9.9e18, {"tok-1234": "Admin"})

    client = await aiohttp_client(router_app)
    resp = await client.post(
        f"/agent-mcp/mcp/{name}",
        data=b"{}",
        headers={"Authorization": "Bearer tok-1234"},
    )

    assert resp.status == 502, await resp.text()
    body = await resp.text()
    assert str(sock_path) not in body
