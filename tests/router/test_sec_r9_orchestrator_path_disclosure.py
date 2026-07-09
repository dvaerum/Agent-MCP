"""SC-R9-1 — socket-poll-timeout branch of ``_ensure`` must not leak
server-side internals to the client.

Round 8 (SC-R8-2, #329) genericised the *systemctl-spawn-failure*
branch of ``project_orchestrator._ensure`` so a project MEMBER
warm-starting a broken backend no longer sees raw systemd stderr
(unit-file paths, exec-step details) in the HTTP response. The ADJACENT
*socket-poll-timeout* branch in the same ``_ensure`` was left reflecting
raw internals:

    reason = f"{unit} did not create {sock} within ~{attempts*0.1:.0f} s"
    ensure_failures[(name, role)] = (time.monotonic(), reason)
    raise web.HTTPGatewayTimeout(reason=reason)

``unit`` = ``agent-mcp@<name>.service`` and ``sock`` =
``$AGENT_MCP_SOCK_DIR/<name>/backend.sock`` — an ABSOLUTE server-side
filesystem path (``/run/...`` in prod). That ``reason`` goes into the
504 status line AND is stored in ``ensure_failures`` so the whole
cooldown window replays it to every subsequent caller.

Reachability is identical to the branch SC-R8-2 fixed: any project
MEMBER can warm-start a slow/broken backend and trip the poll timeout.

Fix (mirror SC-R8-2 exactly): log the detailed phrase server-side and
return/store a GENERIC client reason (``"backend not ready"``) so the
504 status line and the cooldown-replay 504 both stay generic.

These tests force the socket-poll-timeout path (the backend unit
"starts" but never binds its UDS; ``AGENT_MCP_ENSURE_SOCKET_ATTEMPTS=1``
from the router_env fixture caps the wait at ~0.1 s) and assert:

  1. the raised ``HTTPGatewayTimeout.reason`` is the generic string —
     no ``agent-mcp@``, no ``.sock``, no absolute path;
  2. the stored ``ensure_failures`` reason (replayed for the whole
     cooldown window) is likewise generic;
  3. the detailed unit+sock phrase WAS logged server-side (so operators
     keep the diagnostic).

RED against origin/main (raw path leaked into reason + no server log);
GREEN after the fix. A regression case confirms a successful ensure
(socket appears) still returns the UDS path normally.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from aiohttp import web


pytestmark = pytest.mark.asyncio

_GENERIC = "backend not ready"


def _assert_generic(value: str, *, where: str) -> None:
    """A client-facing reason must carry no server-side internals."""
    assert value == _GENERIC, f"{where} not generic: {value!r}"
    # Belt-and-suspenders: even if the constant changes, none of these
    # server-side tokens may appear in a client-facing reason.
    assert "agent-mcp@" not in value, f"{where} leaks unit: {value!r}"
    assert ".sock" not in value, f"{where} leaks socket file: {value!r}"
    assert "/" not in value, f"{where} leaks a path: {value!r}"
    assert "did not create" not in value, f"{where} leaks internals: {value!r}"


async def _start_uds_backend(sock_path: Path) -> web.AppRunner:
    """Bind a no-op aiohttp app to ``sock_path`` (regression case)."""
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    sock_path.unlink(missing_ok=True)
    app = web.Application()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.UnixSite(runner, str(sock_path))
    await site.start()
    return runner


async def test_socket_timeout_reason_is_generic(
    router_module, router_env, systemctl_stub, caplog,
) -> None:
    """The 504 raised when the UDS never appears carries the generic
    reason, not the ``agent-mcp@<name>.service did not create
    <abs-sock-path>`` phrase."""
    name = "slow-backend"
    router_module._REGISTRY.register(name, str(router_env.root / "ws"))
    router_module._clear_ensure_failures()

    with caplog.at_level(
        logging.ERROR, logger="agent_mcp.router.project_orchestrator",
    ):
        with pytest.raises(web.HTTPGatewayTimeout) as excinfo:
            await router_module._ensure(name, "backend")

    _assert_generic(excinfo.value.reason, where="raised 504 reason")

    # The detailed unit+sock phrase MUST be logged server-side so
    # operators keep the diagnostic the client no longer receives.
    unit = f"agent-mcp@{name}.service"
    logged = "\n".join(
        r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR
    )
    assert unit in logged and ".sock" in logged, (
        "socket-timeout detail (unit + sock path) must be logged "
        f"server-side; error log was: {logged!r}"
    )


async def test_socket_timeout_cached_reason_is_generic(
    router_module, router_env, systemctl_stub,
) -> None:
    """The reason stored in ``ensure_failures`` — replayed as the 504
    for every caller inside the cooldown window — must also be generic,
    otherwise the leak survives via the cooldown replay path."""
    name = "slow-backend-cache"
    router_module._REGISTRY.register(name, str(router_env.root / "ws"))
    router_module._clear_ensure_failures()

    with pytest.raises(web.HTTPGatewayTimeout):
        await router_module._ensure(name, "backend")

    entry = router_module.ensure_failures.get((name, "backend"))
    assert entry is not None, "socket timeout must cache a failure entry"
    _assert_generic(entry[1], where="cached ensure_failures reason")

    # The cooldown-replay 504 (short-circuit branch) must also stay
    # generic — it re-raises the stored reason verbatim.
    with pytest.raises(web.HTTPGatewayTimeout) as excinfo:
        await router_module._ensure(name, "backend")
    _assert_generic(excinfo.value.reason, where="cooldown-replay 504 reason")


async def test_successful_ensure_still_returns_socket(
    router_module, router_env, systemctl_stub,
) -> None:
    """Regression: when the UDS does appear, ``_ensure`` returns the
    socket path normally and caches no failure."""
    # Short name: the UDS path (tmp dir + name + backend.sock) must stay
    # under the AF_UNIX 108-byte limit for the real bind below.
    name = "ok"
    router_module._REGISTRY.register(name, str(router_env.root / "ws"))
    router_module._clear_ensure_failures()

    sock = router_env.sock_dir / name / "backend.sock"
    runner = await _start_uds_backend(sock)
    try:
        path = await router_module._ensure(name, "backend")
    finally:
        await runner.cleanup()

    assert path == sock
    assert (name, "backend") not in router_module.ensure_failures
