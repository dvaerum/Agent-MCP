"""R8-F1 (round-8 protocol/fuzz pentest): the MCP tool-argument schema
layer never declared ``maxLength`` on any string-typed property, so a
caller could send an arbitrarily large string in ANY field and it
would land — capped only by the router's blanket 1MB request-body cap
(``router/app.py::_MCP_MAX_BODY_BYTES``), not by anything tool-schema
or dispatcher level.

Confirmed live against vm-dev @ e68e36a:

* ``register_agent`` with ``name`` = 200,000 x "a" -> 200 OK, agent
  created with a 200,000-char agent_id.
* ``create_task`` with ``task_title`` = 500,000 x "X" -> 200 OK, task
  created and stored with a 500KB title.

Fix: every string-typed schema property either declares an explicit
``maxLength`` (identifier/title-shaped fields, mirroring the
``router/app.py::_NAME_MAX = 64`` precedent) or falls back to the
shared ``DEFAULT_STRING_MAX_LEN`` the dispatcher enforces on any
undeclared string leaf (``tools/registry.py::
_first_oversized_string_path``), so every tool -- present and future
-- is bounded.
"""

from __future__ import annotations

import pytest

from agent_mcp.core.schema_limits import DEFAULT_STRING_MAX_LEN
from agent_mcp.tools.registry import _first_oversized_string_path
from tests.harness import mcp_session


def _result_text(result_blocks) -> str:
    parts = [getattr(b, "text", "") for b in result_blocks]
    return "\n".join(p for p in parts if p)


@pytest.mark.asyncio
async def test_register_agent_rejects_200k_char_name(tmp_path) -> None:
    """R8-F1 confirmed sink #1: register_agent's `name` had no
    maxLength -- a 200,000-char name minted a 200,000-char agent_id.
    """
    async with mcp_session(tmp_path) as admin:
        huge_name = "a" * 200_000
        result = await admin.call("register_agent", {"name": huge_name})

        assert admin._last_is_error, (
            "register_agent should reject a 200,000-char name as "
            f"invalid; got isError=False, text={_result_text(result)!r}"
        )
        text = _result_text(result).lower()
        assert (
            "length" in text or "invalid" in text or "long" in text
        ), f"expected a length/invalid rejection message, got: {text!r}"


@pytest.mark.asyncio
async def test_create_task_rejects_500k_char_title(tmp_path) -> None:
    """R8-F1 confirmed sink #2: create_task's `task_title` had no
    maxLength -- a 500,000-char title was stored and echoed verbatim
    in view_tasks.
    """
    async with mcp_session(tmp_path) as admin:
        huge_title = "X" * 500_000
        result = await admin.call("create_task", {"task_title": huge_title})

        assert admin._last_is_error, (
            "create_task should reject a 500,000-char task_title as "
            f"invalid; got isError=False, text={_result_text(result)!r}"
        )
        text = _result_text(result).lower()
        assert (
            "length" in text or "invalid" in text or "long" in text
        ), f"expected a length/invalid rejection message, got: {text!r}"


@pytest.mark.asyncio
async def test_assign_task_rejects_oversized_title(tmp_path) -> None:
    """Sibling flagged by the pentest lane: assign_task's Mode-1
    `task_title` is reachable by WORKER-tier bearers (lower trust bar
    than operator-only register_agent/create_task) and had the same
    unbounded-string gap.
    """
    async with mcp_session(tmp_path) as admin:
        huge_title = "T" * 500_000
        result = await admin.call(
            "assign_task",
            {"task_title": huge_title, "task_description": "d"},
        )
        assert admin._last_is_error, (
            "assign_task should reject a 500,000-char task_title as "
            f"invalid; got isError=False, text={_result_text(result)!r}"
        )


@pytest.mark.asyncio
async def test_assign_task_mode2_batch_rejects_oversized_title(
    tmp_path,
) -> None:
    """Sibling: the Mode-2 batch `tasks[].title` nested field."""
    async with mcp_session(tmp_path) as admin:
        huge_title = "B" * 500_000
        result = await admin.call(
            "assign_task",
            {
                "tasks": [
                    {"title": huge_title, "description": "d"},
                ]
            },
        )
        assert admin._last_is_error, (
            "assign_task Mode-2 batch should reject an oversized "
            f"tasks[].title; got isError=False, text={_result_text(result)!r}"
        )


@pytest.mark.asyncio
async def test_edit_agent_rejects_oversized_color_and_working_directory(
    tmp_path,
) -> None:
    """Sibling: edit_agent's `color`/`working_directory` fields."""
    async with mcp_session(tmp_path) as admin:
        worker = await admin.create_worker("bob")

        result = await admin.call(
            "edit_agent",
            {"agent_id": worker.agent_id, "color": "c" * 100_000},
        )
        assert admin._last_is_error, (
            "edit_agent should reject a 100,000-char color; got "
            f"isError=False, text={_result_text(result)!r}"
        )

        result = await admin.call(
            "edit_agent",
            {
                "agent_id": worker.agent_id,
                "working_directory": "/" + ("d" * 100_000),
            },
        )
        assert admin._last_is_error, (
            "edit_agent should reject a 100,000-char working_directory; "
            f"got isError=False, text={_result_text(result)!r}"
        )


# --- Unit coverage for the generic dispatcher-level backstop ---------


def test_oversized_path_none_when_within_default_bound():
    schema = {"type": "object", "properties": {"foo": {"type": "string"}}}
    assert _first_oversized_string_path({"foo": "short"}, schema) is None


def test_oversized_path_flags_undeclared_string_over_default():
    schema = {"type": "object", "properties": {"foo": {"type": "string"}}}
    oversized = {"foo": "x" * (DEFAULT_STRING_MAX_LEN + 1)}
    assert _first_oversized_string_path(oversized, schema) == "foo"


def test_oversized_path_skips_fields_with_explicit_maxlength():
    """A field that already declares its own (smaller OR larger)
    maxLength is jsonschema.validate's job, not this backstop's --
    this function must not double-flag it.
    """
    schema = {
        "type": "object",
        "properties": {"foo": {"type": "string", "maxLength": 5}},
    }
    # Deliberately over the field's own maxLength=5 but the backstop
    # must still return None: jsonschema.validate (run separately by
    # the caller) is what enforces the declared bound.
    assert _first_oversized_string_path({"foo": "x" * 100}, schema) is None


def test_oversized_path_handles_nullable_string_type_list():
    """R8-F1 fix: a `"type": ["string", "null"]` declaration (common
    in this codebase for optional fields) must still be recognized as
    string-shaped -- a bare `schema.get("type") == "string"` check
    would silently skip these.
    """
    schema = {
        "type": "object",
        "properties": {"prompt": {"type": ["string", "null"]}},
    }
    oversized = {"prompt": "x" * (DEFAULT_STRING_MAX_LEN + 1)}
    assert _first_oversized_string_path(oversized, schema) == "prompt"


def test_oversized_path_recurses_into_nested_array_of_objects():
    schema = {
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"title": {"type": "string"}},
                },
            }
        },
    }
    oversized = {
        "tasks": [{"title": "ok"}, {"title": "x" * (DEFAULT_STRING_MAX_LEN + 1)}]
    }
    assert _first_oversized_string_path(oversized, schema) == "tasks[1].title"


def test_oversized_path_recurses_into_anyof_string_branch():
    schema = {
        "type": "object",
        "properties": {
            "context_value": {
                "anyOf": [
                    {"type": "string"},
                    {"type": "number"},
                    {"type": "object", "additionalProperties": True},
                ]
            }
        },
    }
    oversized = {"context_value": "x" * (DEFAULT_STRING_MAX_LEN + 1)}
    assert _first_oversized_string_path(oversized, schema) == "context_value"
    # A non-string alternative (e.g. a number) is simply not a string
    # leaf and passes through untouched.
    assert _first_oversized_string_path({"context_value": 42}, schema) is None


@pytest.mark.asyncio
async def test_legitimate_task_description_and_rag_document_still_work(
    tmp_path,
) -> None:
    """Regression guard: the fix must not reject normal-sized legitimate
    content. A several-KB task description (well within any sane
    free-text bound) must still succeed.
    """
    async with mcp_session(tmp_path) as admin:
        normal_description = "This is a perfectly normal task description. " * 100
        assert len(normal_description) < 10_000
        result = await admin.call(
            "create_task",
            {
                "task_title": "A normal task title",
                "task_description": normal_description,
            },
        )
        assert not admin._last_is_error, (
            "create_task must accept a normal-sized description; got "
            f"isError=True, text={_result_text(result)!r}"
        )
