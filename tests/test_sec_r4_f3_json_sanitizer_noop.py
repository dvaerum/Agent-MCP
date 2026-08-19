"""Security R4-F3 — ``sanitize_json_input`` is a no-op for the common case.

FINDING (owner-authorized pentest): ``sanitize_json_input``
(``agent_mcp/utils/json_utils.py``) fails to sanitize on the two paths
that matter most:

  1. When the input is already a Python ``dict``/``list`` — the case
     for EVERY MCP ``tools/call`` (the MCP SDK JSON-decodes the
     JSON-RPC body into a dict before ``registry.py`` ever sees it,
     see ``tools/registry.py`` ~474-481) — the function returned the
     input UNCHANGED. Zero sanitization, regardless of what control
     bytes the string leaves carry.

  2. For string/bytes input, the control-byte-stripping logic ("Step
     3") only ran on the FALLBACK path taken when the initial
     ``json.loads()`` ("Step 1") raised ``JSONDecodeError``. Standard
     JSON escapes (``\\u001b``, ``\\u0007``, ``\\u0000``, …) parse
     cleanly under Python's default ``strict=True`` — so Step 1
     SUCCEEDS on well-formed JSON carrying escaped control characters
     and returns before Step 3 ever runs. The sanitizer only fired on
     malformed JSON, the exceptional case, not the normal one.

Confirmed live repro (2 independent REST surfaces, pre-fix): POST
``/api/tasks`` with ``task_title`` containing ``\\u001b``/``\\u0007``/
``\\u0000`` escapes persisted the raw ESC/BEL/NUL bytes verbatim to the
DB + API response. Same for POST ``/api/messages``' ``subject`` field.

Fix: a single shared recursive walk-and-strip helper is applied
UNCONDITIONALLY — after a successful Step-1 parse of string/bytes
input, and to already-parsed dict/list input — stripping the same C0
control-byte set the existing regex targeted (``\\x00-\\x08``,
``\\x0B``, ``\\x0C``, ``\\x0E-\\x1F``; ``\\t``/``\\n``/``\\r`` are
whitespace, not control bytes worth stripping, and are preserved).
"""

from __future__ import annotations

import pytest

from agent_mcp.utils.json_utils import sanitize_json_input
from tests.harness import mcp_session

_CONTROL_LADEN = "hello\x1bworld\x07bell\x00nul"
_CONTROL_STRIPPED = "helloworldbellnul"


# ---------------------------------------------------------------------
# Unit-level: sanitize_json_input direct calls (covers the MCP dict-input
# path — tools/registry.py hands sanitize_json_input an already-decoded
# dict, never a raw string).
# ---------------------------------------------------------------------


def test_dict_input_strips_control_bytes_from_string_leaves() -> None:
    """The dict short-circuit must not just return the input unchanged."""
    given = {"task_title": _CONTROL_LADEN, "nested": {"x": _CONTROL_LADEN}}
    result = sanitize_json_input(given)
    assert result["task_title"] == _CONTROL_STRIPPED
    assert result["nested"]["x"] == _CONTROL_STRIPPED


def test_list_input_strips_control_bytes_from_string_leaves() -> None:
    given = [_CONTROL_LADEN, {"a": [_CONTROL_LADEN]}]
    result = sanitize_json_input(given)
    assert result[0] == _CONTROL_STRIPPED
    assert result[1]["a"][0] == _CONTROL_STRIPPED


def test_dict_input_leaves_non_string_leaves_untouched() -> None:
    given = {"count": 3, "ok": True, "ratio": 1.5, "nothing": None}
    result = sanitize_json_input(given)
    assert result == given


def test_string_json_with_valid_unicode_escapes_is_still_stripped() -> None:
    """Step 1 (json.loads) succeeds on standard \\u-escaped control
    characters — that success must not skip sanitization."""
    raw = '{"task_title": "hello\\u001bworld\\u0007bell\\u0000nul"}'
    result = sanitize_json_input(raw)
    assert result["task_title"] == _CONTROL_STRIPPED


def test_bytes_json_with_valid_unicode_escapes_is_still_stripped() -> None:
    raw = '{"subject": "hello\\u001bworld\\u0007bell\\u0000nul"}'.encode("utf-8")
    result = sanitize_json_input(raw)
    assert result["subject"] == _CONTROL_STRIPPED


def test_happy_path_multiline_whitespace_preserved() -> None:
    """Legitimate whitespace (newline/tab) in a multi-line description
    must NOT be mangled — only C0 control bytes are stripped."""
    text = "line one\nline two\tindented\r\nline three"
    given = {"task_description": text}
    result = sanitize_json_input(given)
    assert result["task_description"] == text

    raw = '{"task_description": "line one\\nline two\\tindented\\r\\nline three"}'
    result_str = sanitize_json_input(raw)
    assert result_str["task_description"] == text


def test_happy_path_normal_text_unchanged() -> None:
    given = {
        "task_title": "Refactor the widget loader",
        "task_description": "Nothing weird here, just plain prose.",
        "tags": ["a", "b", "c"],
    }
    result = sanitize_json_input(given)
    assert result == given


# ---------------------------------------------------------------------
# Live REST repro — POST /api/tasks task_title, POST /api/messages subject.
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rest_task_title_control_bytes_stripped_not_persisted(
    tmp_path,
) -> None:
    async with mcp_session(tmp_path) as admin:
        r = admin.post(
            "/api/tasks",
            json={
                "task_title": _CONTROL_LADEN,
                "task_description": "repro R4-F3",
            },
        )
        assert r.status_code == 200, r.text
        payload = r.json()
        task_id = payload["task_id"]

        listing = admin.client.get("/api/tasks").json()
        matches = [t for t in listing if t.get("task_id") == task_id]
        assert matches, f"task {task_id} not found in listing"
        title = matches[0]["title"]
        assert "\x1b" not in title, f"raw ESC byte persisted verbatim: {title!r}"
        assert "\x07" not in title, f"raw BEL byte persisted verbatim: {title!r}"
        assert "\x00" not in title, f"raw NUL byte persisted verbatim: {title!r}"
        assert title == _CONTROL_STRIPPED, title


@pytest.mark.asyncio
async def test_rest_message_subject_control_bytes_stripped_not_persisted(
    tmp_path,
) -> None:
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        r = admin.post(
            "/api/messages",
            json={
                "recipient_id": "alice",
                "message_content": "repro R4-F3",
                "subject": _CONTROL_LADEN,
            },
        )
        assert r.status_code == 200, r.text
        payload = r.json()
        message_id = payload["message_id"]

        r2 = admin.post("/api/messages/query", json={})
        assert r2.status_code == 200, r2.text
        matches = [
            m for m in r2.json()["messages"] if m["message_id"] == message_id
        ]
        assert matches, f"message {message_id} not found in query"
        subject = matches[0].get("subject")
        assert subject is not None
        assert "\x1b" not in subject, f"raw ESC byte persisted verbatim: {subject!r}"
        assert "\x07" not in subject, f"raw BEL byte persisted verbatim: {subject!r}"
        assert "\x00" not in subject, f"raw NUL byte persisted verbatim: {subject!r}"
        assert subject == _CONTROL_STRIPPED, subject
