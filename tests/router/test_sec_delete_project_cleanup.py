"""Security: project delete must purge membership + agent-token files.

Owner-authorized defensive review (2026-07-09), FINDING 2 [MED-HIGH].

``delete_project_handler`` (``router/admin_api.py``) did rmtree +
systemctl stop + ``_REGISTRY.unregister`` but NEVER touched router.db.
``project_membership.project_name`` is a bare TEXT column with no FK, so
membership rows (per-user AND per-group grants) survived a delete.
Re-creating a same-named project — the workspace path is deterministic —
silently restored every prior member's operator/viewer caps
("privilege resurrection"). Separately, ``<name>--*.token`` agent-token
files were cleaned only on rename, never on delete, leaking live agent
bearers on disk.

Fix: in the delete handler, ``DELETE FROM project_membership WHERE
project_name = ?`` and glob-delete the ``<name>--*.token`` files
(mirroring the rename path's token cleanup), idempotently.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.asyncio]


_ACCEPT = {"Accept": "application/vnd.agent-mcp.v1+json"}


def _membership_rows(router_module, project_name: str):
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


async def test_delete_purges_membership_and_token_files(
    aiohttp_client, router_app, router_module, register_project,
    monkeypatch, tmp_path,
) -> None:
    token_dir = tmp_path / "tokens"
    token_dir.mkdir()
    monkeypatch.setenv("AGENT_MCP_TOKENS_DIR", str(token_dir))

    register_project("doomed")
    # A per-user grant AND a per-group grant, both keyed on the project.
    _make_user("u-mallory", "mallory")
    _make_group("g-doomed", "Doomed Admins")
    _seed_membership("doomed", user_id="u-mallory", role="operator")
    _seed_membership("doomed", group_id="g-doomed", role="viewer")
    # Live agent-token files on disk for the project.
    (token_dir / "doomed--agent1.token").write_text("live-bearer-1\n")
    (token_dir / "doomed--agent2.token").write_text("live-bearer-2\n")
    # A sibling project's token must be left untouched.
    (token_dir / "safe--agent1.token").write_text("keep-me\n")

    # register_project also grants the sentinel operator, so >= our two.
    assert len(_membership_rows(router_module, "doomed")) >= 2

    client = await aiohttp_client(router_app)
    resp = await client.delete(
        "/agent-mcp/api/router/projects/doomed", headers=_ACCEPT,
    )
    assert resp.status == 200, await resp.text()

    # Membership rows (user + group) are gone.
    assert _membership_rows(router_module, "doomed") == []
    # The project's token files are gone; the sibling's remain.
    assert not (token_dir / "doomed--agent1.token").exists()
    assert not (token_dir / "doomed--agent2.token").exists()
    assert (token_dir / "safe--agent1.token").exists()


async def test_recreate_after_delete_grants_no_residual_membership(
    aiohttp_client, router_app, router_module, register_project,
    monkeypatch, tmp_path,
) -> None:
    monkeypatch.setenv("AGENT_MCP_TOKENS_DIR", str(tmp_path / "tokens"))

    register_project("phoenix")
    _make_user("u-eve", "eve")
    _seed_membership("phoenix", user_id="u-eve", role="operator")

    client = await aiohttp_client(router_app)
    resp = await client.delete(
        "/agent-mcp/api/router/projects/phoenix", headers=_ACCEPT,
    )
    assert resp.status == 200, await resp.text()

    # Re-create the same name (deterministic workspace path).
    resp = await client.post(
        "/agent-mcp/api/router/projects",
        json={"name": "phoenix"},
        headers=_ACCEPT,
    )
    assert resp.status == 201, await resp.text()

    # Prior member 'eve' must NOT be resurrected.
    rows = _membership_rows(router_module, "phoenix")
    user_ids = {r["user_id"] for r in rows}
    assert "u-eve" not in user_ids, (
        f"privilege resurrection: {rows!r}"
    )


async def test_delete_purges_runtime_dir_with_hmac_key(
    aiohttp_client, router_app, router_module, router_env, register_project,
    monkeypatch, tmp_path,
) -> None:
    """SC-3 [LOW]: the systemd unit preserves ``/run/agent-mcp/<name>/``
    across stop/start (``RuntimeDirectoryPreserve=yes``), so the
    ``forwarding_hmac`` key lingers there after a project delete. The
    delete handler must purge the runtime dir itself.
    """
    monkeypatch.setenv("AGENT_MCP_TOKENS_DIR", str(tmp_path / "tokens"))
    register_project("hmac-proj")

    # Lay down the per-project runtime dir the systemd unit would create,
    # with the preserved HMAC key + backend socket placeholder.
    runtime_dir = router_env.sock_dir / "hmac-proj"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "forwarding_hmac").write_bytes(b"x" * 32)
    (runtime_dir / "backend.sock").write_bytes(b"")
    # A sibling project's runtime dir must be left untouched.
    sibling = router_env.sock_dir / "keep-proj"
    sibling.mkdir(parents=True, exist_ok=True)
    (sibling / "forwarding_hmac").write_bytes(b"y" * 32)

    client = await aiohttp_client(router_app)
    resp = await client.delete(
        "/agent-mcp/api/router/projects/hmac-proj", headers=_ACCEPT,
    )
    assert resp.status == 200, await resp.text()

    # The deleted project's runtime dir (and its HMAC key) are gone.
    assert not runtime_dir.exists()
    # The sibling's runtime dir + key survive.
    assert (sibling / "forwarding_hmac").exists()


async def test_delete_runtime_dir_cleanup_is_idempotent(
    aiohttp_client, router_app, router_module, router_env, register_project,
    monkeypatch, tmp_path,
) -> None:
    """SC-3: delete must succeed even when the runtime dir was never
    created (ignore-if-absent)."""
    monkeypatch.setenv("AGENT_MCP_TOKENS_DIR", str(tmp_path / "tokens"))
    register_project("no-runtime")
    # Deliberately do NOT create sock_dir/no-runtime.
    assert not (router_env.sock_dir / "no-runtime").exists()

    client = await aiohttp_client(router_app)
    resp = await client.delete(
        "/agent-mcp/api/router/projects/no-runtime", headers=_ACCEPT,
    )
    assert resp.status == 200, await resp.text()


async def test_delete_membership_cleanup_is_idempotent(
    aiohttp_client, router_app, router_module, register_project,
    monkeypatch, tmp_path,
) -> None:
    # No membership rows, no token dir — delete must still succeed and
    # not raise on the cleanup path.
    monkeypatch.setenv("AGENT_MCP_TOKENS_DIR", str(tmp_path / "nonexistent"))
    register_project("empty")

    client = await aiohttp_client(router_app)
    resp = await client.delete(
        "/agent-mcp/api/router/projects/empty", headers=_ACCEPT,
    )
    assert resp.status == 200, await resp.text()
    assert _membership_rows(router_module, "empty") == []
