"""Pentest R2-F1: close the cross-tenant project-EXISTENCE oracle on the
inside-lock TOCTOU EXCEPTION BACKSTOP that ``rename_project_handler``'s
``_REGISTRY.rename(...)`` atomic write falls back to — the class-sweep
miss of R1-F1 (PR #651).

R1-F1 threaded ``_deny_cross_tenant_project_read`` through the
OUTSIDE-lock name/alias collision checks and the INSIDE-lock alias-
collision RE-check in ``rename_project_handler``, but missed the
INSIDE-LOCK exception-handler backstop for the SAME call: when
``_REGISTRY.rename()`` itself raises ``ProjectNameTaken`` or
``AliasCollision`` (``new_name`` got claimed by a concurrent operation
BETWEEN the outside-lock check and the inside-lock atomic write — a real
race, because ``_ensure_lock`` keys on ``old_name``, so two renames with
DIFFERENT old-names racing the SAME new-name never serialise against
each other), the handler's ``except (ValueError, KeyError)`` block
returned the raw, UNGATED 409 straight to the caller — reopening the
R1-F1 oracle, probabilistically (triggered by winning/losing a race, not
a static hidden-project lookup).

Confirmed live repro: a non-member delegate (``system.projects.manage``
cap, zero project membership) racing a rename against a concurrent
sysadmin create of the same target name leaked the raw
``{"error":"name_taken", ...}`` 409 in ~2/8 attempts instead of the
gated uniform 404 ``unknown_project``.

Class-swept in this fix:

  1. rename's ``ProjectNameTaken`` backstop branch (confirmed
     live-exploitable via the real thread-park race below).
  2. rename's ``AliasCollision`` backstop branch, immediately below —
     same except block, identical shape. Reproduced here via a REAL
     concurrent ``add_alias`` landing inside the same stop-park window
     (not just a static exception-message stub).
  3. ``create_project_handler``'s own analogous ``register()`` backstop
     — NOT race-reachable today (no ``await`` between the outside check
     and ``register()``), fixed anyway as defense-in-depth. Isolated
     here by bypassing the (necessarily unreachable) outside check
     directly, since there is no real scheduling window to race.

Fix: thread the SAME ``_deny_cross_tenant_project_read`` escape hatch
used at the outside-lock checks + inside-lock alias RE-check into these
exception-handler branches too. Alias-collision branches resolve to the
alias OWNER's project name (mirroring the existing inside-lock alias
re-check) before gating, since that's what confirms existence. A
sysadmin, or a caller with a resolved role on the colliding project,
still gets the real 409 (happy path unchanged).
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import threading

import pytest

pytestmark = [pytest.mark.asyncio]

_ACCEPT = {"Accept": "application/vnd.agent-mcp.v1+json"}
_REST_HEADERS = {**_ACCEPT, "Content-Type": "application/json"}

_HIDDEN = "pentest-tenant-b"
_OWN = "own-visible-project"


# ── Helpers (mirror test_sec_r1f1_create_rename_name_oracle.py) ──────


def _identity_module():
    import agent_mcp.router.identity as identity

    identity.run_router_migrations_upgrade()
    return identity


def _seed_user(
    username: str,
    password: str = "passwordpassword",
    *,
    is_sysadmin: bool = False,
) -> str:
    """Create a user. The first-ever user is auto-promoted to sysadmin by the
    router bootstrap, so seed a throwaway sentinel sysadmin first when the
    table is empty to keep the real test user at ``is_sysadmin=0``."""
    identity = _identity_module()
    with identity._connect() as conn:
        is_empty = (
            conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is None
        )
    if is_empty and username != "__test_first_sysadmin":
        identity.create_user(
            username="__test_first_sysadmin",
            password="ignoredsentinelpassword",
        )
    user_id = identity.create_user(username=username, password=password)
    if is_sysadmin:
        with identity._connect() as conn:
            conn.execute(
                "UPDATE users SET is_sysadmin = 1 WHERE user_id = ?",
                (user_id,),
            )
    return user_id


def _seed_group(group_id: str, name: str) -> str:
    identity = _identity_module()
    with identity._connect() as conn:
        conn.execute(
            "INSERT INTO groups (group_id, name, is_sysadmin, created_at) "
            "VALUES (?, ?, 0, '2026-06-30T00:00:00')",
            (group_id, name),
        )
    return group_id


def _grant_capability(group_id: str, *caps: str) -> None:
    from agent_mcp.repositories import group_capability_repository

    group_capability_repository.replace(group_id, caps)


def _seed_project_membership(
    project: str,
    *,
    user_id: str | None = None,
    group_id: str | None = None,
    role: str = "operator",
) -> None:
    identity = _identity_module()
    with identity._connect() as conn:
        if user_id is not None:
            conn.execute(
                "INSERT INTO project_membership "
                "(project_name, user_id, role) VALUES (?, ?, ?)",
                (project, user_id, role),
            )
        else:
            conn.execute(
                "INSERT INTO project_membership "
                "(project_name, user_id, group_id, role) "
                "VALUES (?, NULL, ?, ?)",
                (project, group_id, role),
            )


async def _login(
    client, username: str, password: str = "passwordpassword",
) -> str:
    resp = await client.post(
        "/agent-mcp/login",
        data={"username": username, "password": password},
        allow_redirects=False,
    )
    assert resp.status == 303, await resp.text()
    set_cookie = resp.headers.get("Set-Cookie")
    assert set_cookie, "expected Set-Cookie on successful login"
    name_val = set_cookie.split(";", 1)[0]
    name, _, value = name_val.partition("=")
    assert name.strip() == "agent_mcp_session"
    return value.strip()


async def _delegated_client(aiohttp_client, router_app, *caps: str):
    """Log in a non-sysadmin operator 'alice' who carries ``caps`` via a group
    capability grant. Returns (client, cookie, alice_id, group_id)."""
    from agent_mcp.router import group_resolver

    alice_id = _seed_user("alice", is_sysadmin=False)
    group_id = _seed_group("g-delegated", "Delegated Admins")
    _grant_capability(group_id, *caps)
    group_resolver.add_group_member(group_id, member_user_id=alice_id)

    client = await aiohttp_client(router_app)
    cookie = await _login(client, "alice")
    return client, cookie, alice_id, group_id


async def _rename(client, cookie, project: str, new_name: str):
    return await client.patch(
        f"/agent-mcp/api/router/projects/{project}",
        data=json.dumps({"name": new_name, "grace_days": 7}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )


def _stub_systemctl_park_on_stop(
    unit_substr: str, stop_started: threading.Event, release: threading.Event,
):
    def _stub(*args: str) -> subprocess.CompletedProcess:
        verb = args[0] if args else ""
        unit = args[1] if len(args) > 1 else ""
        if verb == "stop" and unit_substr in unit:
            stop_started.set()
            release.wait(timeout=5)
        elif verb == "is-active":
            return subprocess.CompletedProcess(list(args), 3, "", "")
        return subprocess.CompletedProcess(list(args), 0, "", "")

    return _stub


# ── 1. rename ProjectNameTaken backstop — REAL race, non-member delegate ──


async def test_rename_delegate_loses_project_name_taken_race_gets_uniform_404(
    aiohttp_client, router_app, router_module, register_project,
    monkeypatch, tmp_path,
) -> None:
    """Confirmed live repro: a non-member delegate renames their own
    project onto a target name that a concurrent (sysadmin-owned) create
    claims first. ``_ensure_lock`` keys on old_name so the two never
    serialise; ``_REGISTRY.rename`` raises ``ProjectNameTaken`` inside the
    lock. The delegate has ZERO membership on the winning project, so the
    backstop must gate to the SAME uniform 404, not leak the raw 409.
    """
    monkeypatch.setenv("AGENT_MCP_TOKENS_DIR", str(tmp_path / "tokens"))
    register_project(_OWN, str(tmp_path / "ws" / "ws_own"))
    from agent_mcp.router import project_orchestrator as _po

    stop_started = threading.Event()
    release_stop = threading.Event()
    monkeypatch.setattr(
        _po,
        "_systemctl",
        _stub_systemctl_park_on_stop(_OWN, stop_started, release_stop),
    )

    client, cookie, alice_id, _ = await _delegated_client(
        aiohttp_client, router_app, "system.projects.manage",
    )
    _seed_project_membership(_OWN, user_id=alice_id, role="operator")

    # Seed + log in the winning (sysadmin) actor BEFORE the rename parks —
    # argon2 password hashing + the login round-trip are slow enough that
    # doing them INSIDE the stop-park window risks outrunning the stub's
    # ``release.wait(timeout=5)`` and self-releasing before this test calls
    # ``release_stop.set()``, which would let the delegate's rename win the
    # race instead of losing it (flaky-false-negative, not a fix bug).
    root_client = await aiohttp_client(router_app)
    _seed_user("root", is_sysadmin=True)
    root_cookie = await _login(root_client, "root")

    rename_task = asyncio.create_task(_rename(client, cookie, _OWN, "shared-target"))
    for _ in range(500):
        if stop_started.is_set():
            break
        await asyncio.sleep(0.01)
    assert stop_started.is_set(), "rename never reached its stop"

    # The sysadmin actor wins the race, claiming "shared-target" as a real
    # project the delegate has no membership on. Only the fast HTTP call
    # itself runs inside the park window now.
    create_resp = await root_client.post(
        "/agent-mcp/api/router/projects",
        data=json.dumps({"name": "shared-target"}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": root_cookie},
        allow_redirects=False,
    )
    assert create_resp.status == 201, await create_resp.text()

    release_stop.set()
    resp = await rename_task

    assert resp.status == 404, await resp.text()
    body = await resp.json()
    assert body["success"] is False
    assert body["error"] == "not_found"
    text = json.dumps(body).lower()
    assert "taken" not in text
    assert "already" not in text
    # No membership was silently granted, and the loser's own project is
    # untouched (no partial rename).
    from agent_mcp.router import group_resolver

    assert (
        group_resolver.resolve_user_project_role(alice_id, "shared-target")
        is None
    )
    assert router_module._REGISTRY.get(_OWN) is not None


async def test_rename_sysadmin_wins_project_name_taken_race_still_gets_real_409(
    aiohttp_client, router_app, router_module, register_project,
    monkeypatch, tmp_path,
) -> None:
    """Happy path: the SAME race, but the loser is a sysadmin (or a caller
    who can see the winning project) — must still get the real, informative
    409 ``name_taken``, matching PF-R37-1's original behaviour."""
    monkeypatch.setenv("AGENT_MCP_TOKENS_DIR", str(tmp_path / "tokens"))
    register_project("winsrc", str(tmp_path / "ws" / "ws_winsrc"))
    register_project("losesrc", str(tmp_path / "ws" / "ws_losesrc"))
    from agent_mcp.router import project_orchestrator as _po

    stop_started = threading.Event()
    release_stop = threading.Event()
    monkeypatch.setattr(
        _po,
        "_systemctl",
        _stub_systemctl_park_on_stop("losesrc", stop_started, release_stop),
    )

    client = await aiohttp_client(router_app)

    loser = asyncio.create_task(
        client.patch(
            "/agent-mcp/api/router/projects/losesrc",
            json={"name": "shared"},
            headers=_ACCEPT,
        )
    )
    for _ in range(500):
        if stop_started.is_set():
            break
        await asyncio.sleep(0.01)
    assert stop_started.is_set(), "loser rename never reached its stop"

    winner_resp = await client.patch(
        "/agent-mcp/api/router/projects/winsrc",
        json={"name": "shared"},
        headers=_ACCEPT,
    )
    assert winner_resp.status == 200, await winner_resp.text()

    release_stop.set()
    loser_resp = await loser
    assert loser_resp.status == 409, (
        f"expected the sysadmin-visible race-loser to keep its real 409, "
        f"got {loser_resp.status}: {await loser_resp.text()}"
    )
    body = await loser_resp.json()
    assert body.get("error") == "name_taken", body


# ── 2. rename AliasCollision backstop — REAL race, non-member delegate ────


async def test_rename_delegate_loses_alias_collision_race_gets_uniform_404(
    aiohttp_client, router_app, router_module, register_project,
    monkeypatch, tmp_path,
) -> None:
    """Same class of race, but the winner is a concurrent ``add_alias`` on
    a HIDDEN project rather than a create: the target name becomes an
    active alias of ``_HIDDEN`` while the delegate's rename is parked
    inside its stop, so ``_REGISTRY.rename`` raises ``AliasCollision``
    inside the lock. The delegate has no membership on ``_HIDDEN`` — must
    get the uniform 404, not the raw alias_collision 409.
    """
    monkeypatch.setenv("AGENT_MCP_TOKENS_DIR", str(tmp_path / "tokens"))
    register_project(_HIDDEN, str(tmp_path / "ws" / "ws_hidden"))
    register_project(_OWN, str(tmp_path / "ws" / "ws_own"))
    from agent_mcp.router import project_orchestrator as _po

    stop_started = threading.Event()
    release_stop = threading.Event()
    monkeypatch.setattr(
        _po,
        "_systemctl",
        _stub_systemctl_park_on_stop(_OWN, stop_started, release_stop),
    )

    client, cookie, alice_id, _ = await _delegated_client(
        aiohttp_client, router_app, "system.projects.manage",
    )
    _seed_project_membership(_OWN, user_id=alice_id, role="operator")

    rename_task = asyncio.create_task(
        _rename(client, cookie, _OWN, "alias-target")
    )
    for _ in range(500):
        if stop_started.is_set():
            break
        await asyncio.sleep(0.01)
    assert stop_started.is_set(), "rename never reached its stop"

    # A concurrent actor adds "alias-target" as an active alias of the
    # HIDDEN project WHILE the rename is parked — lands strictly between
    # the inside-lock alias RE-check (already passed) and the atomic
    # ``_REGISTRY.rename`` write below.
    router_module._REGISTRY.add_alias(_HIDDEN, "alias-target")

    release_stop.set()
    resp = await rename_task

    assert resp.status == 404, await resp.text()
    body = await resp.json()
    assert body["success"] is False
    assert body["error"] == "not_found"
    text = json.dumps(body).lower()
    assert "alias" not in text
    assert router_module._REGISTRY.get(_OWN) is not None


async def test_rename_sysadmin_loses_alias_collision_race_still_gets_real_409(
    aiohttp_client, router_app, router_module, register_project,
    monkeypatch, tmp_path,
) -> None:
    """Happy path: same race, but the caller can see the alias owner
    (sysadmin here) — must still get the real 409 ``alias_collision``."""
    monkeypatch.setenv("AGENT_MCP_TOKENS_DIR", str(tmp_path / "tokens"))
    register_project(_HIDDEN, str(tmp_path / "ws" / "ws_hidden2"))
    register_project(_OWN, str(tmp_path / "ws" / "ws_own2"))
    from agent_mcp.router import project_orchestrator as _po

    stop_started = threading.Event()
    release_stop = threading.Event()
    monkeypatch.setattr(
        _po,
        "_systemctl",
        _stub_systemctl_park_on_stop(_OWN, stop_started, release_stop),
    )

    _seed_user("root", is_sysadmin=True)
    client = await aiohttp_client(router_app)
    cookie = await _login(client, "root")

    rename_task = asyncio.create_task(
        _rename(client, cookie, _OWN, "alias-target2")
    )
    for _ in range(500):
        if stop_started.is_set():
            break
        await asyncio.sleep(0.01)
    assert stop_started.is_set(), "rename never reached its stop"

    router_module._REGISTRY.add_alias(_HIDDEN, "alias-target2")

    release_stop.set()
    resp = await rename_task

    assert resp.status == 409, await resp.text()
    body = await resp.json()
    assert body["error"] == "alias_collision"


# ── 3. create's register() backstop — defense-in-depth, not race-reachable ─


async def test_create_delegate_register_project_name_taken_backstop_gets_uniform_404(
    aiohttp_client, router_app, router_module, register_project, monkeypatch,
) -> None:
    """No real scheduling window exists for this one today (``register()``
    has no ``await`` between the outside check and the atomic write), so
    isolate the exception-handler branch directly: bypass the (necessarily
    unreachable) outside ``_validate_name`` check and force ``register()``
    to raise ``ProjectNameTaken`` for a name that resolves to a real,
    HIDDEN project the delegate has no membership on.
    """
    from agent_mcp.router import project_registry as _registry

    register_project(_HIDDEN)
    monkeypatch.setattr(router_module, "_validate_name", lambda *a, **k: None)

    def _boom(name, workspace, **extra):
        raise _registry.ProjectNameTaken(f"project {name!r} is already registered")

    monkeypatch.setattr(router_module._REGISTRY, "register", _boom)

    client, cookie, alice_id, _ = await _delegated_client(
        aiohttp_client, router_app, "system.projects.manage",
    )

    resp = await client.post(
        "/agent-mcp/api/router/projects",
        data=json.dumps({"name": _HIDDEN}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )

    assert resp.status == 404, await resp.text()
    body = await resp.json()
    assert body["success"] is False
    assert body["error"] == "not_found"
    text = json.dumps(body).lower()
    assert "already" not in text
    assert "registered" not in text


async def test_create_delegate_register_alias_collision_backstop_gets_uniform_404(
    aiohttp_client, router_app, router_module, register_project, monkeypatch,
) -> None:
    """Same isolation for the ``AliasCollision`` variant of the register()
    backstop: the outside alias-collision check (called once, before the
    ``try``) is bypassed via a stateful stub that only starts resolving to
    the HIDDEN owner on its SECOND call (the one inside the except block),
    simulating the alias having been added in the race window."""
    from agent_mcp.router import project_registry as _registry

    register_project(_HIDDEN)
    calls = {"n": 0}

    def _resolve_alias_stub(maybe_alias):
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return _HIDDEN

    monkeypatch.setattr(
        router_module._REGISTRY, "resolve_alias", _resolve_alias_stub,
    )

    def _boom(name, workspace, **extra):
        raise _registry.AliasCollision(
            f"name {name!r} is already an active alias for project {_HIDDEN!r}"
        )

    monkeypatch.setattr(router_module._REGISTRY, "register", _boom)

    client, cookie, alice_id, _ = await _delegated_client(
        aiohttp_client, router_app, "system.projects.manage",
    )

    resp = await client.post(
        "/agent-mcp/api/router/projects",
        data=json.dumps({"name": "some-fresh-slug"}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )

    assert resp.status == 404, await resp.text()
    body = await resp.json()
    assert body["success"] is False
    assert body["error"] == "not_found"
    assert "alias" not in json.dumps(body).lower()


async def test_create_sysadmin_register_backstop_still_gets_real_409(
    aiohttp_client, router_app, router_module, register_project, monkeypatch,
) -> None:
    """Happy path: the sysadmin still gets the real 409 through the same
    isolated backstop branch."""
    from agent_mcp.router import project_registry as _registry

    register_project(_HIDDEN)
    monkeypatch.setattr(router_module, "_validate_name", lambda *a, **k: None)

    def _boom(name, workspace, **extra):
        raise _registry.ProjectNameTaken(f"project {name!r} is already registered")

    monkeypatch.setattr(router_module._REGISTRY, "register", _boom)

    _seed_user("root", is_sysadmin=True)
    client = await aiohttp_client(router_app)
    cookie = await _login(client, "root")

    resp = await client.post(
        "/agent-mcp/api/router/projects",
        data=json.dumps({"name": _HIDDEN}),
        headers=_REST_HEADERS,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )

    assert resp.status == 409, await resp.text()
    body = await resp.json()
    assert body["error"] == "already_registered"
