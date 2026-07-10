"""PF-R37-1 — map EVERY ``_REGISTRY.rename`` failure mode to its correct
HTTP status, closing the rename/registry error-mapping class.

``rename_project_handler`` used to wrap ``_REGISTRY.rename`` in a single
``except (ValueError, KeyError)`` that collapsed EVERY failure to a 500.
But the registry raises semantically-distinct signals:

  * old_name not registered            → 404 not_registered
  * new_name is a registered PROJECT   → 409 name_taken
  * new_name is an active ALIAS        → 409 alias_collision
  * new_name is an invalid slug        → 400 invalid_name

The PF-R36-1 inside-lock re-checks (see test_sec_r36_lifecycle_parity)
cover the SERIALISED cases (two renames of the SAME old_name), but
``_ensure_lock`` keys on OLD_name, so two renames with DIFFERENT
old-names and the SAME new-name never serialise — the registry's atomic
``ProjectNameTaken`` guard is the ONLY backstop, and it previously
surfaced a 500. Same for a rename racing a CREATE of that new name.

RED against the pre-fix tree: the losing concurrent rename (and the
rename racing a create) 500s; GREEN → 409 name_taken.

Fix uses typed registry exceptions (``project_registry.ProjectNameTaken``
etc.) that subclass the built-in they replace, so the handler maps each
by type and reserves 500 for a genuine internal fault only.
"""

from __future__ import annotations

import asyncio
import subprocess
import threading

import pytest


pytestmark = [pytest.mark.asyncio]


_ACCEPT = {"Accept": "application/vnd.agent-mcp.v1+json"}


# Registry-layer typed-exception assertions live next to the other
# registry unit tests in ``test_project_registry.py`` (no event loop
# needed); this file pins the HTTP handler's status mapping end-to-end.


# ── Handler: sequential mapping regressions ─────────────────────────


async def test_rename_to_existing_project_name_returns_409(
    aiohttp_client, router_app, router_module, register_project,
    monkeypatch, tmp_path,
) -> None:
    """Renaming onto an already-registered project name → 409 name_taken
    (the outside-lock ``_validate_name`` probe), never a 500."""
    monkeypatch.setenv("AGENT_MCP_TOKENS_DIR", str(tmp_path / "tokens"))
    register_project("alpha")
    register_project("beta")

    client = await aiohttp_client(router_app)
    resp = await client.patch(
        "/agent-mcp/api/router/projects/alpha",
        json={"name": "beta"},
        headers=_ACCEPT,
    )
    assert resp.status == 409, await resp.text()
    body = await resp.json()
    assert body.get("error") == "name_taken", body


async def test_rename_unknown_old_name_returns_404(
    aiohttp_client, router_app, router_module, register_project,
    monkeypatch, tmp_path,
) -> None:
    """Renaming a project that isn't registered → 404 not_registered."""
    monkeypatch.setenv("AGENT_MCP_TOKENS_DIR", str(tmp_path / "tokens"))
    # Register at least one project so the app / migrations are wired.
    register_project("present")

    client = await aiohttp_client(router_app)
    resp = await client.patch(
        "/agent-mcp/api/router/projects/ghostproject",
        json={"name": "whatever"},
        headers=_ACCEPT,
    )
    assert resp.status == 404, await resp.text()
    body = await resp.json()
    assert body.get("error") == "not_registered", body


# ── Handler: the PF-R37-1 races — loser/racer gets 409, never 500 ───


async def test_concurrent_rename_same_new_name_loser_returns_409_not_500(
    aiohttp_client, router_app, router_module, register_project,
    monkeypatch, tmp_path,
) -> None:
    """Two concurrent renames with DIFFERENT old-names but the SAME new
    name. ``_ensure_lock`` keys on OLD_name so they DON'T serialise; the
    registry's atomic write does. The loser passes both its outside- and
    inside-lock probes (the new name isn't a project yet), then
    ``_REGISTRY.rename`` raises ``ProjectNameTaken`` because the winner's
    write already landed. That MUST surface a 409 name_taken, not a 500.

    Custom workspace basenames (``!= old_name``) so no on-disk workspace
    rename happens — the test isolates the registry error-mapping.
    """
    monkeypatch.setenv("AGENT_MCP_TOKENS_DIR", str(tmp_path / "tokens"))
    register_project("winsrc", str(tmp_path / "ws" / "ws_winsrc"))
    register_project("losesrc", str(tmp_path / "ws" / "ws_losesrc"))
    from agent_mcp.router import project_orchestrator as _po

    loser_stop_started = threading.Event()
    release_loser = threading.Event()

    def _stub_systemctl(*args: str) -> subprocess.CompletedProcess:
        verb = args[0] if args else ""
        unit = args[1] if len(args) > 1 else ""
        if verb == "stop" and "losesrc" in unit:
            # Park the loser INSIDE its stop — after its inside-lock probe
            # (which sees "shared" not-yet-registered) but before its
            # registry.rename — so the winner's write lands first.
            loser_stop_started.set()
            release_loser.wait(timeout=5)
        elif verb == "is-active":
            return subprocess.CompletedProcess(list(args), 3, "", "")
        return subprocess.CompletedProcess(list(args), 0, "", "")

    monkeypatch.setattr(router_module, "_systemctl", _stub_systemctl)
    monkeypatch.setattr(_po, "_systemctl", _stub_systemctl)

    client = await aiohttp_client(router_app)

    # Loser: losesrc -> shared. Parks in its stub systemctl stop.
    loser = asyncio.create_task(
        client.patch(
            "/agent-mcp/api/router/projects/losesrc",
            json={"name": "shared"},
            headers=_ACCEPT,
        )
    )
    for _ in range(500):
        if loser_stop_started.is_set():
            break
        await asyncio.sleep(0.01)
    assert loser_stop_started.is_set(), "loser rename never reached its stop"

    # Winner: winsrc -> shared. Its stop unit doesn't contain "losesrc" so
    # it runs to completion, claiming "shared" as a real project.
    winner_resp = await client.patch(
        "/agent-mcp/api/router/projects/winsrc",
        json={"name": "shared"},
        headers=_ACCEPT,
    )
    assert winner_resp.status == 200, await winner_resp.text()

    # Release the loser; its registry.rename now hits the already-taken name.
    release_loser.set()
    loser_resp = await loser
    assert loser_resp.status != 500, (
        "the losing concurrent rename must NOT surface a 500 — the "
        "_REGISTRY.rename ProjectNameTaken (winner already claimed the new "
        "name) must map to a 409 (PF-R37-1)"
    )
    assert loser_resp.status == 409, (
        f"expected 409, got {loser_resp.status}: {await loser_resp.text()}"
    )
    body = await loser_resp.json()
    assert body.get("error") == "name_taken", body


async def test_rename_racing_create_of_new_name_returns_409_not_500(
    aiohttp_client, router_app, router_module, register_project,
    monkeypatch, tmp_path,
) -> None:
    """A rename to NEW racing a CREATE of NEW. The rename passes its probes
    (NEW not registered yet), parks, the create registers NEW, and the
    rename's ``_REGISTRY.rename`` then raises ``ProjectNameTaken`` → 409,
    never a 500.
    """
    monkeypatch.setenv("AGENT_MCP_TOKENS_DIR", str(tmp_path / "tokens"))
    register_project("mover", str(tmp_path / "ws" / "ws_mover"))
    from agent_mcp.router import project_orchestrator as _po

    stop_started = threading.Event()
    release_stop = threading.Event()

    def _stub_systemctl(*args: str) -> subprocess.CompletedProcess:
        verb = args[0] if args else ""
        unit = args[1] if len(args) > 1 else ""
        if verb == "stop" and "mover" in unit:
            stop_started.set()
            release_stop.wait(timeout=5)
        elif verb == "is-active":
            return subprocess.CompletedProcess(list(args), 3, "", "")
        return subprocess.CompletedProcess(list(args), 0, "", "")

    monkeypatch.setattr(router_module, "_systemctl", _stub_systemctl)
    monkeypatch.setattr(_po, "_systemctl", _stub_systemctl)

    client = await aiohttp_client(router_app)

    # Rename mover -> fresh. Parks in its stop.
    rename = asyncio.create_task(
        client.patch(
            "/agent-mcp/api/router/projects/mover",
            json={"name": "fresh"},
            headers=_ACCEPT,
        )
    )
    for _ in range(500):
        if stop_started.is_set():
            break
        await asyncio.sleep(0.01)
    assert stop_started.is_set(), "rename never reached its stop"

    # Create "fresh" as a real project while the rename is parked.
    create_resp = await client.post(
        "/agent-mcp/api/router/projects",
        json={"name": "fresh"},
        headers=_ACCEPT,
    )
    assert create_resp.status == 201, await create_resp.text()

    release_stop.set()
    rename_resp = await rename
    assert rename_resp.status != 500, (
        "rename racing a create of the same new name must not 500 — the "
        "ProjectNameTaken from _REGISTRY.rename must map to 409 (PF-R37-1)"
    )
    assert rename_resp.status == 409, (
        f"expected 409, got {rename_resp.status}: {await rename_resp.text()}"
    )
    body = await rename_resp.json()
    assert body.get("error") == "name_taken", body
