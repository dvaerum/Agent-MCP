"""Security R13-F2 — hidden/spoofing Unicode survives sanitize_json_input's
fast paths (live-exploited).

FINDING: ``sanitize_json_input``'s "Step 4" (``agent_mcp/utils/json_utils.py``)
strips hidden/spoofing Unicode -- zero-width spaces, BOM, line/paragraph
separators (``\\u200B-\\u200F``, ``\\uFEFF``, ``\\u2028``, ``\\u2029``) -- but
only inside the aggressive-cleanup fallback branch, reached ONLY when the
initial ``json.loads()`` raises ``JSONDecodeError``. Every well-formed
request -- virtually all real traffic -- returns early via one of two fast
paths that never reach Step 4:

  1. The already-parsed dict/list path (the MCP ``tools/call`` case: the
     SDK JSON-decodes the body before ``dispatch_tool_call`` runs).
  2. Step 1, the string/bytes path when ``json.loads()`` succeeds on the
     first try -- virtually every REST body.

Both fast paths call ``_strip_control_bytes``, which only covers C0/C1/DEL
control bytes (R4-F3/R5-F8) -- NOT this hidden-Unicode character set. Same
bug shape as R4-F3 (sanitizer correct in the fallback branch, silently
skipped on the common path), on a sibling character class R4-F3's fix
never swept.

Confirmed live: a task title with a trailing zero-width space
(``"pentest-fuzz-target-r13b\\u200b"``) was accepted and stored --
visually indistinguishable from an existing title but a distinct string
(duplicate-identifier / visual-spoofing risk). Sent via
``bulk_task_operations``'s ``add_note`` over MCP, read back over REST with
codepoints intact.

Confirmed NOT affected: ``register_agent``'s ``name`` field, gated by its
own ``_AGENT_ID_RE`` (``agent_mcp/repositories/agent_repository.py``),
which already rejects this payload with a 400/tool-error -- no change
needed there.

Fix: hoist the Step 4 regex to module scope (mirroring
``_CONTROL_BYTE_RE``) and fold the strip into ``_strip_control_bytes``
itself so it runs UNCONDITIONALLY on every string leaf, on both fast
paths.
"""

from __future__ import annotations

import pytest

from agent_mcp.utils.json_utils import sanitize_json_input
from tests.harness import mcp_session

_ZWSP = "\u200b"  # zero-width space
_ZWNJ = "‌"  # zero-width non-joiner
_BOM = "﻿"  # byte order mark / zero-width no-break space
_LINE_SEP = " "  # line separator
_PARA_SEP = " "  # paragraph separator

_HIDDEN_LADEN = f"before{_ZWSP}mid{_ZWNJ}dle{_BOM}after{_LINE_SEP}x{_PARA_SEP}y"
_HIDDEN_STRIPPED = "beforemiddleafterxy"


# ---------------------------------------------------------------------
# Fast path #1: already-parsed dict/list (MCP tools/call path).
# ---------------------------------------------------------------------


def test_dict_input_strips_hidden_unicode_from_string_leaves() -> None:
    """The dict fast path must strip hidden Unicode, not just control bytes."""
    given = {"task_title": _HIDDEN_LADEN, "nested": {"x": _HIDDEN_LADEN}}
    result = sanitize_json_input(given)
    assert result["task_title"] == _HIDDEN_STRIPPED
    assert result["nested"]["x"] == _HIDDEN_STRIPPED


def test_list_input_strips_hidden_unicode_from_string_leaves() -> None:
    given = [_HIDDEN_LADEN, {"a": [_HIDDEN_LADEN]}]
    result = sanitize_json_input(given)
    assert result[0] == _HIDDEN_STRIPPED
    assert result[1]["a"][0] == _HIDDEN_STRIPPED


def test_dict_input_trailing_zwsp_stripped() -> None:
    """Exact confirmed repro shape: a trailing zero-width space on an
    otherwise-normal title."""
    given = {"task_title": f"pentest-fuzz-target-r13b{_ZWSP}"}
    result = sanitize_json_input(given)
    assert result["task_title"] == "pentest-fuzz-target-r13b"
    assert _ZWSP not in result["task_title"]


# ---------------------------------------------------------------------
# Fast path #2: Step 1, well-formed JSON string/bytes that parses cleanly
# on the first json.loads() attempt.
# ---------------------------------------------------------------------


def test_string_json_well_formed_strips_hidden_unicode() -> None:
    """Step 1 (json.loads succeeds first try) must not skip the hidden-
    Unicode strip -- the well-formed-JSON case is virtually all real
    REST traffic."""
    raw = (
        '{"task_title": "before\\u200bmid\\u200cdle\\ufeffafter'
        '\\u2028x\\u2029y"}'
    )
    result = sanitize_json_input(raw)
    assert result["task_title"] == _HIDDEN_STRIPPED


def test_bytes_json_well_formed_strips_hidden_unicode() -> None:
    raw = (
        b'{"subject": "before\\u200bmid\\u200cdle\\ufeffafter'
        b'\\u2028x\\u2029y"}'
    )
    result = sanitize_json_input(raw)
    assert result["subject"] == _HIDDEN_STRIPPED


def test_string_json_raw_hidden_unicode_bytes_stripped() -> None:
    """Hidden Unicode as raw (unescaped) UTF-8 bytes in an otherwise
    well-formed JSON string -- also legal JSON content, reachable with
    zero escaping."""
    raw = '{"task_title": "before' + _ZWSP + 'after' + _BOM + 'end"}'
    result = sanitize_json_input(raw)
    assert result["task_title"] == "beforeafterend"


# ---------------------------------------------------------------------
# Regression guards: existing behavior must still hold.
# ---------------------------------------------------------------------


def test_malformed_json_fallback_path_still_strips_hidden_unicode() -> None:
    """The pre-existing malformed-JSON fallback path must keep stripping
    hidden Unicode after the hoist/removal. A raw (unescaped) BEL byte
    is invalid inside a JSON string per RFC 8259 -- it forces Step 1's
    ``json.loads`` to raise ``JSONDecodeError`` and fall through to the
    aggressive-cleanup path, alongside the hidden-Unicode chars under
    test."""
    raw = "before" + _ZWSP + "x\x07y" + _BOM + "z"
    payload = '{"task_title": "' + raw + '"}'
    result = sanitize_json_input(payload)
    assert result["task_title"] == "beforexyz"


def test_c0_control_bytes_still_stripped_regression_guard() -> None:
    given = {"task_title": "hello\x1bworld\x07bell\x00nul"}
    result = sanitize_json_input(given)
    assert result["task_title"] == "helloworldbellnul"


def test_happy_path_normal_text_unchanged() -> None:
    given = {
        "task_title": "Refactor the widget loader",
        "task_description": "Nothing weird here, just plain prose.",
        "tags": ["a", "b", "c"],
    }
    result = sanitize_json_input(given)
    assert result == given


def test_happy_path_accented_and_cjk_unchanged() -> None:
    text = "Café résumé naïve façade 你好世界 こんにちは"
    given = {"task_title": text}
    result = sanitize_json_input(given)
    assert result["task_title"] == text


# ---------------------------------------------------------------------
# Confirmed-NOT-affected: register_agent's name regex still rejects.
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_agent_name_with_zwsp_normalized_not_rejected(
    tmp_path,
) -> None:
    """``dispatch_tool_call`` runs ``sanitize_json_input`` on EVERY tool's
    arguments -- including ``register_agent``'s -- before the tool's own
    ``_AGENT_ID_RE`` validation ever sees them. That ordering predates
    R13-F2: a C0 control byte in ``name`` was already silently stripped
    before validation (R4-F3/R5-F8), so ``"alice\\x01"`` already
    registered as clean ``"alice"``, not rejected. R13-F2 extends the
    exact same normalize-then-validate behavior to hidden Unicode, so
    ``"pentest-fuzz\\u200b"`` now likewise registers as clean
    ``"pentest-fuzz"`` -- consistent with the established pattern, not a
    new gap (the hidden char never reaches storage either way)."""
    async with mcp_session(tmp_path) as admin:
        result = await admin.call(
            "register_agent", {"name": f"pentest-fuzz{_ZWSP}"}
        )
        assert not admin._last_is_error, (
            "register_agent should accept the sanitizer-normalized name "
            f"(no raw hidden Unicode reaches _AGENT_ID_RE): {result!r}"
        )

        listing = admin.client.get("/api/agents").json()
        ids = [a.get("agent_id") for a in listing]
        assert "pentest-fuzz" in ids, ids
        assert not any(_ZWSP in (i or "") for i in ids), ids


@pytest.mark.asyncio
async def test_register_agent_name_with_uppercase_still_rejected(
    tmp_path,
) -> None:
    """Regression guard: _AGENT_ID_RE still rejects genuinely-invalid
    names that remain invalid after sanitization (this fix only strips
    hidden Unicode / control bytes -- it must not loosen the regex
    itself)."""
    async with mcp_session(tmp_path) as admin:
        result = await admin.call("register_agent", {"name": "PentestFuzz"})
        assert admin._last_is_error, (
            "register_agent should reject an uppercase name regardless "
            f"of sanitization: {result!r}"
        )


# ---------------------------------------------------------------------
# Live MCP -> REST repro: bulk_task_operations add_note carrying a
# trailing ZWSP, read back over REST with codepoints intact (pre-fix).
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_task_title_hidden_unicode_stripped_not_persisted(
    tmp_path,
) -> None:
    async with mcp_session(tmp_path) as admin:
        r = admin.post(
            "/api/tasks",
            json={
                "task_title": f"pentest-fuzz-target-r13b{_ZWSP}",
                "task_description": "repro R13-F2",
            },
        )
        assert r.status_code == 200, r.text
        payload = r.json()
        task_id = payload["task_id"]

        listing = admin.client.get("/api/tasks").json()
        matches = [t for t in listing if t.get("task_id") == task_id]
        assert matches, f"task {task_id} not found in listing"
        title = matches[0]["title"]
        assert _ZWSP not in title, f"raw ZWSP persisted verbatim: {title!r}"
        assert title == "pentest-fuzz-target-r13b", title
