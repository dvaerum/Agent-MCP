"""Wave 11 PR 0 — the ``project_settings`` store (ADR-0016).

Separates per-project operational config (``config_*`` keys) OUT of the
``project_context`` (memory/knowledge) table into a dedicated
``project_settings`` store:

* **memory** = agent-authored shared knowledge (``project_context``,
  RAG-indexed);
* **settings** = operational config (``project_settings``, never
  RAG-indexed, operator-only access model).

Coverage (the RED set written before the implementation):

  1. Migration ``0016_move_config_to_project_settings`` — HARD CUTOVER:
     one transaction copies every ``config_*`` row into
     ``project_settings`` AND deletes it from ``project_context``;
     knowledge rows untouched; values byte-identical.
  2. The canonical config-read seams (``_get_config_bool`` /
     ``_get_config_int`` in ``tools/access.py`` and
     ``aoe_notify.load_config``) read the NEW table.
  3. F009 regression at the new seam: ``GET /api/settings-data`` returns
     the REAL toggle value to a cookie/forwarding (non-confirmed)
     operator; only the two genuinely secret keys
     (``config_aoe_bearer_token`` / ``config_aoe_bearer_token_file``)
     redact for non-confirmed tiers.
  4. Write gates on the new MCP tool family
     (``update_project_settings`` / ``delete_project_settings``):
     ``system.config.write`` cap required; ``config_aoe_*`` stays
     sysadmin-only; non-``config_*`` keys rejected.
  5. The ``project_context`` write path now rejects ``config_*`` for
     EVERYONE (admin included) with the ADR-0016 pointer.
  6. Wake parity (BL-R14-1): settings writes/deletes fire the same
     ``tools/list_changed`` + ``wake_all_for_flag_recheck`` seams the
     context tools fired for these keys.
"""

from __future__ import annotations

import datetime
import json
import os
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from tests.harness import make_principal, mcp_session

_REDACTED = "[redacted]"
# The alembic head advances as migrations are added; keep this in lockstep
# with the newest revision (0020 = agent last_activity_at for idle-stop).
_MIGRATION_HEAD = "0023_single_root_task_index"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _alembic_cfg(project_dir: str):
    from alembic.config import Config

    import agent_mcp

    pkg_root = os.path.dirname(agent_mcp.__file__)
    cfg = Config()
    cfg.set_main_option(
        "script_location", os.path.join(pkg_root, "migrations"),
    )
    os.environ["MCP_PROJECT_DIR"] = project_dir
    return cfg


def _bootstrap_fresh_db(tmp_path) -> str:
    """Production bootstrap: ``init_database()`` (create_all) then the
    Alembic chain to head. Mirrors lifespan startup ordering."""
    from alembic import command

    project_dir = str(tmp_path)
    agent_dir = tmp_path / ".agent"
    agent_dir.mkdir(exist_ok=True)
    db_path = str(agent_dir / "mcp_state.db")

    os.environ["MCP_PROJECT_DIR"] = project_dir
    from agent_mcp.db import engine as _engine

    _engine.reset_engine_cache()

    from agent_mcp.db.schema import init_database

    init_database()
    command.upgrade(_alembic_cfg(project_dir), "head")
    return db_path


def _seed_context_row(
    db_path: str, key: str, value: str, description: str = "seed",
) -> None:
    now = datetime.datetime.now().isoformat()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO project_context "
            "(context_key, value, description, created_at, created_by, "
            "updated_at, updated_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (key, value, description, now, "seed-actor", now, "seed-actor"),
        )
        conn.commit()
    finally:
        conn.close()


def _fetch_row(db_path: str, table: str, key: str):
    assert table in ("project_context", "project_settings")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            f"SELECT context_key, value, description, created_at, "
            f"created_by, updated_at, updated_by FROM {table} "
            f"WHERE context_key = ?",
            (key,),
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


def _seed_setting(key: str, value: Any, description: str | None = None) -> None:
    """Seed a ``project_settings`` row the way a sysadmin write lands
    (JSON-encoded value via the settings repository)."""
    from agent_mcp.db.connection import get_db_connection
    from agent_mcp.repositories import (
        project_settings_repository as settings_repo,
    )

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        settings_repo.upsert(
            key,
            json.dumps(value),
            description,
            description_provided=description is not None,
            actor="sysadmin",
            connection=cursor,
        )
        conn.commit()
    finally:
        conn.close()


def _text(result) -> str:
    return getattr(result[0], "text", "") if result else ""


# ---------------------------------------------------------------------------
# 1. Migration — hard cutover
# ---------------------------------------------------------------------------


def test_migration_0016_is_alembic_head(tmp_path) -> None:
    db_path = _bootstrap_fresh_db(tmp_path)
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[0] == _MIGRATION_HEAD


def test_fresh_db_has_project_settings_table(tmp_path) -> None:
    """``Base.metadata.create_all`` gives fresh DBs the table without
    the migration doing any work."""
    db_path = _bootstrap_fresh_db(tmp_path)
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='project_settings'"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, "fresh DB must have project_settings via create_all"


def test_migration_0016_moves_config_rows_hard_cutover(tmp_path) -> None:
    """Pre-cutover shape → alembic upgrade → config rows live in
    ``project_settings`` byte-identical, gone from ``project_context``;
    the knowledge row is untouched."""
    from alembic import command

    db_path = _bootstrap_fresh_db(tmp_path)
    cfg = _alembic_cfg(str(tmp_path))

    # Rewind to the pre-cutover revision (0016's downgrade drops the
    # settings table), then seed the pre-cutover shape.
    command.downgrade(cfg, "0015_drop_config_system_token")

    _seed_context_row(
        db_path, "config_allow_worker_to_worker", "true", "policy seed",
    )
    _seed_context_row(
        db_path, "config_aoe_bearer_token", '"SENTINEL-BEARER"', "aoe seed",
    )
    _seed_context_row(
        db_path, "team_motto", '"ship the v1"', "knowledge seed",
    )
    pre_toggle = _fetch_row(db_path, "project_context", "config_allow_worker_to_worker")
    pre_bearer = _fetch_row(db_path, "project_context", "config_aoe_bearer_token")
    pre_knowledge = _fetch_row(db_path, "project_context", "team_motto")

    command.upgrade(cfg, "head")

    # Config rows moved — byte-identical across all seven columns.
    assert _fetch_row(
        db_path, "project_settings", "config_allow_worker_to_worker"
    ) == pre_toggle
    assert _fetch_row(
        db_path, "project_settings", "config_aoe_bearer_token"
    ) == pre_bearer

    # HARD CUTOVER: gone from project_context.
    assert _fetch_row(
        db_path, "project_context", "config_allow_worker_to_worker"
    ) is None
    assert _fetch_row(
        db_path, "project_context", "config_aoe_bearer_token"
    ) is None

    # Knowledge row untouched.
    assert _fetch_row(db_path, "project_context", "team_motto") == pre_knowledge
    assert _fetch_row(db_path, "project_settings", "team_motto") is None


def test_migration_0016_copy_does_not_clobber_existing_setting(tmp_path) -> None:
    """Re-running the upgrade body against a DB that already carries a
    ``project_settings`` row for the same key must NOT overwrite it
    (the ``NOT IN`` guard) — but the ``project_context`` copy is still
    deleted (cutover converges)."""
    db_path = _bootstrap_fresh_db(tmp_path)

    _seed_setting("config_allow_worker_to_worker", False, "already migrated")
    # A stale duplicate left in project_context (e.g. a re-run after a
    # partial restore).
    _seed_context_row(
        db_path, "config_allow_worker_to_worker", "true", "stale duplicate",
    )

    # Execute the migration's upgrade body directly (alembic itself is
    # a no-op once the DB is stamped at head) — same technique as
    # tests/test_migration_0015_drop_config_system_token.py.
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO project_settings (context_key, value, description, "
            "created_at, created_by, updated_at, updated_by) "
            "SELECT context_key, value, description, created_at, created_by, "
            "updated_at, updated_by FROM project_context "
            "WHERE context_key LIKE 'config\\_%' ESCAPE '\\' "
            "AND context_key NOT IN (SELECT context_key FROM project_settings)"
        )
        conn.execute(
            "DELETE FROM project_context "
            "WHERE context_key LIKE 'config\\_%' ESCAPE '\\'"
        )
        conn.commit()
    finally:
        conn.close()

    settings_row = _fetch_row(
        db_path, "project_settings", "config_allow_worker_to_worker"
    )
    assert settings_row is not None
    assert settings_row["value"] == "false", (
        "existing project_settings row must win over a stale "
        "project_context duplicate"
    )
    assert _fetch_row(
        db_path, "project_context", "config_allow_worker_to_worker"
    ) is None


def test_migration_0016_escape_does_not_move_confignoscore_keys(tmp_path) -> None:
    """The LIKE pattern is escaped: a knowledge key like ``configX...``
    (no underscore) must NOT be swept into settings."""
    from alembic import command

    db_path = _bootstrap_fresh_db(tmp_path)
    cfg = _alembic_cfg(str(tmp_path))
    command.downgrade(cfg, "0015_drop_config_system_token")

    _seed_context_row(db_path, "configuration_notes", '"knowledge"')
    command.upgrade(cfg, "head")

    assert _fetch_row(db_path, "project_context", "configuration_notes") is not None
    assert _fetch_row(db_path, "project_settings", "configuration_notes") is None


# ---------------------------------------------------------------------------
# 2. Read seams point at the NEW table
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_config_bool_reads_project_settings(tmp_path: Path) -> None:
    async with mcp_session(tmp_path) as admin:
        from agent_mcp.tools.access import _get_config_bool

        assert _get_config_bool("config_allow_worker_to_worker", False) is False

        _seed_setting("config_allow_worker_to_worker", True)
        assert _get_config_bool("config_allow_worker_to_worker", False) is True

        # A contradictory row smuggled into project_context (raw SQL —
        # the write path rejects it) must be IGNORED by the gate read.
        # The harness roots the app at tmp_path/"project".
        db_path = str(tmp_path / "project" / ".agent" / "mcp_state.db")
        _seed_context_row(
            db_path, "config_allow_worker_self_assign", "true",
        )
        assert _get_config_bool(
            "config_allow_worker_self_assign", False
        ) is False, "gate read must consult project_settings, not project_context"
        del admin  # silence unused warning


@pytest.mark.asyncio
async def test_get_config_int_reads_project_settings(tmp_path: Path) -> None:
    async with mcp_session(tmp_path):
        from agent_mcp.tools.access import _get_config_int

        assert _get_config_int("config_message_retention_days", 0) == 0
        _seed_setting("config_message_retention_days", 7)
        assert _get_config_int("config_message_retention_days", 0) == 7


@pytest.mark.asyncio
async def test_aoe_load_config_reads_project_settings(tmp_path: Path) -> None:
    async with mcp_session(tmp_path):
        _seed_setting("config_aoe_notify_enabled", True)
        _seed_setting("config_aoe_base_url", "http://aoe.test")
        _seed_setting("config_aoe_bearer_token", "SENTINEL-AOE-TOKEN")
        _seed_setting("config_aoe_timeout_ms", 1234)

        from agent_mcp.features.aoe_notify import load_config

        cfg = load_config()
        assert cfg.enabled is True
        assert cfg.base_url == "http://aoe.test"
        assert cfg.bearer_token == "SENTINEL-AOE-TOKEN"
        assert cfg.timeout_ms == 1234


# ---------------------------------------------------------------------------
# 3. F009 regression at the new REST seam
# ---------------------------------------------------------------------------


def _settings_rows(response) -> list[dict]:
    body = response.json()
    rows = body["settings"] if isinstance(body, dict) else body
    assert isinstance(rows, list)
    return rows


def _row_for(rows: list[dict], key: str) -> dict:
    for r in rows:
        if r.get("context_key") == key:
            return r
    raise AssertionError(f"settings row {key!r} missing from response")


@pytest.mark.asyncio
async def test_settings_data_real_values_for_non_confirmed_operator(
    tmp_path: Path,
) -> None:
    """The F009 scenario against the NEW store: a cookie/forwarding
    (non-confirmed) operator reads the REAL toggle value — only the two
    genuinely secret keys redact."""
    async with mcp_session(tmp_path) as admin:
        _seed_setting("config_allow_worker_to_worker", True)
        _seed_setting("config_message_retention_days", 7)
        _seed_setting("config_aoe_bearer_token", "SENTINEL-AOE-BEARER-9f04")
        _seed_setting("config_aoe_bearer_token_file", "/run/secret/token")

        r = admin.get("/api/settings-data")  # signed forwarding header
        assert r.status_code == 200, r.text
        rows = _settings_rows(r)

        toggle = _row_for(rows, "config_allow_worker_to_worker")
        assert toggle["value"] != _REDACTED, (
            "policy toggle redacted to a session operator — F009 regressed "
            "on the settings store"
        )
        assert json.loads(toggle["value"]) is True

        retention = _row_for(rows, "config_message_retention_days")
        assert json.loads(retention["value"]) == 7

        # The two genuinely secret keys DO redact for non-confirmed tiers.
        bearer = _row_for(rows, "config_aoe_bearer_token")
        assert bearer["value"] == _REDACTED
        token_file = _row_for(rows, "config_aoe_bearer_token_file")
        assert token_file["value"] == _REDACTED
        assert "SENTINEL-AOE-BEARER-9f04" not in r.text


@pytest.mark.asyncio
async def test_settings_data_real_secret_for_confirmed_operator_bearer(
    tmp_path: Path,
) -> None:
    """A CONFIRMED operator-tier bearer (per-agent manager/admin token)
    reads the real secret value."""
    async with mcp_session(tmp_path) as admin:
        _seed_setting("config_aoe_bearer_token", "SENTINEL-AOE-BEARER-9f04")

        r = admin.client.get(
            "/api/settings-data",
            headers={"Authorization": f"Bearer {admin.admin_token}"},
        )
        assert r.status_code == 200, r.text
        bearer = _row_for(_settings_rows(r), "config_aoe_bearer_token")
        assert bearer["value"] == json.dumps("SENTINEL-AOE-BEARER-9f04")


@pytest.mark.asyncio
async def test_settings_data_requires_operator_session(tmp_path: Path) -> None:
    async with mcp_session(tmp_path) as admin:
        r = admin.client.get("/api/settings-data")  # no auth at all
        assert r.status_code in (401, 403), r.text


# ---------------------------------------------------------------------------
# 4. REST write path — /api/settings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rest_settings_put_round_trips(tmp_path: Path) -> None:
    """PUT /api/settings/<key> as a forwarding operator upserts the row
    into project_settings and it reads back via /api/settings-data —
    the end-to-end F009 fix at the new seam."""
    async with mcp_session(tmp_path) as admin:
        r = admin.request(
            "PUT",
            "/api/settings/config_allow_worker_to_worker",
            json={"context_value": True},
        )
        assert r.status_code == 200, r.text

        read = admin.get("/api/settings-data")
        row = _row_for(_settings_rows(read), "config_allow_worker_to_worker")
        assert json.loads(row["value"]) is True

        # And the row landed in project_settings — NOT project_context.
        # The harness roots the app at tmp_path/"project".
        db_path = str(tmp_path / "project" / ".agent" / "mcp_state.db")
        assert _fetch_row(
            db_path, "project_settings", "config_allow_worker_to_worker"
        ) is not None
        assert _fetch_row(
            db_path, "project_context", "config_allow_worker_to_worker"
        ) is None


@pytest.mark.asyncio
async def test_rest_settings_post_and_delete(tmp_path: Path) -> None:
    async with mcp_session(tmp_path) as admin:
        r = admin.post(
            "/api/settings",
            json={
                "context_key": "config_message_retention_days",
                "context_value": 30,
                "description": "retention window",
            },
        )
        assert r.status_code == 200, r.text

        read = admin.get("/api/settings-data")
        row = _row_for(_settings_rows(read), "config_message_retention_days")
        assert json.loads(row["value"]) == 30
        assert row["description"] == "retention window"

        r = admin.request(
            "DELETE", "/api/settings/config_message_retention_days", json={},
        )
        assert r.status_code == 200, r.text

        read = admin.get("/api/settings-data")
        assert all(
            row["context_key"] != "config_message_retention_days"
            for row in _settings_rows(read)
        )


@pytest.mark.asyncio
async def test_rest_settings_aoe_write_denied_for_non_sysadmin(
    tmp_path: Path,
) -> None:
    """The config_aoe_* sysadmin gate carries over to the settings write
    path: a forwarding OPERATOR (non-sysadmin) is denied (SSRF /
    bearer-exfil rationale — see _CONFIG_AOE_KEY_RE)."""
    async with mcp_session(tmp_path) as admin:
        r = admin.request(
            "PUT",
            "/api/settings/config_aoe_base_url",
            json={"context_value": "http://169.254.169.254"},
        )
        assert r.status_code == 403, r.text

        # The harness roots the app at tmp_path/"project".
        db_path = str(tmp_path / "project" / ".agent" / "mcp_state.db")
        assert _fetch_row(
            db_path, "project_settings", "config_aoe_base_url"
        ) is None


@pytest.mark.asyncio
async def test_rest_settings_rejects_non_config_key(tmp_path: Path) -> None:
    """The settings store holds config_* keys ONLY — knowledge belongs
    in project_context."""
    async with mcp_session(tmp_path) as admin:
        r = admin.post(
            "/api/settings",
            json={"context_key": "team_motto", "context_value": "ship it"},
        )
        assert r.status_code == 400, r.text


# ---------------------------------------------------------------------------
# 5. MCP tool write gates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_project_settings_denied_for_worker(tmp_path: Path) -> None:
    async with mcp_session(tmp_path) as admin:
        worker = await admin.create_worker("settings-worker")
        result = await worker.call(
            "update_project_settings",
            {
                "context_key": "config_allow_worker_to_worker",
                "context_value": True,
            },
        )
        text = _text(result)
        assert worker._last_is_error or "Unauthorized" in text, (
            f"worker bearer must lack system.config.write; got: {text}"
        )

        from agent_mcp.tools.access import _get_config_bool

        assert _get_config_bool("config_allow_worker_to_worker", False) is False


@pytest.mark.asyncio
async def test_update_project_settings_allows_operator_tier(
    tmp_path: Path,
) -> None:
    """A non-sysadmin operator (system.config.write in the operator
    bundle) may write non-AoE config keys."""
    async with mcp_session(tmp_path):
        from agent_mcp.core.tool_result import Ok
        from agent_mcp.tools.registry import dispatch_tool_call

        operator = make_principal(
            kind="operator_session",
            user_id="op-nonsysadmin",
            project_role="operator",
        )
        result = await dispatch_tool_call(
            "update_project_settings",
            {
                "context_key": "config_allow_worker_to_worker",
                "context_value": True,
            },
            principal=operator,
        )
        assert isinstance(result, Ok), f"expected Ok, got {result!r}"

        from agent_mcp.tools.access import _get_config_bool

        assert _get_config_bool("config_allow_worker_to_worker", False) is True


@pytest.mark.asyncio
async def test_update_project_settings_aoe_requires_sysadmin(
    tmp_path: Path,
) -> None:
    async with mcp_session(tmp_path) as admin:
        from agent_mcp.core.tool_result import Ok, PermissionDenied
        from agent_mcp.tools.registry import dispatch_tool_call

        operator = make_principal(
            kind="operator_session",
            user_id="op-nonsysadmin",
            project_role="operator",
        )
        denied = await dispatch_tool_call(
            "update_project_settings",
            {
                "context_key": "config_aoe_base_url",
                "context_value": "http://aoe.test",
            },
            principal=operator,
        )
        assert isinstance(denied, PermissionDenied), (
            f"non-sysadmin operator must be denied config_aoe_*; got {denied!r}"
        )

        # The harness admin is sysadmin — allowed.
        sysadmin = admin._principal()
        ok = await dispatch_tool_call(
            "update_project_settings",
            {
                "context_key": "config_aoe_base_url",
                "context_value": "http://aoe.test",
            },
            principal=sysadmin,
        )
        assert isinstance(ok, Ok), f"sysadmin write must pass; got {ok!r}"


@pytest.mark.asyncio
async def test_update_project_settings_rejects_non_config_key(
    tmp_path: Path,
) -> None:
    async with mcp_session(tmp_path) as admin:
        from agent_mcp.core.tool_result import Invalid
        from agent_mcp.tools.registry import dispatch_tool_call

        result = await dispatch_tool_call(
            "update_project_settings",
            {"context_key": "team_motto", "context_value": "ship it"},
            principal=admin._principal(),
        )
        assert isinstance(result, Invalid), (
            f"non-config key must be Invalid on the settings path; got {result!r}"
        )


@pytest.mark.asyncio
async def test_delete_project_settings_gates(tmp_path: Path) -> None:
    async with mcp_session(tmp_path) as admin:
        from agent_mcp.core.tool_result import (
            Ok,
            PermissionDenied,
        )
        from agent_mcp.tools.registry import dispatch_tool_call

        _seed_setting("config_allow_worker_to_worker", True)
        _seed_setting("config_aoe_base_url", "http://aoe.test")

        worker = await admin.create_worker("settings-del-worker")
        denied = await dispatch_tool_call(
            "delete_project_settings",
            {"context_key": "config_allow_worker_to_worker"},
            principal=worker._principal(),
        )
        assert isinstance(denied, PermissionDenied)

        operator = make_principal(
            kind="operator_session",
            user_id="op-nonsysadmin",
            project_role="operator",
        )
        aoe_denied = await dispatch_tool_call(
            "delete_project_settings",
            {"context_key": "config_aoe_base_url"},
            principal=operator,
        )
        assert isinstance(aoe_denied, PermissionDenied)

        ok = await dispatch_tool_call(
            "delete_project_settings",
            {"context_key": "config_allow_worker_to_worker"},
            principal=operator,
        )
        assert isinstance(ok, Ok), f"operator delete must pass; got {ok!r}"

        from agent_mcp.tools.access import _get_config_bool

        assert _get_config_bool("config_allow_worker_to_worker", False) is False


@pytest.mark.asyncio
async def test_view_project_settings_redaction(tmp_path: Path) -> None:
    """view_project_settings masks only _SECRET_SETTING_KEYS, and only
    for non-confirmed tiers."""
    async with mcp_session(tmp_path) as admin:
        _seed_setting("config_allow_worker_to_worker", True)
        _seed_setting("config_aoe_bearer_token", "SENTINEL-VIEW-BEARER")

        # Harness admin: agent_bearer manager + sysadmin → CONFIRMED tier.
        result = await admin.assert_tool_succeeds("view_project_settings", {})
        text = _text(result)
        assert "SENTINEL-VIEW-BEARER" in text

        # A non-confirmed operator-session principal gets the mask.
        from agent_mcp.core.tool_result import Ok
        from agent_mcp.tools.registry import dispatch_tool_call

        # kind=forwarding_header + no project_role visible would lack the
        # cap; use an operator whose tier is nonetheless confirmed only
        # via project_role — so instead exercise the mask through the
        # REST seam test above. Here: viewer-tier caller is DENIED
        # outright (no system.config.write).
        viewer = make_principal(
            kind="operator_session",
            user_id="viewer-1",
            project_role="viewer",
        )
        denied = await dispatch_tool_call(
            "view_project_settings", {}, principal=viewer,
        )
        assert not isinstance(denied, Ok), (
            "viewer must not read the settings store via the MCP tool"
        )


@pytest.mark.asyncio
async def test_settings_tools_hidden_from_worker_tools_list(
    tmp_path: Path,
) -> None:
    async with mcp_session(tmp_path) as admin:
        worker = await admin.create_worker("settings-vis-worker")
        worker_tools = {t.name for t in await worker.list_tools()}
        assert "view_project_settings" not in worker_tools
        assert "update_project_settings" not in worker_tools
        assert "delete_project_settings" not in worker_tools

        admin_tools = {t.name for t in await admin.list_tools()}
        assert {"view_project_settings", "update_project_settings",
                "delete_project_settings"} <= admin_tools


# ---------------------------------------------------------------------------
# 6. project_context write path rejects config_* for EVERYONE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_context_write_rejects_config_for_admin(tmp_path: Path) -> None:
    async with mcp_session(tmp_path) as admin:
        for tool in ("update_project_context", "create_project_context"):
            result = await admin.call(
                tool,
                {
                    "context_key": "config_allow_worker_to_worker",
                    "context_value": True,
                },
            )
            text = _text(result)
            assert admin._last_is_error, (
                f"{tool}: config_* must be rejected on the context path "
                f"even for admin; got: {text}"
            )
            # Worker-message clarity: rejection is Invalid (not the
            # Unauthorized-framed PermissionDenied) and drops the internal
            # ADR-0016 jargon; it still points at the settings store.
            assert "project settings store" in text, (
                f"{tool}: rejection must point at the settings store; "
                f"got: {text}"
            )
            assert "Unauthorized" not in text, (
                f"{tool}: must not render as Unauthorized; got: {text}"
            )

        # The harness roots the app at tmp_path/"project".
        db_path = str(tmp_path / "project" / ".agent" / "mcp_state.db")
        assert _fetch_row(
            db_path, "project_context", "config_allow_worker_to_worker"
        ) is None


@pytest.mark.asyncio
async def test_context_write_rejects_config_for_worker(tmp_path: Path) -> None:
    async with mcp_session(tmp_path) as admin:
        worker = await admin.create_worker("ctx-config-worker")
        result = await worker.call(
            "update_project_context",
            {
                "context_key": "config_allow_worker_to_worker",
                "context_value": True,
            },
        )
        assert worker._last_is_error
        assert "project settings store" in _text(result)


@pytest.mark.asyncio
async def test_context_bulk_write_rejects_config_for_admin(
    tmp_path: Path,
) -> None:
    async with mcp_session(tmp_path) as admin:
        result = await admin.call(
            "bulk_update_project_context",
            {
                "updates": [
                    {"context_key": "harmless_note", "context_value": "x"},
                    {
                        "context_key": "config_auto_event_loop_global",
                        "context_value": False,
                    },
                ],
            },
        )
        text = _text(result)
        assert admin._last_is_error, (
            f"bulk context write containing config_* must be rejected: {text}"
        )
        # Atomic: the innocuous key must not have landed either.
        # The harness roots the app at tmp_path/"project".
        db_path = str(tmp_path / "project" / ".agent" / "mcp_state.db")
        assert _fetch_row(db_path, "project_context", "harmless_note") is None


@pytest.mark.asyncio
async def test_context_delete_rejects_config_keys(tmp_path: Path) -> None:
    """Deleting a config_* key via the context tools is rejected too —
    those rows can only live in project_settings now."""
    async with mcp_session(tmp_path) as admin:
        result = await admin.call(
            "delete_project_context",
            {"context_key": "config_allow_worker_to_worker"},
        )
        assert admin._last_is_error
        assert "project settings store" in _text(result)


@pytest.mark.asyncio
async def test_rest_memories_rejects_config_for_operator(tmp_path: Path) -> None:
    """The REST /api/memories surface dispatches the context tools, so
    the everyone-rejection shows up there too.

    Worker-message clarity: config_* rejection is now ``Invalid`` (an
    unprocessable input, not an authorization failure), so the REST status
    is 400 rather than 403 — the caller can't fix it by re-authenticating;
    they must pick a non-config_* key or use the settings surface."""
    async with mcp_session(tmp_path) as admin:
        r = admin.post(
            "/api/memories",
            json={
                "context_key": "config_allow_worker_to_worker",
                "context_value": True,
            },
        )
        assert r.status_code == 400, r.text
        assert "settings" in r.text


# ---------------------------------------------------------------------------
# 7. Wake parity (BL-R14-1) on the settings write path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_settings_worker_toggle_pushes_tools_list_changed(
    tmp_path: Path,
) -> None:
    import agent_mcp.tools.project_context_tools as pct

    async with mcp_session(tmp_path) as admin:
        with patch.object(pct, "_emit_tools_list_changed", autospec=True) as emit:
            await admin.assert_tool_succeeds(
                "update_project_settings",
                {
                    "context_key": "config_allow_worker_self_assign",
                    "context_value": True,
                },
            )
            assert emit.called, (
                "settings write of config_allow_worker_* must push "
                "tools/list_changed"
            )


@pytest.mark.asyncio
async def test_settings_loop_toggle_wakes_waiters(tmp_path: Path) -> None:
    from agent_mcp.core import globals as g

    async with mcp_session(tmp_path) as admin:
        with patch.object(g, "wake_all_for_flag_recheck", autospec=True) as wake:
            await admin.assert_tool_succeeds(
                "update_project_settings",
                {
                    "context_key": "config_auto_event_loop_global",
                    "context_value": False,
                },
            )
            assert wake.called, (
                "settings write of config_auto_event_loop_global must call "
                "wake_all_for_flag_recheck"
            )


@pytest.mark.asyncio
async def test_settings_delete_fires_wakes(tmp_path: Path) -> None:
    """A deleted toggle reverts to its default — visibility may change,
    so the delete fires the same wake seam."""
    from agent_mcp.core import globals as g

    async with mcp_session(tmp_path) as admin:
        _seed_setting("config_auto_event_loop_global", False)
        with patch.object(g, "wake_all_for_flag_recheck", autospec=True) as wake:
            await admin.assert_tool_succeeds(
                "delete_project_settings",
                {"context_key": "config_auto_event_loop_global"},
            )
            assert wake.called


@pytest.mark.asyncio
async def test_rest_settings_write_fires_wakes(tmp_path: Path) -> None:
    """REST PUT /api/settings/<key> dispatches the same gated tool, so
    the wake parity holds on the REST surface too."""
    import agent_mcp.tools.project_context_tools as pct

    async with mcp_session(tmp_path) as admin:
        with patch.object(pct, "_emit_tools_list_changed", autospec=True) as emit:
            r = admin.request(
                "PUT",
                "/api/settings/config_allow_worker_to_worker",
                json={"context_value": True},
            )
            assert r.status_code == 200, r.text
            assert emit.called


@pytest.mark.asyncio
async def test_settings_unrelated_key_fires_no_wakes(tmp_path: Path) -> None:
    import agent_mcp.tools.project_context_tools as pct
    from agent_mcp.core import globals as g

    async with mcp_session(tmp_path) as admin:
        with patch.object(
            pct, "_emit_tools_list_changed", autospec=True
        ) as emit, patch.object(
            g, "wake_all_for_flag_recheck", autospec=True
        ) as wake:
            await admin.assert_tool_succeeds(
                "update_project_settings",
                {"context_key": "config_message_retention_days",
                 "context_value": 14},
            )
            assert not emit.called
            assert not wake.called


# ---------------------------------------------------------------------------
# 8. Live policy gate reads the migrated value
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_to_worker_gate_reads_new_store_live(
    tmp_path: Path,
) -> None:
    """End-to-end: flipping the toggle in the NEW store changes the live
    worker→worker send gate (no restart).

    The default is now True, so the OFF leg is seeded explicitly — the
    test still proves the gate re-reads the project_settings store live
    across a real False→True flip."""
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("w2w-alice")
        bob = await admin.create_worker("w2w-bob")

        _seed_setting("config_allow_worker_to_worker", False)

        denied = await alice.call(
            "send_agent_message",
            {"recipient_id": bob.agent_id, "message": "hi bob"},
        )
        assert alice._last_is_error or "worker" in _text(denied).lower(), (
            f"explicit OFF: worker→worker send must be denied; got "
            f"{_text(denied)}"
        )

        _seed_setting("config_allow_worker_to_worker", True)

        await alice.assert_tool_succeeds(
            "send_agent_message",
            {"recipient_id": bob.agent_id, "message": "hi again"},
        )
