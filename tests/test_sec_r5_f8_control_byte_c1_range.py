"""Security R5-F8 — control-byte strip misses the C1 range (U+0080-U+009F).

FINDING: R4-F3's control-byte strip (``_CONTROL_BYTE_RE`` in
``agent_mcp/utils/json_utils.py``, both the Step-3 fallback regex and
the unconditional ``_strip_control_bytes`` pass) only matches the C0
range ``[\\x00-\\x08\\x0B\\x0C\\x0E-\\x1F]`` — the C1 range
(U+0080-U+009F) was completely uncovered by any pass. C1 includes CSI
(U+009B) and OSC (U+009D), the 8-bit single-character equivalents of
``ESC[`` / ``ESC]`` — the exact same terminal-injection primitive
R3-F1/R4-F3 exist to strip, reachable with NO ESC byte and NO JSON
escaping at all: U+0080-U+009F are ordinary legal JSON string content
per RFC 8259 (only U+0000-U+001F must be escaped), so this is
trivially reachable, not a parser-corner-case.

Fix: widen ``_CONTROL_BYTE_RE`` (and the Step-3 fallback regex, kept in
sync) to also cover DEL (``\\x7F``) and the C1 range
(``\\x80-\\x9F``).
"""

from __future__ import annotations

import pytest

from agent_mcp.utils.json_utils import sanitize_json_input
from tests.harness import mcp_session

_C1_CSI = "\x9b"  # 8-bit CSI, equivalent to ESC [
_C1_OSC = "\x9d"  # 8-bit OSC, equivalent to ESC ]
_DEL = "\x7f"

_C1_LADEN = f"before{_C1_CSI}after{_C1_OSC}end"
_C1_STRIPPED = "beforeafterend"

_C0_LADEN = "hello\x1bworld\x07bell\x00nul"
_C0_STRIPPED = "helloworldbellnul"


# ---------------------------------------------------------------------
# Unit-level: sanitize_json_input direct calls.
# ---------------------------------------------------------------------


def test_dict_input_strips_c1_csi_and_osc_from_string_leaves() -> None:
    """Top-level string leaf: C1 CSI/OSC must be stripped."""
    given = {"task_title": _C1_LADEN}
    result = sanitize_json_input(given)
    assert _C1_CSI not in result["task_title"]
    assert _C1_OSC not in result["task_title"]
    assert result["task_title"] == _C1_STRIPPED


def test_list_of_strings_strips_c1_range() -> None:
    given = [_C1_LADEN, "clean string"]
    result = sanitize_json_input(given)
    assert result[0] == _C1_STRIPPED
    assert result[1] == "clean string"


def test_list_of_dicts_strips_c1_range_at_nesting() -> None:
    given = [{"a": _C1_LADEN}, {"b": {"c": _C1_LADEN}}]
    result = sanitize_json_input(given)
    assert result[0]["a"] == _C1_STRIPPED
    assert result[1]["b"]["c"] == _C1_STRIPPED


def test_nested_dict_strips_c1_range() -> None:
    given = {"outer": {"inner": _C1_LADEN}}
    result = sanitize_json_input(given)
    assert result["outer"]["inner"] == _C1_STRIPPED


def test_del_byte_is_stripped() -> None:
    given = {"task_title": f"before{_DEL}after"}
    result = sanitize_json_input(given)
    assert result["task_title"] == "beforeafter"


def test_string_json_with_raw_c1_bytes_is_stripped() -> None:
    """U+009B/U+009D are ordinary legal JSON string content (RFC 8259
    only requires escaping U+0000-U+001F) — reachable with no JSON
    escaping at all, unlike the C0 R4-F3 repro."""
    raw = '{"task_title": "before\x9bafter\x9dend"}'
    result = sanitize_json_input(raw)
    assert result["task_title"] == _C1_STRIPPED


def test_bytes_json_with_raw_c1_bytes_is_stripped() -> None:
    raw = 'before\x9bafter\x9dend'.encode("utf-8")
    payload = ('{"subject": "' + raw.decode("utf-8") + '"}').encode("utf-8")
    result = sanitize_json_input(payload)
    assert result["subject"] == _C1_STRIPPED


# ---------------------------------------------------------------------
# Regression guard: existing C0 stripping still works.
# ---------------------------------------------------------------------


def test_c0_control_bytes_still_stripped_regression_guard() -> None:
    given = {"task_title": _C0_LADEN}
    result = sanitize_json_input(given)
    assert result["task_title"] == _C0_STRIPPED


# ---------------------------------------------------------------------
# Happy path: legitimate non-ASCII text must survive unchanged.
# ---------------------------------------------------------------------


def test_happy_path_accented_latin_unchanged() -> None:
    text = "Café résumé naïve façade"
    given = {"task_title": text}
    result = sanitize_json_input(given)
    assert result["task_title"] == text


def test_happy_path_cjk_unchanged() -> None:
    text = "你好世界 こんにちは 안녕하세요"
    given = {"task_description": text}
    result = sanitize_json_input(given)
    assert result["task_description"] == text


def test_happy_path_normal_text_unchanged() -> None:
    given = {
        "task_title": "Refactor the widget loader",
        "task_description": "Nothing weird here, just plain prose.",
        "tags": ["a", "b", "c"],
    }
    result = sanitize_json_input(given)
    assert result == given


# ---------------------------------------------------------------------
# Live REST repro — POST /api/tasks task_title carrying raw C1 bytes.
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rest_task_title_c1_bytes_stripped_not_persisted(
    tmp_path,
) -> None:
    async with mcp_session(tmp_path) as admin:
        r = admin.post(
            "/api/tasks",
            json={
                "task_title": _C1_LADEN,
                "task_description": "repro R5-F8",
            },
        )
        assert r.status_code == 200, r.text
        payload = r.json()
        task_id = payload["task_id"]

        listing = admin.client.get("/api/tasks").json()
        matches = [t for t in listing if t.get("task_id") == task_id]
        assert matches, f"task {task_id} not found in listing"
        title = matches[0]["title"]
        assert _C1_CSI not in title, f"raw C1 CSI persisted verbatim: {title!r}"
        assert _C1_OSC not in title, f"raw C1 OSC persisted verbatim: {title!r}"
        assert title == _C1_STRIPPED, title
