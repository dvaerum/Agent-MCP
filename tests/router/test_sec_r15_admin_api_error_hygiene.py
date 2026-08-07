"""SEC-R15 — admin-API error-body information-disclosure hardening.

Two sibling error-genericization findings on the router admin surface,
both in the same class as already-fixed sites (SC-R8-2 at
``project_orchestrator._ensure``; SD-R6-2 at the rename handler):

SD-R15-1 (LOW-MED) — systemctl stderr reflection
  ``stop_project_handler`` (``router/admin_api.py``) returned
  ``message=f"systemctl stop {unit} failed: {r.stderr.strip()}"`` in
  its 500 envelope, leaking the unit-file name/path plus the raw
  systemd exec-step detail ("Failed at step EXEC …", ``/nix/store``
  unit paths) to any caller who can reach the delegatable
  ``system.projects.manage`` cap. The stderr is now logged
  server-side; the client gets a static message.

SD-R15-2 (LOW) — absolute workspace-path reflection
  ``create_project_handler`` returned
  ``message=f"could not create workspace {workspace}: {e.strerror}"``
  on an ``mkdir`` ``OSError``, leaking the resolved ABSOLUTE workspace
  path (server home dir / username). The path is now logged
  server-side; the client message drops it and keeps only the generic
  OS category (``e.strerror``), mirroring the already-hardened rename
  handler.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.asyncio


_STRICT_ACCEPT = {"Accept": "application/vnd.agent-mcp.v1+json"}


# ── SD-R15-1: systemctl stderr must not reach the client body ────────


async def test_stop_project_does_not_reflect_systemctl_stderr(
    aiohttp_client, router_app, router_module, register_project, monkeypatch,
) -> None:
    """A failed ``systemctl stop`` MUST NOT reflect the raw stderr (unit
    paths, exec-step detail) into the 500 client envelope."""
    from agent_mcp.router import project_orchestrator as _po

    register_project("leaky")

    secret = "/nix/store/SECRET-unit-path/agent-mcp-leaky-backend.service"

    def _fake_systemctl(*args: str) -> subprocess.CompletedProcess:
        verb = args[0] if args else ""
        if verb == "is-active":
            # Force the "unit is active" branch so the stop is attempted.
            return subprocess.CompletedProcess(
                args=list(args), returncode=0, stdout="", stderr="",
            )
        if verb == "stop":
            return subprocess.CompletedProcess(
                args=list(args), returncode=1, stdout="",
                stderr=f"Failed at step EXEC spawning {secret}: No such file",
            )
        return subprocess.CompletedProcess(
            args=list(args), returncode=0, stdout="", stderr="",
        )

    monkeypatch.setattr(_po, "_systemctl", _fake_systemctl)

    client = await aiohttp_client(router_app)
    resp = await client.post(
        "/agent-mcp/api/router/projects/leaky/stop",
        data="{}",
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 500
    text = await resp.text()
    assert secret not in text, f"systemctl stderr leaked into body: {text!r}"
    assert "Failed at step EXEC" not in text, (
        f"systemctl exec-step detail leaked into body: {text!r}"
    )
    # The systemd unit name itself must not be echoed either.
    assert "leaky" not in text, (
        f"unit/project name leaked into body: {text!r}"
    )


# ── SD-R15-2: absolute workspace path must not reach the client body ──


async def test_create_project_does_not_reflect_absolute_workspace_path(
    aiohttp_client, router_app, router_env, monkeypatch,
) -> None:
    """An ``mkdir`` ``OSError`` MUST NOT reflect the resolved ABSOLUTE
    workspace path (server home dir / username) into the 500 body."""
    import json

    name = "path-leak"
    orig_mkdir = Path.mkdir

    def _fake_mkdir(self: Path, *a, **k):
        # Only the workspace dir being created trips the error; other
        # mkdir calls during request handling proceed normally.
        if self.name == name:
            raise OSError(13, "Permission denied")
        return orig_mkdir(self, *a, **k)

    monkeypatch.setattr(Path, "mkdir", _fake_mkdir)

    client = await aiohttp_client(router_app)
    resp = await client.post(
        "/agent-mcp/api/router/projects",
        data=json.dumps({"name": name}),
        headers=_STRICT_ACCEPT,
        allow_redirects=False,
    )

    assert resp.status == 500
    body = await resp.json()
    assert body["success"] is False
    text = await resp.text()
    # The workspace resolves under the test root (an absolute path
    # standing in for the server's home dir); it must not be echoed.
    assert str(router_env.root) not in text, (
        f"absolute workspace path leaked into body: {text!r}"
    )
