"""ADR-0017 (Wave 12 PR B): view_project_context returns rows in FULL.

This suite was UPSTREAM_ISSUES.md issue I — filter secret-named rows
(``config_*_token`` / ``config_*_secret``) from non-admin callers. Wave 12
PR B removes content-based secret detection: project_context is shared
project knowledge, returned AS-IS to any authorized reader (workers
included). Real secrets belong in the operator-only, non-RAG
project_settings store — where the settings-store redaction survives — not
in memory. The config_* namespace can no longer be written to memory at
all (ADR-0016), but a legacy/tampered DB shape is still returned verbatim.

The former "worker does NOT see secret-named keys" tests are inverted here
to "worker sees them in full".

Migrated to ``tests/harness.py::mcp_session`` (Candidate F from
architecture review 2026-06-02).
"""

from __future__ import annotations

import pytest

from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio


def _seed(admin, *, key: str, value: str) -> None:
    """Seed a project_context row DIRECTLY via the repository.

    Wave 11 (ADR-0016): the write path rejects config_* keys for every
    caller, so the config-named rows here are seeded raw — they pin the
    read-side redaction on legacy/tampered DB shapes the live write
    path can no longer create. The keys redact via the secret-word
    VOCABULARY (``token`` / ``secret`` segments in _SECRET_SUFFIX_RE);
    the old blanket config_* rule is deleted.
    """
    import json as _json

    from agent_mcp.db.connection import get_db_connection
    from agent_mcp.repositories import project_context_repository as _pc_repo

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        _pc_repo.upsert(
            key,
            _json.dumps(value),
            None,
            description_provided=False,
            actor="admin",
            connection=cursor,
        )
        conn.commit()
    finally:
        conn.close()


async def test_admin_sees_config_system_token(tmp_path) -> None:
    """Admins must continue to see config_*_token rows (baseline).

    retire-system-token Wave 3 deleted the startup write of
    ``config_system_token`` (the row is gone with the global). Seed a
    representative ``config_*_token`` row directly so the redaction
    contract still has a target to assert against.
    """
    async with mcp_session(tmp_path) as admin:
        _seed(admin, key="config_system_token", value="sentinel-system-token")
        result = await admin.call("view_project_context", {})
        text = result[0].text
        assert "config_system_token" in text, (
            "admin should see config_system_token in view_project_context "
            "output"
        )


async def test_worker_sees_config_system_token(tmp_path) -> None:
    """ADR-0017: a worker sees a secret-named memory row AS-IS — memory is
    shared project content, protection is by authorization not content."""
    async with mcp_session(tmp_path) as admin:
        _seed(admin, key="config_system_token", value="sentinel-system-token")
        worker = await admin.create_worker("test-worker")

        result = await worker.call("view_project_context", {})
        text = result[0].text
        assert "config_system_token" in text
        assert "sentinel-system-token" in text


async def test_worker_sees_other_config_secrets(tmp_path) -> None:
    """ADR-0017: any secret-named memory row is returned in full."""
    async with mcp_session(tmp_path) as admin:
        _seed(admin, key="config_openai_secret", value="sk-very-secret-12345")
        worker = await admin.create_worker("test-worker")

        result = await worker.call("view_project_context", {})
        text = result[0].text
        assert "config_openai_secret" in text
        assert "sk-very-secret-12345" in text


async def test_worker_still_sees_non_secret_keys(tmp_path) -> None:
    """Non-secret keys must still be visible to workers (no
    over-filtering)."""
    async with mcp_session(tmp_path) as admin:
        _seed(admin, key="project_notes", value="some non-secret info")
        worker = await admin.create_worker("test-worker")

        result = await worker.call("view_project_context", {})
        text = result[0].text
        assert "project_notes" in text
        assert "some non-secret info" in text
