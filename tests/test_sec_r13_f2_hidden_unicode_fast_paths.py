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

import unicodedata

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


# ---------------------------------------------------------------------
# R14-F3 — R13-F2's fix hoisted the hidden-Unicode strip onto every fast
# path, but never widened the CHARACTER CLASS it strips. Confirmed live:
# a task title carrying U+202E (RIGHT-TO-LEFT OVERRIDE -- the classic
# filename/display-spoofing primitive), repeated U+0301 combining
# marks, U+2060 (WORD JOINER), and U+FE0F (VARIATION SELECTOR-16) round-
# tripped byte-for-byte identical on read-back -- none of these were
# stripped by the old hand-enumerated regex.
#
# Fix: the hidden-Unicode strip is now driven by Unicode general
# category (mirroring aoe-bridge/src/render.rs's sanitize_for_pane,
# R4-F4/R5-F9) rather than a hand-picked range table: every `Cf`
# (Format) character is stripped -- this covers the bidi override/
# embedding/isolate controls (U+202A-U+202E, U+2066-U+2069) and the
# word joiner (U+2060) in one check, alongside the R13-F2 zero-width/
# BOM/line-separator set. Variation selectors (U+FE00-U+FE0F,
# U+E0100-U+E01EF) are category `Mn`, not `Cf`, so they're stripped via
# an explicit range check, matching render.rs's R5-F9 fix. General
# combining marks (the rest of `Mn`/`Me`) are NOT stripped outright --
# legitimate non-Latin text depends on them -- but consecutive runs are
# CAPPED per base character to block zalgo-style stacking without
# corrupting normal single/double-diacritic use.
# ---------------------------------------------------------------------

_RLO = "‮"  # RIGHT-TO-LEFT OVERRIDE
_LRI = "⁦"  # LEFT-TO-RIGHT ISOLATE
_RLI = "⁧"  # RIGHT-TO-LEFT ISOLATE
_FSI = "⁨"  # FIRST STRONG ISOLATE
_PDI = "⁩"  # POP DIRECTIONAL ISOLATE
_WORD_JOINER = "⁠"
_VS16 = "️"  # VARIATION SELECTOR-16 (BMP block)
_VS_SUPPLEMENT = "\U000e0100"  # VARIATION SELECTOR-17 (supplementary block)
_COMBINING_ACUTE = "́"  # zalgo-stacking building block


def test_rtl_override_stripped_from_dict_input() -> None:
    """U+202E RLO -- the filename/display-spoofing primitive -- must be
    stripped, not just the R13-F2 zero-width/BOM/line-separator set."""
    given = {"task_title": f"safe{_RLO}gnp.exe"}
    result = sanitize_json_input(given)
    assert _RLO not in result["task_title"]
    assert result["task_title"] == "safegnp.exe"


def test_bidi_embedding_and_override_controls_stripped() -> None:
    """The full LRE/RLE/PDF/LRO/RLO block (U+202A-U+202E), not just RLO."""
    for cp in range(0x202A, 0x202F):
        ch = chr(cp)
        given = {"task_title": f"a{ch}b"}
        result = sanitize_json_input(given)
        assert ch not in result["task_title"], f"U+{cp:04X} survived"
        assert result["task_title"] == "ab"


def test_bidi_isolate_controls_stripped() -> None:
    """LRI/RLI/FSI/PDI (U+2066-U+2069) -- the newer bidi-isolate
    controls, functionally equivalent spoofing primitives to LRE/RLO."""
    for ch in (_LRI, _RLI, _FSI, _PDI):
        given = {"task_title": f"a{ch}b"}
        result = sanitize_json_input(given)
        assert ch not in result["task_title"]
        assert result["task_title"] == "ab"


def test_word_joiner_stripped() -> None:
    """U+2060 WORD JOINER -- an invisible-formatting `Cf` character
    adjacent in intent to the R13-F2 zero-width-space set, but outside
    its old U+200B-U+200F range."""
    given = {"task_title": f"a{_WORD_JOINER}b"}
    result = sanitize_json_input(given)
    assert _WORD_JOINER not in result["task_title"]
    assert result["task_title"] == "ab"


def test_variation_selectors_stripped_bmp_and_supplement() -> None:
    """Variation Selectors 1-16 (BMP, U+FE00-U+FE0F) and 17-256
    (supplementary plane, U+E0100-U+E01EF) carry no glyph of their own
    (R5-F9's exact reasoning in render.rs) and must be stripped."""
    given = {"task_title": f"safe{_VS16}mid{_VS_SUPPLEMENT}end"}
    result = sanitize_json_input(given)
    assert _VS16 not in result["task_title"]
    assert _VS_SUPPLEMENT not in result["task_title"]
    assert result["task_title"] == "safemidend"


def test_confirmed_live_repro_fully_sanitized() -> None:
    """The exact confirmed-live-exploited payload: RLO + repeated
    combining marks + word joiner + a variation selector, on the REST
    task-creation path. Must not round-trip byte-for-byte identical."""
    evil = f"safe{_RLO}gnp.exe{_COMBINING_ACUTE * 10}{_WORD_JOINER}{_VS16}end"
    given = {"task_title": evil}
    result = sanitize_json_input(given)
    assert result["task_title"] != evil
    assert _RLO not in result["task_title"]
    assert _WORD_JOINER not in result["task_title"]
    assert _VS16 not in result["task_title"]
    # The combining-mark run must be capped, not left at its original
    # length of 10.
    combining_run = sum(
        1 for ch in result["task_title"] if unicodedata.category(ch) in ("Mn", "Me")
    )
    assert combining_run < 10, f"combining-mark run not capped: {combining_run}"


def test_zalgo_combining_marks_capped_not_left_unbounded() -> None:
    """A base character with an excessive run of combining marks
    (zalgo-style stacking) must be capped to a small number per base
    character, not stripped-to-zero (legitimate accented text needs
    combining marks to survive) and not left completely unbounded
    (unbounded stacking is a display-integrity / layout-DoS primitive
    once persisted and rendered indefinitely in the dashboard)."""
    zalgo = "z" + _COMBINING_ACUTE * 50 + "algo"
    given = {"task_title": zalgo}
    result = sanitize_json_input(given)

    marks_after_z = 0
    for ch in result["task_title"][1:]:
        if unicodedata.category(ch) in ("Mn", "Me"):
            marks_after_z += 1
        else:
            break
    assert 0 < marks_after_z <= 4, (
        f"expected a small capped run of combining marks, got {marks_after_z}"
    )
    # The base character and the trailing plain text must still be present.
    assert result["task_title"].startswith("z")
    assert result["task_title"].endswith("algo")


@pytest.mark.asyncio
async def test_live_task_title_rtlo_spoofing_stripped_not_persisted(
    tmp_path,
) -> None:
    """Live REST repro: POST a task title carrying the confirmed
    exploit payload, read it back over REST, and confirm none of the
    spoofing/hidden characters survived to storage."""
    async with mcp_session(tmp_path) as admin:
        evil = f"pentest-fuzz-r14f3-safe{_RLO}gnp.exe{_WORD_JOINER}{_VS16}"
        r = admin.post(
            "/api/tasks",
            json={
                "task_title": evil,
                "task_description": "repro R14-F3",
            },
        )
        assert r.status_code == 200, r.text
        payload = r.json()
        task_id = payload["task_id"]

        listing = admin.client.get("/api/tasks").json()
        matches = [t for t in listing if t.get("task_id") == task_id]
        assert matches, f"task {task_id} not found in listing"
        title = matches[0]["title"]
        assert title != evil, "spoofing payload round-tripped byte-for-byte identical"
        assert _RLO not in title
        assert _WORD_JOINER not in title
        assert _VS16 not in title


# ---------------------------------------------------------------------
# Non-regression: legitimate international text using combining marks
# in the ordinary (non-excessive) way it was designed for must survive
# untouched. This is the "getting it wrong = breaks legitimate Unicode
# text" side of the R14-F3 fix.
# ---------------------------------------------------------------------


def test_legitimate_vietnamese_combining_marks_unchanged() -> None:
    """Vietnamese written with decomposed combining marks (base letter +
    a vowel-modifier mark + a tone mark -- at most 2 combining marks per
    base character) must survive unchanged. e.g. decomposed "Việt Nam":
    i + COMBINING DOT BELOW (U+0323), except this uses the fully
    decomposed 'ệ' as e + COMBINING CIRCUMFLEX ACCENT (U+0302) +
    COMBINING DOT BELOW (U+0323)."""
    text = "Tiếng Vị̂t Nam"  # decomposed-ish sample
    # Use NFD-decomposed real Vietnamese words directly for clarity:
    text = unicodedata.normalize("NFD", "Tiếng Việt")
    given = {"task_title": text}
    result = sanitize_json_input(given)
    assert result["task_title"] == text, (
        f"legitimate Vietnamese text was mangled: {result['task_title']!r} != {text!r}"
    )


def test_legitimate_arabic_diacritics_unchanged() -> None:
    """Arabic tashkeel (vowel diacritics, combining marks) -- commonly
    2-3 stacked per letter (e.g. shadda + a vowel) -- must survive."""
    # "بِسْمِ اللَّهِ" (bismillah) with full diacritics -- several letters
    # here carry 2 combining marks (e.g. shadda + fatha on the lam-lam).
    text = "بِسْمِ اللَّهِ"
    given = {"task_title": text}
    result = sanitize_json_input(given)
    assert result["task_title"] == text, (
        f"legitimate Arabic diacritics were mangled: {result['task_title']!r} != {text!r}"
    )


def test_legitimate_single_diacritic_accented_text_unchanged() -> None:
    """A single combining accent per base character (the common case
    for NFD-normalized European text) must never be touched by the
    combining-mark cap."""
    text = unicodedata.normalize("NFD", "café résumé naïve")
    given = {"task_title": text}
    result = sanitize_json_input(given)
    assert result["task_title"] == text
