"""Security: project rename must migrate project_membership rows.

Owner-authorized defensive review (2026-07-09), FINDING AZ-R13-1 [MED].

``rename_project_handler`` (``router/admin_api.py``) renamed the project
(registry + workspace dir + token files) but NEVER migrated the
``project_membership`` rows keyed on the OLD ``project_name``. That column
is a bare TEXT column with no FK cascade, so a rename orphaned every
membership row under the old name. Two exploits follow:

  1. Member lockout — after ``rename(P -> P2)`` the members' grants still
     point at ``P``, which no longer resolves, so every member loses access
     to the project under its new name ``P2``.
  2. Cross-tenant privilege resurrection — re-create a NEW, unrelated
     project named ``P`` (once the rename's grace alias has lapsed, or via
     a fresh deployment) and the orphaned rows silently RESURRECT the old
     members' operator/viewer roles on that fresh project.

This is the RENAME sibling of the round-3 delete-purge fix (#283), which
class-swept delete/purge but missed rename.

Fix: in the rename handler, after the registry rename lands,
``UPDATE project_membership SET project_name = :new WHERE
project_name = :old`` so the authority rows follow the project.

Class-sweep (router.db): ``project_membership`` is the ONLY table keyed on
``project_name`` (the authority table). ``users`` / ``sessions`` /
``groups`` / ``group_membership`` / ``group_capability`` key on
user_id / group_id / session_id, not project_name. Per-project DATA
(tasks, memories, agents) lives in per-project SQLite DBs under the
workspace dir, which the rename already moves via ``os.rename``.
"""

from __future__ import annotations

import pytest


pytestmark = [pytest.mark.asyncio]


_ACCEPT = {"Accept": "application/vnd.agent-mcp.v1+json"}


def _membership_rows(project_name: str):
    from agent_mcp.router import identity

    with identity._connect() as conn:
        cur = conn.execute(
            "SELECT user_id, group_id, role FROM project_membership "
            "WHERE project_name = ?",
            (project_name,),
        )
        return cur.fetchall()


def _seed_membership(project_name: str, *, user_id=None, group_id=None,
                     role: str = "operator") -> None:
    from agent_mcp.router import identity

    identity.run_router_migrations_upgrade()
    with identity._connect() as conn:
        conn.execute(
            "INSERT INTO project_membership "
            "(project_name, user_id, group_id, role) VALUES (?, ?, ?, ?)",
            (project_name, user_id, group_id, role),
        )


def _make_group(group_id: str, name: str) -> None:
    from agent_mcp.router import identity

    identity.run_router_migrations_upgrade()
    with identity._connect() as conn:
        conn.execute(
            "INSERT INTO groups (group_id, name, is_sysadmin, created_at) "
            "VALUES (?, ?, 0, '2026-06-30T00:00:00')",
            (group_id, name),
        )


def _make_user(user_id: str, username: str) -> None:
    from agent_mcp.router import identity

    identity.run_router_migrations_upgrade()
    with identity._connect() as conn:
        conn.execute(
            "INSERT INTO users (user_id, username, created_at) "
            "VALUES (?, ?, '2026-06-30T00:00:00')",
            (user_id, username),
        )


async def test_rename_migrates_membership_to_new_name(
    aiohttp_client, router_app, register_project, monkeypatch, tmp_path,
) -> None:
    """Exploit 1 (member lockout): a member's grant must follow the rename.

    RED on origin/main — the rows stay keyed on the old name and the
    member is locked out under the new name.
    """
    monkeypatch.setenv("AGENT_MCP_TOKENS_DIR", str(tmp_path / "tokens"))

    register_project("proj-old")
    _make_user("u-mallory", "mallory")
    _make_group("g-admins", "Proj Admins")
    _seed_membership("proj-old", user_id="u-mallory", role="operator")
    _seed_membership("proj-old", group_id="g-admins", role="viewer")

    client = await aiohttp_client(router_app)
    resp = await client.patch(
        "/agent-mcp/api/router/projects/proj-old",
        json={"name": "proj-new"},
        headers=_ACCEPT,
    )
    assert resp.status == 200, await resp.text()

    # The member/group grants now resolve under the NEW name.
    new_rows = _membership_rows("proj-new")
    user_ids = {r["user_id"] for r in new_rows if r["user_id"]}
    group_ids = {r["group_id"] for r in new_rows if r["group_id"]}
    assert "u-mallory" in user_ids, f"membership lost on rename: {new_rows!r}"
    assert "g-admins" in group_ids, f"group grant lost on rename: {new_rows!r}"

    # And NOTHING is left orphaned under the old name.
    assert _membership_rows("proj-old") == []


async def test_rename_then_recreate_old_name_grants_no_residual_membership(
    aiohttp_client, router_app, register_project, monkeypatch, tmp_path,
) -> None:
    """Exploit 2 (privilege resurrection): re-creating the OLD name after a
    rename must NOT resurrect the prior members' roles.

    RED on origin/main — the orphaned rows under the old name resurrect on
    the fresh same-named project.
    """
    monkeypatch.setenv("AGENT_MCP_TOKENS_DIR", str(tmp_path / "tokens"))

    register_project("victim")
    _make_user("u-eve", "eve")
    _seed_membership("victim", user_id="u-eve", role="operator")

    client = await aiohttp_client(router_app)
    # Rename victim -> renamed, with zero grace so the old-name alias
    # doesn't block re-registration.
    resp = await client.patch(
        "/agent-mcp/api/router/projects/victim",
        json={"name": "renamed", "grace_days": 0},
        headers=_ACCEPT,
    )
    assert resp.status == 200, await resp.text()

    # Re-create a NEW, unrelated project reusing the OLD name.
    resp = await client.post(
        "/agent-mcp/api/router/projects",
        json={"name": "victim"},
        headers=_ACCEPT,
    )
    assert resp.status == 201, await resp.text()

    # The prior member 'eve' must NOT be a member of the fresh 'victim'.
    rows = _membership_rows("victim")
    user_ids = {r["user_id"] for r in rows}
    assert "u-eve" not in user_ids, f"privilege resurrection: {rows!r}"


async def test_rename_with_zero_memberships_still_succeeds(
    aiohttp_client, router_app, register_project, monkeypatch, tmp_path,
) -> None:
    """Regression: rename with no matching membership rows must succeed."""
    monkeypatch.setenv("AGENT_MCP_TOKENS_DIR", str(tmp_path / "tokens"))
    register_project("lonely")

    client = await aiohttp_client(router_app)
    resp = await client.patch(
        "/agent-mcp/api/router/projects/lonely",
        json={"name": "lonely-renamed"},
        headers=_ACCEPT,
    )
    assert resp.status == 200, await resp.text()


async def test_rename_leaves_unrelated_projects_membership_untouched(
    aiohttp_client, router_app, register_project, monkeypatch, tmp_path,
) -> None:
    """Regression: a rename must migrate ONLY the renamed project's rows;
    an unrelated project's memberships stay put."""
    monkeypatch.setenv("AGENT_MCP_TOKENS_DIR", str(tmp_path / "tokens"))

    register_project("moving")
    register_project("bystander")
    _make_user("u-carol", "carol")
    _seed_membership("moving", user_id="u-carol", role="operator")
    _seed_membership("bystander", user_id="u-carol", role="viewer")

    client = await aiohttp_client(router_app)
    resp = await client.patch(
        "/agent-mcp/api/router/projects/moving",
        json={"name": "moved"},
        headers=_ACCEPT,
    )
    assert resp.status == 200, await resp.text()

    # The bystander's grant is untouched (carol still a member of it).
    bystander_ids = {r["user_id"] for r in _membership_rows("bystander")}
    assert "u-carol" in bystander_ids
    # The moved project's grant followed the rename; the old name is empty.
    moved_ids = {r["user_id"] for r in _membership_rows("moved")}
    assert "u-carol" in moved_ids, "renamed project's membership was lost"
    moving_ids = {r["user_id"] for r in _membership_rows("moving")}
    assert "u-carol" not in moving_ids, "membership stranded under old name"
