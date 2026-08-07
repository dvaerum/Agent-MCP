"""VULN-003 — path-traversal hardening for backup_project_context.

The ``backup_project_context`` tool writes a JSON file whose name is
``f"{backup_name}.json"`` inside ``$MCP_PROJECT_DIR/.agent/backups/
context/``. Before this fix the ``backup_name`` MCP argument flowed
unsanitized into ``os.path.join(backup_dir, backup_filename)``, so a
caller who can reach the tool (operator-tier — guarded by
``_is_admin_principal``) could supply
``backup_name="../../../tmp/pwned"`` and overwrite arbitrary
JSON-writable paths the server process owns.

The operator-gating is the primary control. This still matters under
two amplifying paths:

  * **Stolen operator cookie.** Anyone holding a valid operator
    session can otherwise write outside the project dir — turns a
    read-only intrusion surface into a write-anywhere primitive.
  * **VULN-001 CORS exploit vector.** A misconfigured CORS origin
    lets an attacker page a victim operator's browser into firing
    operator tool calls; combine with this and the attacker reaches
    arbitrary-write through a victim's authenticated session.

The fix has two layers:

  1. **Schema pattern**: ``backup_name`` is constrained to
     ``^[A-Za-z0-9._-]{1,128}$`` in the tool's inputSchema. The
     dispatcher runs ``jsonschema.validate`` before the impl runs,
     so any traversal payload (``../``, absolute paths, spaces, NUL
     bytes, shell metacharacters) is rejected with the framework's
     ``Input validation error: ...`` text + ``isError=True``.

  2. **Defense in depth**: the impl resolves the candidate path with
     ``Path.resolve()`` and asserts it's inside the resolved backup
     directory via ``relative_to``. A future in-process caller that
     bypasses schema validation still cannot write outside.

These tests pin both layers.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.harness import make_principal, mcp_session

pytestmark = pytest.mark.asyncio


def _first_text(result) -> str:
    if not result:
        return ""
    return getattr(result[0], "text", "") or ""


def _backup_dir_for(tmp_path: Path) -> Path:
    """Path the impl writes into. ``mcp_session(tmp_path)`` builds the
    app with ``project_dir=str(tmp_path / "project")`` and lifespan
    startup stamps that on ``MCP_PROJECT_DIR``."""
    return tmp_path / "project" / ".agent" / "backups" / "context"


# ─────────────────────────────────────────────────────────────────────
# Layer 1: schema-pattern rejection (the wire path real clients hit)
# ─────────────────────────────────────────────────────────────────────


async def test_traversal_payload_rejected_at_schema(tmp_path) -> None:
    """``backup_name="../../../tmp/pwned"`` must be rejected by the
    dispatcher's jsonschema validation BEFORE the impl runs, and no
    file may land anywhere — inside the backup dir or out."""
    pwned_target = Path("/tmp/pwned.json")
    # Pre-existence sanity: if a prior test left /tmp/pwned.json behind,
    # this assertion catches it so we don't falsely pass.
    pre_existed = pwned_target.exists()

    async with mcp_session(tmp_path) as admin:
        result = await admin.call(
            "backup_project_context",
            {"backup_name": "../../../tmp/pwned"},
        )
        text = _first_text(result)

        assert getattr(admin, "_last_is_error", False), (
            f"expected isError=true, got text={text!r}"
        )
        assert "Input validation error" in text or "pattern" in text.lower(), (
            f"expected schema validation rejection, got: {text!r}"
        )

        # The backup directory itself may or may not exist (the impl
        # never ran, so makedirs() didn't fire) — what matters is that
        # NO file lives at any traversal-resolved path.
        backup_dir = _backup_dir_for(tmp_path)
        if backup_dir.exists():
            entries = list(backup_dir.iterdir())
            assert entries == [], (
                f"backup dir should be empty after rejection, got {entries}"
            )

        # And nothing landed outside the project dir.
        if not pre_existed:
            assert not pwned_target.exists(), (
                "traversal write succeeded — /tmp/pwned.json was created"
            )


async def test_legit_backup_name_succeeds(tmp_path) -> None:
    """A well-formed slug succeeds and lands inside the backup dir."""
    async with mcp_session(tmp_path) as admin:
        result = await admin.call(
            "backup_project_context",
            {"backup_name": "legit_backup_2026"},
        )
        text = _first_text(result)
        assert not getattr(admin, "_last_is_error", False), (
            f"unexpected isError=true: {text!r}"
        )
        assert "Unauthorized" not in text, text

        backup_dir = _backup_dir_for(tmp_path)
        expected = backup_dir / "legit_backup_2026.json"
        assert expected.exists(), (
            f"expected backup file at {expected}; "
            f"dir contents = {list(backup_dir.glob('*'))}"
        )

        # Containment cross-check: resolve() must keep the file inside
        # the backup directory tree.
        resolved = expected.resolve()
        resolved.relative_to(backup_dir.resolve())  # raises ValueError if outside


async def test_spaces_rejected_at_schema(tmp_path) -> None:
    """Spaces in ``backup_name`` don't match the slug pattern."""
    async with mcp_session(tmp_path) as admin:
        result = await admin.call(
            "backup_project_context",
            {"backup_name": "has spaces"},
        )
        text = _first_text(result)
        assert getattr(admin, "_last_is_error", False), (
            f"expected isError=true, got text={text!r}"
        )
        assert "Input validation error" in text or "pattern" in text.lower(), (
            f"expected schema validation rejection, got: {text!r}"
        )


async def test_empty_string_rejected_at_schema(tmp_path) -> None:
    """Empty ``backup_name`` doesn't satisfy the pattern's min-length
    (``{1,128}``). NB: the dispatcher does NOT strip empty strings
    (only ``None``), so this reaches the validator as ``""``."""
    async with mcp_session(tmp_path) as admin:
        result = await admin.call(
            "backup_project_context",
            {"backup_name": ""},
        )
        text = _first_text(result)
        assert getattr(admin, "_last_is_error", False), (
            f"expected isError=true, got text={text!r}"
        )
        assert "Input validation error" in text or "pattern" in text.lower(), (
            f"expected schema validation rejection, got: {text!r}"
        )


async def test_overlong_name_rejected_at_schema(tmp_path) -> None:
    """``backup_name`` longer than 128 chars violates the pattern."""
    overlong = "a" * 200
    async with mcp_session(tmp_path) as admin:
        result = await admin.call(
            "backup_project_context",
            {"backup_name": overlong},
        )
        text = _first_text(result)
        assert getattr(admin, "_last_is_error", False), (
            f"expected isError=true, got text={text!r}"
        )
        assert "Input validation error" in text or "pattern" in text.lower(), (
            f"expected schema validation rejection, got: {text!r}"
        )


# ─────────────────────────────────────────────────────────────────────
# Layer 2: defense-in-depth — direct impl call bypasses schema
# ─────────────────────────────────────────────────────────────────────


async def test_path_containment_defense_in_depth_when_schema_bypassed(
    tmp_path,
) -> None:
    """Future internal callers (or buggy refactors) that invoke
    ``backup_project_context_tool_impl`` directly bypass the
    dispatcher's jsonschema validation. The impl's own resolve() +
    relative_to() containment check must still catch traversal."""
    from agent_mcp.core.tool_result import Invalid
    from agent_mcp.tools.project_context_tools import (
        backup_project_context_tool_impl,
    )

    pwned_target = Path("/tmp/pwned_dind.json")
    pre_existed = pwned_target.exists()

    async with mcp_session(tmp_path):
        admin_principal = make_principal(
            kind="agent_bearer",
            user_id="test-harness-operator",
            agent_id="admin",
            sysadmin=True,
            project_name="harness",
            project_role="operator",
            agent_role="manager",
            can_wake_loop=False,
            source_token=None,
        )

        result = await backup_project_context_tool_impl(
            {"backup_name": "../../../tmp/pwned_dind"},
            principal=admin_principal,
        )

        assert isinstance(result, Invalid), (
            f"expected Invalid (containment check), got: {result!r}"
        )
        assert result.field == "backup_name", result
        assert "outside" in result.message.lower(), result

        backup_dir = _backup_dir_for(tmp_path)
        # makedirs() ran before the containment check, so the dir
        # exists — but must be empty.
        if backup_dir.exists():
            entries = list(backup_dir.iterdir())
            assert entries == [], (
                f"backup dir must be empty after containment rejection, "
                f"got {entries}"
            )

        if not pre_existed:
            assert not pwned_target.exists(), (
                "defense-in-depth failed — /tmp/pwned_dind.json was created"
            )
