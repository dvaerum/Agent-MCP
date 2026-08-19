"""Pentest R3-F3: close the live TOCTOU on the ``active_conns`` guard in
``delete_project_handler`` / ``rename_project_handler`` / ``stop_project_handler``.

All three handlers refuse their destructive op with a clean 409
``active_sessions`` when ``active_conns.get(name, 0) > 0`` — but that
check ran BEFORE ``_ensure_lock`` was acquired and was never re-checked
inside the lock (unlike the existence/alias-collision checks in
``rename_project_handler``, which already got an inside-lock re-check —
see ``PF-R36-1`` in ``admin_api.py``). A client whose connection lands
in the real wall-clock window between the outside check passing and the
destructive ``systemctl stop`` actually executing gets its backend
socket killed out from under it — and (independently hardened in
``app.py``'s ``_proxy_to_backend``, see ``test_sec_r3f3_proxy_backend_gone.py``)
could previously surface as a raw unhandled 500 instead of a clean
error.

Confirmed live repro (barrier-synchronized concurrent HTTP race, delete
vs. a long-lived SSE delivery-stream connection): 4/16 delete trials and
2/8 rename trials crashed with a raw 500 + server traceback. A control
test (connection fully established BEFORE the delete request) correctly
got the clean 409 — confirming the guard logic is right, only the TIMING
was wrong.

Fix: re-check ``active_conns`` a second time INSIDE ``_ensure_lock``,
immediately before the destructive ``systemctl stop`` call — the SAME
idiom as ``PF-R36-1``'s inside-lock existence/alias-collision backstop.

Why the lock hold (not wall-clock racing) reproduces the race
deterministically: ``asyncio.Lock.acquire()`` only actually SUSPENDS
(yields to the event loop) when the lock is CONTENDED — the uncontended
fast path returns synchronously with no ``await``. So the only way a
connection can land in the gap between the outside check and the
destructive op is if something else holds the per-(name, "backend")
lock for a moment — which is exactly what a genuinely racing SSE
connection's own ``_ensure()`` call does (it takes the SAME lock).
Pre-acquiring the lock from the test and releasing it only after
incrementing ``active_conns`` deterministically reproduces that
interleaving without any wall-clock coin-flipping.
"""

from __future__ import annotations

import asyncio
import json

import pytest

pytestmark = [pytest.mark.asyncio]

_STRICT_ACCEPT = {
    "Accept": "application/vnd.agent-mcp.v1+json",
    "Content-Type": "application/json",
}


async def _wait_until_contended(
    lock: asyncio.Lock, *, timeout_iters: int = 2000,
) -> None:
    """Poll until ``lock`` has a real waiter queued.

    Deterministic, not wall-clock-dependent: the polling loop itself
    just gives the event loop enough turns to run the racing task up to
    its (genuinely blocking, since the caller pre-acquired the lock)
    ``await lock.acquire()`` — the assertion is on internal scheduling
    state, not on timing.
    """
    for _ in range(timeout_iters):
        if lock.locked() and lock._waiters and len(lock._waiters) > 0:
            return
        await asyncio.sleep(0.001)
    raise AssertionError(
        "lock was never contended — handler never reached _ensure_lock"
    )


# ── 1. delete_project_handler ────────────────────────────────────────


async def test_delete_toctou_race_connection_lands_before_stop_gets_clean_409(
    aiohttp_client, router_app, router_module, register_project,
) -> None:
    register_project("racer-del")
    lock = router_module._ensure_lock("racer-del", "backend")
    await lock.acquire()
    try:
        client = await aiohttp_client(router_app)
        task = asyncio.create_task(
            client.delete(
                "/agent-mcp/api/router/projects/racer-del",
                headers=_STRICT_ACCEPT,
            )
        )
        await _wait_until_contended(lock)
        # Simulate the connection establishing in the exact window the
        # OUTSIDE active_conns check cannot see: after it ran clean,
        # before the destructive stop actually executes.
        router_module.active_conns["racer-del"] = 1
    finally:
        lock.release()

    resp = await task
    assert resp.status == 409, await resp.text()
    body = await resp.json()
    assert body["success"] is False
    assert body["error"] == "active_sessions"
    assert body["active_connections"] == 1
    # The destructive op must NOT have run: still registered.
    assert router_module._REGISTRY.get("racer-del") is not None
    router_module.active_conns.pop("racer-del", None)


async def test_delete_happy_path_no_racing_connection_still_works(
    aiohttp_client, router_app, router_module, register_project,
) -> None:
    register_project("calm-del")
    client = await aiohttp_client(router_app)

    resp = await client.delete(
        "/agent-mcp/api/router/projects/calm-del",
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 200, await resp.text()
    body = await resp.json()
    assert body["success"] is True
    assert router_module._REGISTRY.get("calm-del") is None


# ── 2. rename_project_handler ────────────────────────────────────────


async def test_rename_toctou_race_connection_lands_before_stop_gets_clean_409(
    aiohttp_client, router_app, router_module, register_project,
) -> None:
    register_project("racer-ren")
    lock = router_module._ensure_lock("racer-ren", "backend")
    await lock.acquire()
    try:
        client = await aiohttp_client(router_app)
        task = asyncio.create_task(
            client.patch(
                "/agent-mcp/api/router/projects/racer-ren",
                data=json.dumps({"name": "racer-ren-2"}),
                headers=_STRICT_ACCEPT,
            )
        )
        await _wait_until_contended(lock)
        router_module.active_conns["racer-ren"] = 3
    finally:
        lock.release()

    resp = await task
    assert resp.status == 409, await resp.text()
    body = await resp.json()
    assert body["success"] is False
    assert body["error"] == "active_sessions"
    assert body["active_connections"] == 3
    # The rename must NOT have happened.
    assert router_module._REGISTRY.get("racer-ren") is not None
    assert router_module._REGISTRY.get("racer-ren-2") is None
    router_module.active_conns.pop("racer-ren", None)


async def test_rename_happy_path_no_racing_connection_still_works(
    aiohttp_client, router_app, router_module, register_project,
) -> None:
    register_project("calm-ren")
    client = await aiohttp_client(router_app)

    resp = await client.patch(
        "/agent-mcp/api/router/projects/calm-ren",
        data=json.dumps({"name": "calm-ren-2"}),
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 200, await resp.text()
    body = await resp.json()
    assert body["success"] is True
    assert router_module._REGISTRY.get("calm-ren-2") is not None


# ── 3. stop_project_handler ──────────────────────────────────────────


async def test_stop_toctou_race_connection_lands_before_stop_gets_clean_409(
    aiohttp_client, router_app, router_module, register_project,
) -> None:
    register_project("racer-stop")
    lock = router_module._ensure_lock("racer-stop", "backend")
    await lock.acquire()
    try:
        client = await aiohttp_client(router_app)
        task = asyncio.create_task(
            client.post(
                "/agent-mcp/api/router/projects/racer-stop/stop",
                data="{}",
                headers=_STRICT_ACCEPT,
            )
        )
        await _wait_until_contended(lock)
        router_module.active_conns["racer-stop"] = 1
    finally:
        lock.release()

    resp = await task
    assert resp.status == 409, await resp.text()
    body = await resp.json()
    assert body["success"] is False
    assert body["error"] == "active_sessions"
    assert body["active_connections"] == 1
    # The project must still be registered (stop only touches runtime
    # state, but confirm the guard fired before any systemctl stop).
    assert router_module._REGISTRY.get("racer-stop") is not None
    router_module.active_conns.pop("racer-stop", None)


async def test_stop_happy_path_no_racing_connection_still_works(
    aiohttp_client, router_app, register_project,
) -> None:
    register_project("calm-stop")
    client = await aiohttp_client(router_app)

    resp = await client.post(
        "/agent-mcp/api/router/projects/calm-stop/stop",
        data="{}",
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 200, await resp.text()
    body = await resp.json()
    assert body["success"] is True
    assert body["stopped"] == "calm-stop"


async def test_stop_rejects_already_established_connection(
    aiohttp_client, router_app, router_module, register_project,
) -> None:
    """The pre-existing OUTSIDE-lock guard, established BEFORE the
    request starts (no race at all) — mirrors the delete/rename
    coverage in ``test_unregister_delete_workspace.py`` /
    ``test_rename_and_aliases.py``, added here for stop since no
    equivalent existed."""
    register_project("busy-stop")
    router_module.active_conns["busy-stop"] = 2
    try:
        client = await aiohttp_client(router_app)
        resp = await client.post(
            "/agent-mcp/api/router/projects/busy-stop/stop",
            data="{}",
            headers=_STRICT_ACCEPT,
        )
        assert resp.status == 409, await resp.text()
        body = await resp.json()
        assert body["error"] == "active_sessions"
        assert body["active_connections"] == 2
    finally:
        router_module.active_conns.pop("busy-stop", None)
