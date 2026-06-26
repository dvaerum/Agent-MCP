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
_DISALLOWED_KEYS = [
    ("null_byte",         "a\x00b"),
    ("control_soh",       "a\x01b"),
    ("del",               "a\x7fb"),
    ("rtl_override",      "config‮drowssap"),   # the spoofing case
    ("zero_width_space",  "a​b"),
    ("bom",               "a﻿b"),
    ("lri_isolate",       "a⁦b"),
    ("line_separator",    "a b"),
    ("word_joiner",       "a⁠b"),
    ("deprecated_bidi",   "a⁯b"),
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


# ── Legitimate inputs (must 200, regression guard) ───────────────────

_ALLOWED_KEYS = [
    ("ascii_dotted",       "a.b.c"),
    ("latin1_accent",      "caf\xe9.config"),
    ("cjk",                "测试.key"),
    ("emoji",              "emoji.\U0001f680.key"),
]


@pytest.mark.parametrize("label,good_key", _ALLOWED_KEYS, ids=[t[0] for t in _ALLOWED_KEYS])
async def test_create_memory_accepts_legitimate_unicode_key(
    tmp_path, label: str, good_key: str,
) -> None:
    """POST /api/memories with normal Unicode (accents, CJK, emoji)
    must continue to succeed — the validator only rejects
    control/bidi/invisible chars, not "I have a non-ASCII codepoint"
    in general."""
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
        # Sanity: the row landed.
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
    surface still satisfy the same shape."""
    from agent_mcp.utils.string_utils import (
        has_unsafe_unicode_for_identifier,
    )

    # All disallowed cases reject.
    for _label, bad_key in _DISALLOWED_KEYS:
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
