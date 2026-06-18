"""view_project_context must not leak the system token to non-admin callers.

UPSTREAM_ISSUES.md issue I: any agent with a valid token can call
view_project_context and read ``config_system_token`` (formerly
``config_admin_token`` before the Phase 2 Wave 1b rename) — the
project's router-internal authority bearer. That's a direct
worker→system privilege escalation through the tool surface.

Fix: filter rows whose context_key matches a sensitive pattern
(``config_*_token``, ``config_*_secret``, etc.) when caller is not
admin. Admins continue to see everything.

Migrated to ``tests/harness.py::mcp_session`` (Candidate F from
architecture review 2026-06-02).
"""

from __future__ import annotations

import pytest

from tests.harness import mcp_session


pytestmark = pytest.mark.asyncio


def _seed(admin, *, key: str, value: str) -> None:
    r = admin.client.post(
        "/api/memories",
        json={"token": admin.admin_token, "context_key": key, "context_value": value},
    )
    assert r.status_code == 200, r.text


async def test_admin_sees_config_system_token(tmp_path) -> None:
    """Admins must continue to see config_system_token (baseline)."""
    async with mcp_session(tmp_path) as admin:
        # Seed a fake secret-looking key (config_system_token already exists
        # from startup; we don't need to add it).
        result = await admin.call("view_project_context", {})
        text = result[0].text
        assert "config_system_token" in text, (
            "admin should see config_system_token in view_project_context "
            "output"
        )


async def test_worker_does_not_see_config_system_token(tmp_path) -> None:
    """Workers must NOT see config_system_token — privilege escalation
    otherwise."""
    async with mcp_session(tmp_path) as admin:
        worker = await admin.create_worker("test-worker")

        result = await worker.call("view_project_context", {})
        text = result[0].text
        assert "config_system_token" not in text, (
            "worker token can read config_system_token via "
            "view_project_context — privilege escalation (issue I). "
            "Got:\n" + text[:1000]
        )
        # And the actual system token value must not appear either.
        assert admin.admin_token not in text, (
            "worker can read the literal system token value (issue I)"
        )


async def test_worker_does_not_see_other_config_secrets(tmp_path) -> None:
    """The redaction applies to any config_*_token / _secret / _password
    key."""
    async with mcp_session(tmp_path) as admin:
        _seed(admin, key="config_openai_secret", value="sk-very-secret-12345")
        worker = await admin.create_worker("test-worker")

        result = await worker.call("view_project_context", {})
        text = result[0].text
        assert "config_openai_secret" not in text
        assert "sk-very-secret-12345" not in text


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
