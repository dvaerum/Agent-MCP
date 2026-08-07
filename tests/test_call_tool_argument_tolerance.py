"""Tools must tolerate the argument shapes real MCP clients actually send.

LLM-driven MCP clients (Claude Code, Cursor agents, etc.) regularly
send argument shapes that a strict `additionalProperties: false`
validator would reject:

    {"token": null, ...}                      # JSON null for an absent value
    {"token": "<legacy-bearer>", ...}         # a stray retired self-auth arg
    {"_meta": {"progressToken": "..."} , ...} # client SDK leaking _meta into arguments

The MCP framework's lowlevel server runs
`jsonschema.validate(arguments, tool.inputSchema)` BEFORE invoking the
registered dispatcher. All three shapes would be rejected upstream:

    Input validation error: None is not of type 'string'
    Input validation error: Additional properties are not allowed
                            ('token' was unexpected)
    Input validation error: Additional properties are not allowed
                            ('_meta' was unexpected)

token-retirement plan Phase C: the self-auth `token` param is retired
from every schema. An old client that still sends `token` (null or a
real value) must not get a hard 400 during the transition — the
dispatcher's `_clean_arguments_for_schema` treats `token` (and
`_meta`/`meta`) as reserved keys and strips them before validation, so
a stray `token` is silently tolerated-and-ignored rather than
rejected. `null`-valued keys are likewise dropped.

These tests pin the tolerance invariant via the same framework
handler real MCP clients hit (CallToolRequest → registered handler).

Migrated to `tests/harness.py::mcp_session` (Candidate F from
architecture review 2026-06-02). Uses `admin.call(...)` which drives
the same registered CallToolRequest handler via
`request_auth_token`, identical wire path.
"""

from __future__ import annotations

import pytest

from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio


def _result_text(result_blocks) -> str:
    parts = [getattr(b, "text", "") for b in result_blocks]
    return "\n".join(p for p in parts if p)


def _is_validation_error(admin, result_blocks) -> bool:
    """The harness stashes the last server isError on the session.
    A "validation error" is isError=True with the framework's
    'Input validation error' text."""
    if not getattr(admin, "_last_is_error", False):
        return False
    return "Input validation error" in _result_text(result_blocks)


# --- Class 1: a stray retired `token` arg is tolerated-and-stripped ---

@pytest.mark.parametrize("token_value", [None, "stray-legacy-bearer-token"])
@pytest.mark.parametrize(
    "tool_name,extra_args",
    [
        # The three tools Dennis explicitly hit in production.
        ("get_agent_messages", {}),
        ("send_agent_message", {"recipient_id": "admin", "message": "ping"}),
        ("bulk_update_project_context", {
            "updates": [{"context_key": "_t_-32602_null", "context_value": "v"}],
        }),
        # A couple more high-traffic tools of each shape.
        ("view_project_context", {}),
        ("view_tasks", {}),
    ],
)
async def test_stray_token_arg_is_tolerated_and_stripped(
    tmp_path, tool_name: str, extra_args: dict, token_value
) -> None:
    """The self-auth `token` param is retired from every schema
    (token-retirement plan Phase C), so `additionalProperties: false`
    would reject a stray `token` an old client still sends — whether
    `null` (model serializing an "absent" optional) or a real bearer
    value. The dispatcher must strip `token` (a reserved key) before
    the framework's validator sees it: tolerate-and-ignore, never a
    hard 400.
    """
    async with mcp_session(tmp_path) as admin:
        args = {"token": token_value, **extra_args}
        result = await admin.call(tool_name, args)

        assert not _is_validation_error(admin, result), (
            f"{tool_name}: framework rejected a stray `token={token_value!r}` "
            f"at schema validation instead of stripping it: "
            f"{_result_text(result)}"
        )


# --- Class 2: `_meta` escape hatch leaked into arguments ---

@pytest.mark.parametrize(
    "tool_name,extra_args",
    [
        ("get_agent_messages", {}),
        ("send_agent_message", {"recipient_id": "admin", "message": "ping"}),
        ("view_tasks", {}),
    ],
)
async def test_meta_escape_hatch_does_not_break_validation(
    tmp_path, tool_name: str, extra_args: dict
) -> None:
    """Some MCP client SDKs put the spec-defined `_meta` field on the
    arguments object (instead of at the params level). Schemas use
    `additionalProperties: false`, so this trips validation. The
    dispatcher must drop `_meta` before validation."""
    async with mcp_session(tmp_path) as admin:
        args = {"_meta": {"progressToken": "p-1"}, **extra_args}
        result = await admin.call(tool_name, args)

        assert not _is_validation_error(admin, result), (
            f"{tool_name}: framework rejected leaked `_meta` at schema "
            f"validation: {_result_text(result)}"
        )


# --- Class 3: combined — both null-token AND _meta ---

async def test_token_null_and_meta_combined(tmp_path) -> None:
    """A real-world Claude Code call from an old client: both a stray
    retired `token` and `_meta` (SDK leaking the progress token into
    arguments). Both are reserved keys the dispatcher strips before
    validation, so the call is tolerated, not rejected."""
    async with mcp_session(tmp_path) as admin:
        args = {
            "token": None,
            "_meta": {"progressToken": "abc"},
            "recipient_id": "admin",
            "message": "combined-probe",
        }
        result = await admin.call("send_agent_message", args)

        assert not _is_validation_error(admin, result), (
            f"framework rejected combined null-token + _meta: "
            f"{_result_text(result)}"
        )


# --- Negative: real validation errors (wrong types, bad enum) still surface ---

async def test_real_schema_violations_still_rejected(tmp_path) -> None:
    """Tolerance must not become permissiveness. A truly wrong value
    (e.g. wrong enum) must still be reported so the model can fix it."""
    async with mcp_session(tmp_path) as admin:
        # `priority` enum doesn't include "urgent-X"
        args = {
            "recipient_id": "admin",
            "message": "ping",
            "priority": "urgent-X",
        }
        result = await admin.call("send_agent_message", args)

        assert _is_validation_error(admin, result), (
            f"a real schema violation should still surface as a validation "
            f"error so the caller can correct it: {_result_text(result)}"
        )
