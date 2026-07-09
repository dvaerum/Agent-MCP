"""SEC round-2 FINDING 3 [MED-HIGH] — grace_days bounds check.

Owner-authorised defensive review (2026-07-09). ``rename_project_handler``
parsed ``grace_days`` as an UNBOUNDED int, then ran destructive steps
(``systemctl stop`` → workspace rename → token rename) BEFORE calling
``_REGISTRY.rename`` — which computes ``now + timedelta(days=grace_days)``
and raises ``OverflowError`` for a huge value. The rollback
``except (ValueError, KeyError)`` does NOT catch ``OverflowError``, so
the project was left half-renamed (registry on the old name, disk on the
new) — a mid-rename brick. The handler now bounds ``grace_days`` to
``0..3650`` and returns 400 BEFORE any destructive step.
"""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.asyncio


_ACCEPT = {"Accept": "application/vnd.agent-mcp.v1+json"}


async def test_grace_days_overflow_returns_400_project_intact(
    aiohttp_client, router_app, router_module, register_project, systemctl_stub,
) -> None:
    """A huge ``grace_days`` must yield 400 with the project fully intact
    — no backend stop, no workspace rename, no registry change.

    The auto-attached sentinel operator is the first user (sysadmin), so
    it clears the ``system.projects.manage`` lifecycle gate.
    """
    ws = register_project("proj-a")
    client = await aiohttp_client(router_app)

    huge = 10 ** 18  # timedelta(days=10**18) → OverflowError
    resp = await client.patch(
        "/agent-mcp/api/router/projects/proj-a",
        json={"name": "proj-b", "grace_days": huge},
        headers=_ACCEPT,
        allow_redirects=False,
    )

    assert resp.status == 400, await resp.text()
    body = await resp.json()
    assert body["success"] is False

    # Registry untouched: old name still present, new name absent.
    assert router_module._REGISTRY.get("proj-a") is not None
    assert router_module._REGISTRY.get("proj-b") is None

    # No destructive systemd step ran.
    stop_count = systemctl_stub.counts.get(
        ("stop", "agent-mcp@proj-a.service"), 0,
    )
    assert stop_count == 0, "backend was stopped despite the 400"

    # Workspace dir not renamed.
    assert ws.exists(), "workspace dir vanished"
    assert not ws.with_name("proj-b").exists(), "workspace was renamed"


async def test_grace_days_upper_bound_ok(
    aiohttp_client, router_app, router_module, register_project,
) -> None:
    """The upper bound (3650) is accepted — a legitimate max grace."""
    register_project("proj-c")
    client = await aiohttp_client(router_app)

    resp = await client.patch(
        "/agent-mcp/api/router/projects/proj-c",
        json={"name": "proj-d", "grace_days": 3650},
        headers=_ACCEPT,
        allow_redirects=False,
    )

    assert resp.status == 200, await resp.text()
    assert router_module._REGISTRY.get("proj-d") is not None


async def test_grace_days_negative_returns_400(
    aiohttp_client, router_app, router_module, register_project,
) -> None:
    """A negative grace_days is rejected up front."""
    register_project("proj-e")
    client = await aiohttp_client(router_app)

    resp = await client.patch(
        "/agent-mcp/api/router/projects/proj-e",
        json={"name": "proj-f", "grace_days": -1},
        headers=_ACCEPT,
        allow_redirects=False,
    )

    assert resp.status == 400, await resp.text()
    assert router_module._REGISTRY.get("proj-e") is not None
    assert router_module._REGISTRY.get("proj-f") is None
