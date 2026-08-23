"""Step 0 of the security-architecture hardening plan
(``~/.claude/plans/security-arch-hardening-consolidated.md``): three standalone fix-now
bugs surfaced by the 2026-08-23 follow-up architecture review, none of
which need the rest of the plan's structural work.

1. Plaintext agent bearer token logged at WARNING when ``terminate_agent``
   finds a DB row absent from the in-memory cache (a normal post-restart
   condition, not exotic) — contradicts this codebase's own established
   practice of only ever logging a token suffix (see
   ``agent_actions_db.py``'s ``source_token_suffix`` pattern).
2. Password-strength policy applied at only 2 of 4 mint sites — the
   env-var bootstrap path and the ``router create-operator`` CLI both
   skip ``validate_password_strength`` despite its own docstring
   declaring itself the canonical check for every new-password path.
3. SSO fresh-install lockout — ``setup_wizard``'s redirect-exempt prefix
   set doesn't include the SSO callback path, so a fresh install (no
   users yet) provisioning its first operator via SSO gets bounced to
   ``/setup`` before the callback ever runs.
"""

from __future__ import annotations

import importlib
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_mcp.core import globals as g
from agent_mcp.core.principal import Principal
from agent_mcp.core.tool_result import Ok
from tests.harness import make_principal, mcp_session

# ── Fixture shared by the password-policy tests (mirrors
# tests/test_router_identity.py's `router_db`) ──────────────────────


@pytest.fixture
def router_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    db_path = tmp_path / "router.db"
    monkeypatch.setenv("AGENT_MCP_ROUTER_DB", str(db_path))
    projects_file = tmp_path / "projects.local.json"
    monkeypatch.setenv("AGENT_MCP_PROJECTS_FILE", str(projects_file))
    for mod in [
        "agent_mcp.router.identity",
        "agent_mcp.router.migrations_runner",
        "agent_mcp.router.project_registry",
    ]:
        sys.modules.pop(mod, None)
    return db_path


def _operator_principal(project_name: str = "demo-project") -> Principal:
    return make_principal(
        kind="operator_session",
        user_id="test-operator",
        agent_id=None,
        sysadmin=True,
        project_name=project_name,
        project_role="operator",
        agent_role=None,
        can_wake_loop=False,
        source_token=None,
    )


# ── Bug 1: plaintext bearer token at WARNING ─────────────────────────


@pytest.mark.asyncio
async def test_terminate_agent_never_logs_full_token(tmp_path, caplog):
    """A DB row found absent from in-memory cache (post-restart) must
    log at most a token suffix, never the full bearer -- same discipline
    ``agent_actions_db.py`` already applies to its audit trail."""
    from agent_mcp.tools.admin_tools import (
        register_agent_tool_impl,
        terminate_agent_tool_impl,
    )

    async with mcp_session(tmp_path):
        reg = await register_agent_tool_impl(
            {"name": "wkr-log-leak", "role": "worker", "host": "https://h.x"},
            principal=_operator_principal(),
        )
        assert isinstance(reg, Ok)
        agent_id = reg.data["agent_id"]
        full_token = reg.data["token"]
        assert full_token and len(full_token) > 8

        # Simulate the post-restart condition: the DB row exists but the
        # in-memory active-agents cache doesn't have it (e.g. a fresh
        # process that hasn't re-warmed the cache yet).
        stale = [
            tkn for tkn, data in g.active_agents.items()
            if data.get("agent_id") == agent_id
        ]
        for tkn in stale:
            del g.active_agents[tkn]

        with caplog.at_level(logging.WARNING):
            term = await terminate_agent_tool_impl(
                {"agent_id": agent_id}, principal=_operator_principal(),
            )
        assert isinstance(term, Ok)

        full_leak = [
            r.getMessage() for r in caplog.records if full_token in r.getMessage()
        ]
        assert not full_leak, (
            "the full bearer token must never appear in a log record; "
            f"found it in: {full_leak}"
        )


# ── Bug 2: password policy skipped at 2 of 4 mint sites ──────────────


def test_env_bootstrap_rejects_weak_password(monkeypatch, router_db):
    """AGENT_MCP_BOOTSTRAP_PASSWORD must go through the same
    validate_password_strength gate as every other new-password path
    (mirrors tests/test_router_identity.py's test_env_var_bootstrap)."""
    monkeypatch.setenv("AGENT_MCP_BOOTSTRAP_USERNAME", "weak_boot_op")
    monkeypatch.setenv("AGENT_MCP_BOOTSTRAP_PASSWORD", "x")

    import agent_mcp.router.identity as identity

    importlib.reload(identity)

    with pytest.raises(identity.WeakPasswordError):
        identity.init_router_db()

    # No half-created user, and the user must not exist afterward.
    assert identity.get_user_by_username("weak_boot_op") is None


def test_cli_create_operator_rejects_weak_password(tmp_path):
    """`router create-operator` must reject a weak password the same
    way the setup wizard and REST create-user already do (mirrors
    tests/test_router_identity.py's test_cli_create_operator, which
    exercises the happy path via the same subprocess pattern)."""
    db_path = tmp_path / "router.db"
    projects_file = tmp_path / "projects.local.json"
    projects_file.write_text("{}")

    env = os.environ.copy()
    env["AGENT_MCP_ROUTER_DB"] = str(db_path)
    env["AGENT_MCP_PROJECTS_FILE"] = str(projects_file)
    env["OPENAI_API_KEY"] = ""

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_mcp.cli",
            "router",
            "create-operator",
            "--username",
            "cli_weak",
            "--password-stdin",
        ],
        input="x\n",
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )
    assert result.returncode != 0, (
        f"weak password must be rejected; stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )
    assert "password" in (result.stdout + result.stderr).lower()

    # No half-created user.
    monkeypatch_env = {
        "AGENT_MCP_ROUTER_DB": str(db_path),
        "AGENT_MCP_PROJECTS_FILE": str(projects_file),
    }
    old_env = {k: os.environ.get(k) for k in monkeypatch_env}
    os.environ.update(monkeypatch_env)
    try:
        for mod in [
            "agent_mcp.router.identity",
            "agent_mcp.router.migrations_runner",
        ]:
            sys.modules.pop(mod, None)
        import agent_mcp.router.identity as identity

        importlib.reload(identity)
        assert identity.get_user_by_username("cli_weak") is None
    finally:
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ── Bug 3: SSO fresh-install lockout ─────────────────────────────────


def test_sso_callback_path_is_redirect_exempt():
    """setup_wizard's redirect-exempt prefixes must include the SSO
    callback path, the same way auth_middleware's unauth prefixes
    already do -- else a fresh install can never complete an SSO-based
    first-operator provisioning flow (redirected to /setup first).

    Mirrors the exact matching logic
    empty_users_redirect_middleware uses: any(path.startswith(p) ...).
    """
    from agent_mcp.router import setup_wizard

    sso_callback_path = "/agent-mcp/sso/callback"
    assert any(
        sso_callback_path.startswith(p)
        for p in setup_wizard._REDIRECT_EXEMPT_PREFIXES
    ), (
        "setup_wizard._REDIRECT_EXEMPT_PREFIXES must exempt the SSO "
        "callback path, or a fresh install can never provision its "
        "first operator via SSO"
    )
