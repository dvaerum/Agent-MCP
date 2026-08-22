"""R9-F1 (round-9 completeness-critic): R8-F1's own generic length
backstop (``tools/registry.py::_first_oversized_string_path``) is
bypassed by any "free-form JSON value" schema field.

Root cause: the walker's dict branch does ``props = schema.get(
"properties") or {}`` then ``sub_schema = props.get(key)`` -- when the
schema node has NO ``properties`` key at all (the shape used by every
"any JSON-serializable value" field: an ``anyOf``/``oneOf`` alternative
of a bare ``{"type": "object"}`` or ``{"type": "array"}``), every key
looks up to ``None`` and is silently skipped -- the RUNTIME VALUE is
never inspected. Same story for the array branch: no ``items`` key
means ``schema.get("items")`` is ``None`` and the whole list is
skipped. jsonschema.validate doesn't catch it either, since
``additionalProperties`` defaults permissive and a bare
``{"type": "array"}`` has no ``items`` constraint to violate.

Live-confirmed sink: ``update_project_context`` with
``context_value: {"blob": "A" * 200000}`` -- accepted (isError=False),
the full 200,000-char string round-tripped verbatim via
``view_project_context``, 3x over ``DEFAULT_STRING_MAX_LEN`` (65,536).

Class-sweep: 4 call sites share the identical "any JSON value" anyOf
shape:
  * project_context_tools.py -- update_project_context's context_value
  * project_context_tools.py -- create_project_context's context_value
  * project_context_tools.py -- bulk_update_project_context's per-item
    context_value
  * project_settings_tools.py (_VALUE_ANYOF) -- update_project_settings's
    context_value

Fix: when the dict/array branch of ``_first_oversized_string_path``
finds NO ``properties``/``items`` key declared at all (the free-form
shape), it no longer skips the subtree -- it recurses into the actual
runtime value's keys/elements with no schema to consult, treating
every nested string as an implicit "no declared maxLength" leaf still
subject to ``DEFAULT_STRING_MAX_LEN``.
"""

from __future__ import annotations

import sqlite3

import pytest

from agent_mcp.core.schema_limits import DEFAULT_STRING_MAX_LEN
from agent_mcp.tools.registry import _first_oversized_string_path
from tests.harness import mcp_session


def _text(result_blocks) -> str:
    parts = [getattr(b, "text", "") for b in result_blocks]
    return "\n".join(p for p in parts if p)


def _context_row(key: str) -> dict | None:
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT * FROM project_context WHERE context_key = ?", (key,)
        )
        r = cur.fetchone()
    finally:
        conn.close()
    return dict(r) if r else None


def _settings_row(key: str) -> dict | None:
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT * FROM project_settings WHERE context_key = ?", (key,)
        )
        r = cur.fetchone()
    finally:
        conn.close()
    return dict(r) if r else None


# --- Unit coverage on the walker itself -------------------------------
# (These pin down the exact bypass mechanics and are cheap/fast; the
# MCP-level tests below prove the fix closes the real live-repro path.)

# Mirrors the actual anyOf shape used by update_project_context /
# create_project_context / bulk_update_project_context / update_project_settings.
_VALUE_ANYOF_SCHEMA = {
    "type": "object",
    "properties": {
        "context_value": {
            "anyOf": [
                {"type": "string"},
                {"type": "number"},
                {"type": "boolean"},
                {"type": "null"},
                {"type": "object", "additionalProperties": True},
                {"type": "array"},
            ]
        }
    },
}


def test_oversized_path_none_for_freeform_object_before_fix_would_be_bug():
    """Baseline: a normal-sized free-form object must not be flagged."""
    value = {"context_value": {"blob": "short"}}
    assert _first_oversized_string_path(value, _VALUE_ANYOF_SCHEMA) is None


def test_oversized_path_flags_oversized_string_nested_in_freeform_object():
    """RED (pre-fix): an oversized string nested inside a free-form
    object value (no `properties` declared on that schema node) must
    be flagged, not silently skipped.
    """
    oversized = {"context_value": {"blob": "A" * (DEFAULT_STRING_MAX_LEN + 1)}}
    hit = _first_oversized_string_path(oversized, _VALUE_ANYOF_SCHEMA)
    assert hit == "context_value.blob", (
        f"expected the free-form nested string to be flagged at "
        f"'context_value.blob', got {hit!r}"
    )


def test_oversized_path_flags_oversized_string_nested_in_freeform_array():
    """RED (pre-fix): an oversized string nested inside a free-form
    array value (no `items` declared on that schema node) must be
    flagged, not silently skipped.
    """
    oversized = {"context_value": ["ok", "A" * (DEFAULT_STRING_MAX_LEN + 1)]}
    hit = _first_oversized_string_path(oversized, _VALUE_ANYOF_SCHEMA)
    assert hit == "context_value[1]", (
        f"expected the free-form array element to be flagged at "
        f"'context_value[1]', got {hit!r}"
    )


def test_oversized_path_recurses_deeply_nested_freeform_array_of_objects():
    """Nested-shape follow-up: arrays-of-objects several levels deep
    inside a free-form value, with no schema at all past the entry
    point, must still be walked all the way down to the offending
    string leaf.
    """
    oversized = {
        "context_value": {
            "items": [
                {"nested": {"deep": "ok"}},
                {"nested": {"deep": "B" * (DEFAULT_STRING_MAX_LEN + 1)}},
            ]
        }
    }
    hit = _first_oversized_string_path(oversized, _VALUE_ANYOF_SCHEMA)
    assert hit == "context_value.items[1].nested.deep", (
        f"expected deep recursion into the free-form value, got {hit!r}"
    )


def test_oversized_path_freeform_via_oneof_alternative_too():
    """The bypass isn't anyOf-specific -- a oneOf alternative with the
    same bare object/array shape must get the same treatment.
    """
    schema = {
        "type": "object",
        "properties": {
            "payload": {
                "oneOf": [
                    {"type": "string"},
                    {"type": "object"},
                    {"type": "array"},
                ]
            }
        },
    }
    oversized_obj = {"payload": {"x": "C" * (DEFAULT_STRING_MAX_LEN + 1)}}
    assert _first_oversized_string_path(oversized_obj, schema) == "payload.x"

    oversized_arr = {"payload": [{"y": [{"z": "D" * (DEFAULT_STRING_MAX_LEN + 1)}]}]}
    assert (
        _first_oversized_string_path(oversized_arr, schema)
        == "payload[0].y[0].z"
    )


def test_oversized_path_freeform_object_within_default_bound_is_none():
    """Regression guard: a reasonably-sized nested free-form value
    (well under DEFAULT_STRING_MAX_LEN) must not be rejected.
    """
    value = {
        "context_value": {
            "notes": "a normal note " * 50,
            "tags": ["a", "b", "c"],
            "nested": {"ok": True, "count": 3},
        }
    }
    assert len(value["context_value"]["notes"]) < DEFAULT_STRING_MAX_LEN
    assert _first_oversized_string_path(value, _VALUE_ANYOF_SCHEMA) is None


# --- MCP-level, live-repro-shaped coverage for all 4 call sites --------


@pytest.mark.asyncio
async def test_update_project_context_rejects_oversized_string_in_freeform_value(
    tmp_path,
) -> None:
    """Live-repro'd sink: update_project_context's context_value."""
    async with mcp_session(tmp_path) as admin:
        huge = "A" * (DEFAULT_STRING_MAX_LEN + 1)
        result = await admin.call(
            "update_project_context",
            {"context_key": "r9f1.update", "context_value": {"blob": huge}},
        )
        assert admin._last_is_error, (
            "update_project_context must reject an oversized string "
            f"nested in a free-form context_value; got isError=False, "
            f"text={_text(result)!r}"
        )
        assert _context_row("r9f1.update") is None, (
            "no row should be written when the oversized nested string "
            "is rejected"
        )


@pytest.mark.asyncio
async def test_create_project_context_rejects_oversized_string_in_freeform_value(
    tmp_path,
) -> None:
    """Sibling sink: create_project_context's context_value."""
    async with mcp_session(tmp_path) as admin:
        huge = "B" * (DEFAULT_STRING_MAX_LEN + 1)
        result = await admin.call(
            "create_project_context",
            {"context_key": "r9f1.create", "context_value": {"blob": huge}},
        )
        assert admin._last_is_error, (
            "create_project_context must reject an oversized string "
            f"nested in a free-form context_value; got isError=False, "
            f"text={_text(result)!r}"
        )
        assert _context_row("r9f1.create") is None


@pytest.mark.asyncio
async def test_bulk_update_project_context_rejects_oversized_string_in_freeform_value(
    tmp_path,
) -> None:
    """Sibling sink: bulk_update_project_context's per-item
    context_value. Atomic: the whole batch must be rejected, including
    the otherwise-legitimate sibling entry.
    """
    async with mcp_session(tmp_path) as admin:
        huge = "C" * (DEFAULT_STRING_MAX_LEN + 1)
        result = await admin.call(
            "bulk_update_project_context",
            {
                "updates": [
                    {"context_key": "r9f1.bulk.ok", "context_value": "fine"},
                    {
                        "context_key": "r9f1.bulk.bad",
                        "context_value": {"blob": huge},
                    },
                ]
            },
        )
        assert admin._last_is_error, (
            "bulk_update_project_context must reject an oversized "
            f"nested string in a free-form context_value; got "
            f"isError=False, text={_text(result)!r}"
        )
        assert _context_row("r9f1.bulk.bad") is None
        assert _context_row("r9f1.bulk.ok") is None, (
            "atomic reject must not write the sibling entry either"
        )


@pytest.mark.asyncio
async def test_update_project_settings_rejects_oversized_string_in_freeform_value(
    tmp_path,
) -> None:
    """Sibling sink: update_project_settings's context_value
    (_VALUE_ANYOF in project_settings_tools.py)."""
    async with mcp_session(tmp_path) as admin:
        huge = "D" * (DEFAULT_STRING_MAX_LEN + 1)
        result = await admin.call(
            "update_project_settings",
            {
                "context_key": "config_r9f1_test",
                "context_value": {"blob": huge},
            },
        )
        assert admin._last_is_error, (
            "update_project_settings must reject an oversized string "
            f"nested in a free-form context_value; got isError=False, "
            f"text={_text(result)!r}"
        )
        assert _settings_row("config_r9f1_test") is None


# --- Regression: legitimate free-form JSON values still round-trip ----


@pytest.mark.asyncio
async def test_update_project_context_accepts_normal_sized_freeform_value(
    tmp_path,
) -> None:
    """Regression guard: a reasonably-sized nested dict/list value (the
    normal shape this field exists to serve) must still work.
    """
    async with mcp_session(tmp_path) as admin:
        normal_value = {
            "service": "backend",
            "endpoints": ["https://a.example", "https://b.example"],
            "meta": {"owner": "team-x", "notes": "fine " * 20},
        }
        result = await admin.call(
            "update_project_context",
            {"context_key": "r9f1.normal", "context_value": normal_value},
        )
        assert not admin._last_is_error, (
            "update_project_context must accept a normal-sized "
            f"free-form context_value; got isError=True, "
            f"text={_text(result)!r}"
        )
        row = _context_row("r9f1.normal")
        assert row is not None


@pytest.mark.asyncio
async def test_update_project_settings_accepts_normal_sized_freeform_value(
    tmp_path,
) -> None:
    async with mcp_session(tmp_path) as admin:
        result = await admin.call(
            "update_project_settings",
            {
                "context_key": "config_r9f1_normal",
                "context_value": {"enabled": True, "threshold": 42},
            },
        )
        assert not admin._last_is_error, (
            "update_project_settings must accept a normal-sized "
            f"free-form context_value; got isError=True, "
            f"text={_text(result)!r}"
        )
        assert _settings_row("config_r9f1_normal") is not None
