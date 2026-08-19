"""F005 verify-all-v6 MUTATING #3 — reject unsafe-unicode memory keys.

The verify-all-v6 mutating round POSTed
``{"context_key": "u‮ ﻿\U0001F680key", "context_value": "val"}``
to ``/api/memories`` and got a 200 — the row landed in
``project_context`` with raw NULL / RTL-override / BOM bytes in the
primary-key field. That's a spoofing vector: a key like
``config‮drowssap`` renders in the dashboard as
``configpassword`` (the U+202E RIGHT-TO-LEFT OVERRIDE flips display
order) while storing/searching as the original. Combined with NULL
chars truncating shell tooling and BOM being invisible, this is a
real attack surface for any operator reading memory keys.

Tests below pin:

* Disallowed characters → 400, no row in the DB.
* Legitimate Unicode (accents, CJK, emoji) → 200, row written.
* PUT (``update_memory_api_route``) inherits the same validation
  via its URL path parameter.

Validator lives in ``agent_mcp/utils/string_utils.py``; the error
envelope (``UNSAFE_KEY_ERROR``) is the single source of truth so the
test, the create handler, and the update handler stay in sync.
"""

from __future__ import annotations

import pytest

from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio


# ── Disallowed inputs (must 400) ─────────────────────────────────────

# Each tuple: (id, key). The id is what pytest prints in
# parametrised-test names — using the codepoint label rather than the
# raw character makes test output readable.
# R4-F3: null_byte / control_soh (C0 control range, \x00-\x1F) are
# deliberately NOT in this list anymore. ``sanitize_json_input``
# (utils/json_utils.py) now strips those bytes UNCONDITIONALLY at the
# shared JSON-input chokepoint every REST/MCP body flows through -- so
# this route never observes them intact; the unsafe-key validator
# below cannot reject what it never sees. That is a STRONGER guarantee
# than this file originally pinned (every string field is covered, not
# just memory keys) via silent normalization instead of rejection. See
# _CONTROL_BYTE_KEYS + test_create_memory_control_byte_key_is_silently_
# sanitized below for the pinned new behavior, and
# test_has_unsafe_unicode_for_identifier_unit for proof the validator
# itself is unchanged (it still flags these bytes when it sees them).
#
# R5-F8: DEL (\x7F) moved out of this list to _CONTROL_BYTE_KEYS below
# for the same reason -- _CONTROL_BYTE_RE now covers DEL + the C1
# range (\x80-\x9F) alongside C0, so DEL (and C1 bytes like CSI
# \x9B) are silently sanitized upstream too, same as
# null_byte/control_soh.
_DISALLOWED_KEYS = [
    ("rtl_override",      "config‮drowssap"),   # the spoofing case
    ("zero_width_space",  "a\u200bb"),
    ("bom",               "a﻿b"),
    ("lri_isolate",       "a⁦b"),
    ("line_separator",    "a b"),
    ("word_joiner",       "a⁠b"),
    ("deprecated_bidi",   "a⁯b"),
]

# R4-F3/R5-F8: control bytes (C0, DEL, C1) are stripped by the
# JSON-input sanitizer before the unsafe-key validator runs, so a
# request carrying one no longer 400s -- see
# test_create_memory_control_byte_key_is_silently_sanitized below.
# Kept as its own list so the direct-validator unit test (which
# bypasses the JSON chokepoint entirely) still exercises them.
_CONTROL_BYTE_KEYS = [
    ("null_byte",   "a\x00b"),
    ("control_soh", "a\x01b"),
    ("del",         "a\x7fb"),
]


@pytest.mark.parametrize("label,bad_key", _DISALLOWED_KEYS, ids=[t[0] for t in _DISALLOWED_KEYS])
async def test_create_memory_rejects_unsafe_unicode_key(
    tmp_path, label: str, bad_key: str,
) -> None:
    """POST /api/memories with a disallowed-unicode key must 400.

    Also asserts the row never landed in ``project_context`` — a 400
    that nevertheless commits is the worst of both worlds.
    """
    async with mcp_session(tmp_path) as admin:
        r = admin.client.post(
            "/api/memories",
            json={"context_key": bad_key, "context_value": "val"},
            headers={"Authorization": f"Bearer {admin.admin_token}"},
        )
        assert r.status_code == 400, (
            f"{label}: expected 400 for key {bad_key!r}, "
            f"got {r.status_code} body={r.text!r}"
        )
        body = r.json()
        assert body.get("error") == "invalid_key_character", (
            f"{label}: expected error=invalid_key_character, "
            f"got {body!r}"
        )
        # Belt-and-braces: the row must NOT be in the DB.
        from agent_mcp.db.engine import SessionLocal
        from agent_mcp.db.models import ProjectContext

        sess = SessionLocal()
        try:
            row = (
                sess.query(ProjectContext)
                .filter(ProjectContext.context_key == bad_key)
                .one_or_none()
            )
            assert row is None, (
                f"{label}: 400 was returned but the row was committed: {row!r}"
            )
        finally:
            sess.close()


@pytest.mark.parametrize(
    "label,bad_key", _CONTROL_BYTE_KEYS, ids=[t[0] for t in _CONTROL_BYTE_KEYS]
)
async def test_create_memory_control_byte_key_is_silently_sanitized(
    tmp_path, label: str, bad_key: str,
) -> None:
    """R4-F3/R5-F8: a control byte (C0, DEL, or C1) embedded in
    ``context_key`` is stripped by the shared JSON-input sanitizer
    BEFORE this route ever sees it — the request now succeeds with the
    SANITIZED key, rather than being rejected by the unsafe-key
    validator (which never observes the raw byte). The invariant this
    file exists to pin still holds: no raw control byte ever reaches
    ``project_context.context_key`` — it's just enforced by upstream
    normalization instead of rejection.

    Reuses the production ``_CONTROL_BYTE_RE`` rather than a third copy
    of the character class, so this test can't silently drift out of
    sync with what the sanitizer actually strips (the R5-F8 bug class).
    """
    from agent_mcp.utils.json_utils import _CONTROL_BYTE_RE

    sanitized_key = _CONTROL_BYTE_RE.sub("", bad_key)

    async with mcp_session(tmp_path) as admin:
        r = admin.client.post(
            "/api/memories",
            json={"context_key": bad_key, "context_value": "val"},
            headers={"Authorization": f"Bearer {admin.admin_token}"},
        )
        assert r.status_code == 200, (
            f"{label}: expected 200 (sanitized upstream), "
            f"got {r.status_code} body={r.text!r}"
        )
        from agent_mcp.db.engine import SessionLocal
        from agent_mcp.db.models import ProjectContext

        sess = SessionLocal()
        try:
            row = (
                sess.query(ProjectContext)
                .filter(ProjectContext.context_key == sanitized_key)
                .one_or_none()
            )
            assert row is not None, (
                f"{label}: 200 returned but sanitized key "
                f"{sanitized_key!r} was not found"
            )
            assert "\x00" not in row.context_key
            assert "\x01" not in row.context_key
        finally:
            sess.close()


# ── Allowed inputs (must 200) ────────────────────────────────────────
# The memory-key charset is now the ASCII allowlist ^[A-Za-z0-9._/-]+$
# (string_utils.MEMORY_KEY_RE) — tighter than the old bidi-only denylist.
# '/' is allowed (namespacing); '.' '_' '-' too.

_ALLOWED_KEYS = [
    ("ascii_dotted",   "a.b.c"),
    ("slashed",        "ios/repo"),
    ("hyphen_under",   "a-b_c.d"),
]


@pytest.mark.parametrize("label,good_key", _ALLOWED_KEYS, ids=[t[0] for t in _ALLOWED_KEYS])
async def test_create_memory_accepts_conforming_key(
    tmp_path, label: str, good_key: str,
) -> None:
    """POST /api/memories with an ASCII-allowlist-conforming key
    (letters/digits/. _ / -) succeeds and the row lands."""
    async with mcp_session(tmp_path) as admin:
        r = admin.client.post(
            "/api/memories",
            json={"context_key": good_key, "context_value": "val"},
            headers={"Authorization": f"Bearer {admin.admin_token}"},
        )
        assert r.status_code == 200, (
            f"{label}: expected 200 for key {good_key!r}, "
            f"got {r.status_code} body={r.text!r}"
        )
        from agent_mcp.db.engine import SessionLocal
        from agent_mcp.db.models import ProjectContext

        sess = SessionLocal()
        try:
            row = (
                sess.query(ProjectContext)
                .filter(ProjectContext.context_key == good_key)
                .one_or_none()
            )
            assert row is not None, (
                f"{label}: 200 was returned but the row did not commit"
            )
        finally:
            sess.close()


# ── Non-ASCII inputs now REJECTED (contract change: ASCII-only) ──────
# Accents / CJK / emoji were permitted by the old bidi-only denylist but
# are rejected by the ASCII allowlist (operator decision 2026-07-15:
# keys are URL path segments; ASCII-only removes homograph/encoding
# ambiguity like café vs cafe).

_NON_ASCII_KEYS = [
    ("latin1_accent",  "caf\xe9.config"),
    ("cjk",            "测试.key"),
    ("emoji",          "emoji.\U0001f680.key"),
]


@pytest.mark.parametrize("label,bad_key", _NON_ASCII_KEYS, ids=[t[0] for t in _NON_ASCII_KEYS])
async def test_create_memory_rejects_non_ascii_key(
    tmp_path, label: str, bad_key: str,
) -> None:
    """POST /api/memories with a non-ASCII key must now 400 (ASCII
    allowlist) and no row lands."""
    async with mcp_session(tmp_path) as admin:
        r = admin.client.post(
            "/api/memories",
            json={"context_key": bad_key, "context_value": "val"},
            headers={"Authorization": f"Bearer {admin.admin_token}"},
        )
        assert r.status_code == 400, (
            f"{label}: expected 400 for non-ASCII key {bad_key!r}, "
            f"got {r.status_code} body={r.text!r}"
        )
        from agent_mcp.db.engine import SessionLocal
        from agent_mcp.db.models import ProjectContext

        sess = SessionLocal()
        try:
            row = (
                sess.query(ProjectContext)
                .filter(ProjectContext.context_key == bad_key)
                .one_or_none()
            )
            assert row is None, (
                f"{label}: 400 returned but the row was committed: {row!r}"
            )
        finally:
            sess.close()


# ── PUT (update_memory_api_route) inherits the same validation ───────


async def test_update_memory_rejects_unsafe_unicode_key_in_url(
    tmp_path,
) -> None:
    """PUT /api/memories/<key> with an unsafe-unicode key in the URL
    path must 400, not silently fall through to the "not found"
    branch (and certainly not perform an UPSERT).

    The handler extracts the key from ``request.url.path.split('/')``;
    Starlette URL-decodes the path before passing it through, so a
    ``%E2%80%AE`` (U+202E) in the URL re-emerges as a real RTL char
    in the handler.
    """
    async with mcp_session(tmp_path) as admin:
        # The URL-encoded form of the RTL-override spoofing key.
        import urllib.parse
        bad_key = "config‮drowssap"
        encoded = urllib.parse.quote(bad_key, safe="")
        r = admin.client.put(
            f"/api/memories/{encoded}",
            json={"context_value": "val"},
            headers={"Authorization": f"Bearer {admin.admin_token}"},
        )
        assert r.status_code == 400, (
            f"PUT with RTL-override key: expected 400, "
            f"got {r.status_code} body={r.text!r}"
        )
        body = r.json()
        assert body.get("error") == "invalid_key_character", body


# ── Unit-test the validator directly (no HTTP round-trip needed) ─────


async def test_has_unsafe_unicode_for_identifier_unit() -> None:
    """Lightweight unit coverage for the helper. The integration tests
    above hit it through the HTTP path; this guard pins the helper's
    contract so refactors that hide the regex behind a different
    surface still satisfy the same shape.

    Includes ``_CONTROL_BYTE_KEYS`` (R4-F3): called directly here, the
    validator still correctly flags C0 control bytes — it's only the
    HTTP round-trip that no longer observes them, because the shared
    JSON-input sanitizer strips them one layer earlier now."""
    from agent_mcp.utils.string_utils import (
        has_unsafe_unicode_for_identifier,
    )

    # All disallowed cases reject.
    for _label, bad_key in _DISALLOWED_KEYS + _CONTROL_BYTE_KEYS:
        assert has_unsafe_unicode_for_identifier(bad_key), (
            f"helper must reject {bad_key!r}"
        )

    # All allowed cases pass.
    for _label, good_key in _ALLOWED_KEYS:
        assert not has_unsafe_unicode_for_identifier(good_key), (
            f"helper must accept {good_key!r}"
        )

    # Non-string input is treated as "no unsafe codepoint" — the
    # caller is responsible for type validation; the helper is a
    # pure character-set check.
    assert not has_unsafe_unicode_for_identifier(None)  # type: ignore[arg-type]
    assert not has_unsafe_unicode_for_identifier(123)   # type: ignore[arg-type]
