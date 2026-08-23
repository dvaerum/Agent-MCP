"""Tests for the router-level identity store (Phase 1 PR B).

These tests exercise the pure-Python `agent_mcp.router.identity`
surface — password hashing, user/session/project_membership CRUD,
env-var bootstrap, retroactive project_membership for the first
operator — and the `agent-mcp router create-operator` CLI subcommand.

No HTTP routes are exercised here; those land in PR C/D.

Isolation strategy: every test gets a tmp router.db via
`AGENT_MCP_ROUTER_DB`, and most tests that touch the project
registry also point `AGENT_MCP_PROJECTS_FILE` at a tmp file. The
identity module is reloaded between tests so its module-level
`PasswordHasher` (if any) stays cheap and the DB path resolves
against the patched env.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def router_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Tmp router.db path, env-injected, fresh module import."""
    db_path = tmp_path / "router.db"
    monkeypatch.setenv("AGENT_MCP_ROUTER_DB", str(db_path))
    # Point the project registry at a tmp file so any code path that
    # peeks at it (retroactive project_membership in particular) sees
    # an empty project list unless the test explicitly populates it.
    projects_file = tmp_path / "projects.local.json"
    monkeypatch.setenv("AGENT_MCP_PROJECTS_FILE", str(projects_file))
    # Drop cached imports so module-level state re-resolves against
    # the patched env.
    for mod in [
        "agent_mcp.router.identity",
        "agent_mcp.router.migrations_runner",
        "agent_mcp.router.project_registry",
    ]:
        sys.modules.pop(mod, None)
    return db_path


@pytest.fixture
def identity(router_db: Path):
    """Freshly imported `agent_mcp.router.identity`."""
    import agent_mcp.router.identity as identity

    importlib.reload(identity)
    identity.init_router_db()
    return identity


@pytest.fixture
def populated_projects(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[str]:
    """Seed the projects.local.json with three projects."""
    projects_file = Path(os.environ["AGENT_MCP_PROJECTS_FILE"])
    workspaces = tmp_path / "workspaces"
    workspaces.mkdir(exist_ok=True)
    data = {}
    names = ["alpha", "beta", "gamma"]
    for n in names:
        wd = workspaces / n
        wd.mkdir()
        data[n] = {"workspace": str(wd), "aliases": []}
    projects_file.write_text(json.dumps(data))
    return names


# ── Password hashing ────────────────────────────────────────────────


def test_hash_and_verify_password(identity) -> None:
    h = identity.hash_password("correct horse battery staple")
    assert isinstance(h, str)
    assert h.startswith("$argon2id$"), (
        f"expected argon2id-encoded hash, got: {h[:30]}…"
    )
    assert identity.verify_password(h, "correct horse battery staple") is True


def test_verify_wrong_password_rejects(identity) -> None:
    h = identity.hash_password("right one")
    assert identity.verify_password(h, "wrong one") is False


def test_hash_password_produces_distinct_hashes(identity) -> None:
    # argon2 salts per call — two hashes of the same password must
    # differ but both verify.
    h1 = identity.hash_password("same")
    h2 = identity.hash_password("same")
    assert h1 != h2
    assert identity.verify_password(h1, "same")
    assert identity.verify_password(h2, "same")


# ── User CRUD ───────────────────────────────────────────────────────


def test_create_user_assigns_user_id(identity) -> None:
    user_id = identity.create_user(username="alice", password="hunter2")
    assert isinstance(user_id, str)
    assert len(user_id) == 16
    int(user_id, 16)  # 16-hex sanity


def test_create_user_unique_username(identity) -> None:
    identity.create_user(username="bob", password="x")
    with pytest.raises(identity.UsernameAlreadyExistsError):
        identity.create_user(username="bob", password="y")


def test_get_user_by_username_returns_record(identity) -> None:
    uid = identity.create_user(
        username="carol", password="pw", email="c@example.com"
    )
    row = identity.get_user_by_username("carol")
    assert row is not None
    assert row["user_id"] == uid
    assert row["username"] == "carol"
    assert row["email"] == "c@example.com"
    # Stored password is hashed, never plaintext.
    assert row["password_hash"] != "pw"
    assert identity.verify_password(row["password_hash"], "pw")
    assert row["created_at"]
    assert row["last_login_at"] is None


def test_get_user_by_username_missing(identity) -> None:
    assert identity.get_user_by_username("nobody") is None


# ── Sessions ────────────────────────────────────────────────────────


def test_session_lifecycle(identity) -> None:
    uid = identity.create_user(username="dave", password="pw")
    sid = identity.create_session(uid)
    assert isinstance(sid, str)
    assert len(sid) == 32
    int(sid, 16)

    row = identity.get_session(sid)
    assert row is not None
    assert row["user_id"] == uid

    identity.delete_session(sid)
    assert identity.get_session(sid) is None


def test_session_last_used_at_slides(identity) -> None:
    uid = identity.create_user(username="erin", password="pw")
    sid = identity.create_session(uid)
    first = identity.get_session(sid)
    assert first is not None
    # Wait a beat so the ISO timestamps differ. We use millisecond
    # precision in the schema, so a small sleep is enough.
    time.sleep(0.02)
    second = identity.get_session(sid)
    assert second is not None
    assert second["last_used_at"] >= first["last_used_at"]
    assert second["last_used_at"] != first["last_used_at"]


def test_get_session_expired_returns_none(identity) -> None:
    uid = identity.create_user(username="frank", password="pw")
    # Negative lifetime → already-expired session.
    sid = identity.create_session(uid, lifetime_days=-1)
    assert identity.get_session(sid) is None


def test_prune_expired_sessions(identity) -> None:
    uid = identity.create_user(username="gail", password="pw")
    fresh = identity.create_session(uid, lifetime_days=30)
    stale = identity.create_session(uid, lifetime_days=-1)
    deleted = identity.prune_expired_sessions()
    assert deleted == 1
    # Fresh still retrievable.
    assert identity.get_session(fresh) is not None
    # Stale gone after prune (independent of the get-side expiry check).
    # Re-call to confirm idempotence.
    assert identity.prune_expired_sessions() == 0
    # And a direct DB check that stale is really gone.
    with identity._connect() as conn:
        cur = conn.execute(
            "SELECT 1 FROM sessions WHERE session_id = ?", (stale,)
        )
        assert cur.fetchone() is None


# ── Project membership ─────────────────────────────────────────────


def test_add_and_list_project_membership(identity) -> None:
    uid = identity.create_user(username="hank", password="pw")
    identity.add_project_membership(uid, "alpha")
    identity.add_project_membership(uid, "beta")
    assert sorted(identity.list_user_projects(uid)) == ["alpha", "beta"]


def test_add_project_membership_is_idempotent(identity) -> None:
    uid = identity.create_user(username="iris", password="pw")
    identity.add_project_membership(uid, "alpha")
    identity.add_project_membership(uid, "alpha")
    assert identity.list_user_projects(uid) == ["alpha"]


def test_list_user_projects_isolated_per_user(identity) -> None:
    a = identity.create_user(username="jane", password="pw")
    b = identity.create_user(username="kyle", password="pw")
    identity.add_project_membership(a, "alpha")
    identity.add_project_membership(b, "beta")
    assert identity.list_user_projects(a) == ["alpha"]
    assert identity.list_user_projects(b) == ["beta"]


# ── First-user retroactive project membership ──────────────────────


def test_first_user_gets_membership_in_existing_projects(
    identity, populated_projects: list[str]
) -> None:
    """When the users table is empty AND projects exist, the first
    create_user call retroactively grants membership in every
    project."""
    uid = identity.create_user(username="root_op", password="pw")
    assert sorted(identity.list_user_projects(uid)) == sorted(populated_projects)


def test_subsequent_users_get_no_automatic_membership(
    identity, populated_projects: list[str]
) -> None:
    """Only the FIRST user gets retroactive memberships; everyone
    else starts with none and gets explicit assignments later."""
    identity.create_user(username="first", password="pw")
    second_uid = identity.create_user(username="second", password="pw")
    assert identity.list_user_projects(second_uid) == []


def test_first_user_with_no_projects_has_empty_membership(identity) -> None:
    """No projects exist → the first user just gets none. No errors,
    no crash, just an empty list."""
    uid = identity.create_user(username="solo", password="pw")
    assert identity.list_user_projects(uid) == []


# ── Bootstrap env-var path ─────────────────────────────────────────


def test_env_var_bootstrap(
    monkeypatch: pytest.MonkeyPatch, router_db: Path
) -> None:
    """Setting AGENT_MCP_BOOTSTRAP_USERNAME + ..._PASSWORD before
    init_router_db() creates the first operator AND clears the env
    vars so they don't leak to subprocess spawns."""
    monkeypatch.setenv("AGENT_MCP_BOOTSTRAP_USERNAME", "boot_op")
    monkeypatch.setenv("AGENT_MCP_BOOTSTRAP_PASSWORD", "boot_pw_12chars")

    import agent_mcp.router.identity as identity

    importlib.reload(identity)
    identity.init_router_db()

    row = identity.get_user_by_username("boot_op")
    assert row is not None
    assert identity.verify_password(row["password_hash"], "boot_pw_12chars")

    # Env vars must be gone — they're a secret and we don't want them
    # to flow into any agent subprocess spawned later.
    assert "AGENT_MCP_BOOTSTRAP_USERNAME" not in os.environ
    assert "AGENT_MCP_BOOTSTRAP_PASSWORD" not in os.environ


def test_env_var_bootstrap_skipped_when_users_exist(
    monkeypatch: pytest.MonkeyPatch, identity
) -> None:
    """If the users table already has rows, the env-var bootstrap is
    a no-op — we never overwrite an existing operator."""
    identity.create_user(username="pre_existing", password="x")
    monkeypatch.setenv("AGENT_MCP_BOOTSTRAP_USERNAME", "intruder")
    monkeypatch.setenv("AGENT_MCP_BOOTSTRAP_PASSWORD", "intruder_pw")

    # Second init_router_db() with bootstrap env set + pre-existing
    # user → bootstrap is skipped (no new user) but the env vars are
    # still cleared (defence-in-depth so an operator who set the var
    # by mistake doesn't have it leak to subprocesses on re-init).
    identity.init_router_db()
    assert identity.get_user_by_username("intruder") is None
    # The pre-existing user is untouched.
    assert identity.get_user_by_username("pre_existing") is not None


def test_env_var_bootstrap_requires_both_vars(
    monkeypatch: pytest.MonkeyPatch, router_db: Path
) -> None:
    """Username without password (or vice versa) does NOT bootstrap.
    The operator probably typoed the var name; failing silently here
    is safer than creating a user with an empty password."""
    monkeypatch.setenv("AGENT_MCP_BOOTSTRAP_USERNAME", "lonely")
    monkeypatch.delenv("AGENT_MCP_BOOTSTRAP_PASSWORD", raising=False)

    import agent_mcp.router.identity as identity

    importlib.reload(identity)
    identity.init_router_db()
    assert identity.get_user_by_username("lonely") is None


def test_env_var_bootstrap_grants_existing_projects(
    monkeypatch: pytest.MonkeyPatch,
    router_db: Path,
    tmp_path: Path,
) -> None:
    """The first operator (whether bootstrapped via env, CLI, or
    wizard) gets membership in every existing project — same code
    path as `test_first_user_gets_membership_in_existing_projects`,
    here driven by the env-var bootstrap path."""
    projects_file = Path(os.environ["AGENT_MCP_PROJECTS_FILE"])
    workspaces = tmp_path / "workspaces"
    workspaces.mkdir()
    p = workspaces / "preexisting"
    p.mkdir()
    projects_file.write_text(
        json.dumps({"preexisting": {"workspace": str(p), "aliases": []}})
    )

    monkeypatch.setenv("AGENT_MCP_BOOTSTRAP_USERNAME", "ops")
    monkeypatch.setenv("AGENT_MCP_BOOTSTRAP_PASSWORD", "ops_pw_12chars")

    import agent_mcp.router.identity as identity

    importlib.reload(identity)
    identity.init_router_db()

    row = identity.get_user_by_username("ops")
    assert row is not None
    assert identity.list_user_projects(row["user_id"]) == ["preexisting"]


# ── CLI subcommand ─────────────────────────────────────────────────


def test_cli_create_operator(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """`agent-mcp router create-operator --username … --password-stdin`
    creates the user end-to-end via the same code path as the env-var
    bootstrap."""
    db_path = tmp_path / "router.db"
    projects_file = tmp_path / "projects.local.json"
    projects_file.write_text("{}")

    env = os.environ.copy()
    env["AGENT_MCP_ROUTER_DB"] = str(db_path)
    env["AGENT_MCP_PROJECTS_FILE"] = str(projects_file)
    # Avoid the CLI's autoload from a project .env masking the test
    # env. Set OPENAI_API_KEY to "" so core.config doesn't moan.
    env["OPENAI_API_KEY"] = ""

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_mcp.cli",
            "router",
            "create-operator",
            "--username",
            "cli_op",
            "--password-stdin",
        ],
        input="cli_pw_12chars\n",
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, (
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )

    # The CLI subprocess created the user in the tmp DB; reload and
    # poke the identity module in-process to verify.
    monkeypatch.setenv("AGENT_MCP_ROUTER_DB", str(db_path))
    monkeypatch.setenv("AGENT_MCP_PROJECTS_FILE", str(projects_file))
    for mod in [
        "agent_mcp.router.identity",
        "agent_mcp.router.migrations_runner",
    ]:
        sys.modules.pop(mod, None)
    import agent_mcp.router.identity as identity

    importlib.reload(identity)
    row = identity.get_user_by_username("cli_op")
    assert row is not None
    assert identity.verify_password(row["password_hash"], "cli_pw_12chars")


def test_cli_create_operator_duplicate_username(
    tmp_path: Path,
) -> None:
    """Running create-operator twice with the same username gives a
    non-zero exit and an actionable error — no traceback splat."""
    db_path = tmp_path / "router.db"
    projects_file = tmp_path / "projects.local.json"
    projects_file.write_text("{}")

    env = os.environ.copy()
    env["AGENT_MCP_ROUTER_DB"] = str(db_path)
    env["AGENT_MCP_PROJECTS_FILE"] = str(projects_file)
    env["OPENAI_API_KEY"] = ""

    cmd = [
        sys.executable,
        "-m",
        "agent_mcp.cli",
        "router",
        "create-operator",
        "--username",
        "dup",
        "--password-stdin",
    ]
    first = subprocess.run(
        cmd, input="first_pw_12ch\n", capture_output=True, text=True, env=env, timeout=30, check=False
    )
    assert first.returncode == 0
    second = subprocess.run(
        cmd, input="second_pw_12ch\n", capture_output=True, text=True, env=env, timeout=30, check=False
    )
    assert second.returncode != 0
    combined = (second.stdout + second.stderr).lower()
    assert "exists" in combined or "duplicate" in combined or "already" in combined


def test_run_router_migrations_tolerates_existing_unwritable_parent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When `/var/lib/agent-mcp` (the production parent dir) is already
    provisioned by systemd-tmpfiles and the service can't escalate to
    create siblings, `run_router_migrations_upgrade` should swallow the
    `PermissionError` from `mkdir(parents=True)` rather than crash.

    Simulates the case by stubbing `Path.mkdir` to raise `PermissionError`
    while the parent already exists — the same shape the VM hit on PR C.
    """
    db_path = tmp_path / "router.db"
    monkeypatch.setenv("AGENT_MCP_ROUTER_DB", str(db_path))
    # The parent (tmp_path) already exists.

    from pathlib import Path as _Path

    original_mkdir = _Path.mkdir

    def raising_mkdir(self, *args, **kwargs):
        if str(self) == str(db_path.parent):
            raise PermissionError(13, "Permission denied", str(self))
        return original_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(_Path, "mkdir", raising_mkdir)

    from agent_mcp.router import migrations_runner

    importlib.reload(migrations_runner)
    # Should NOT raise — parent exists, swallowed PermissionError is OK.
    migrations_runner.run_router_migrations_upgrade()
    assert db_path.exists()


def test_run_router_migrations_re_raises_when_parent_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If the parent dir genuinely doesn't exist AND mkdir fails, we want
    the operator to see the missing-path problem — re-raise the
    PermissionError instead of swallowing it into a later sqlite error."""
    missing = tmp_path / "missing" / "router.db"
    monkeypatch.setenv("AGENT_MCP_ROUTER_DB", str(missing))

    from pathlib import Path as _Path

    def always_raising_mkdir(self, *args, **kwargs):
        raise PermissionError(13, "Permission denied", str(self))

    monkeypatch.setattr(_Path, "mkdir", always_raising_mkdir)

    from agent_mcp.router import migrations_runner

    importlib.reload(migrations_runner)
    with pytest.raises(PermissionError):
        migrations_runner.run_router_migrations_upgrade()
