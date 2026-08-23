"""R20-F2 / R20-F3 (pentest, combined delivery).

R20-F2 (MEDIUM): ``settings.py``'s ``context_key`` validation relies
solely on ``has_unsafe_unicode_for_identifier`` -- a hand-enumerated
Unicode-range DENYLIST -- unlike its sibling ``memories.py``, which
additionally gates every write with ``is_valid_memory_key`` (an ASCII
ALLOWLIST). Unicode categories ``Lo`` (Letter, other) and ``So``
(Symbol, other) are covered by NEITHER the denylist NOR the
category-based sanitizer chokepoint
(``json_utils._HIDDEN_FORMAT_CATEGORIES = {"Cf","Zl","Zp","Cs"}``).
Several codepoints in those categories render as fully blank/invisible
glyphs -- the same spoofing primitive the denylist exists to stop --
and survived into the stored ``context_key`` verbatim before this fix,
via BOTH the URL-path-param ``context_key`` (PUT) and the
body-supplied ``context_key`` (POST).

R20-F3 (LOW): ``json_utils.get_sanitized_json_body`` only special-cased
``ValueError`` and fell through to a generic ``except Exception as e:
raise ValueError(f"Error processing request body: {e}")``, which
echoed the raw Python interpreter message verbatim to the client. A
deeply-nested JSON body triggers CPython's own ``RecursionError``
during JSON parsing, and the message "maximum recursion depth
exceeded" leaked straight into the client-visible error body. This
helper is the ONE chokepoint 8 routers share (settings, memories,
tasks, agents, composition, delivery, messages, schedules); this file
exercises two representative call sites (settings + memories).
"""

from __future__ import annotations

import urllib.parse

import pytest

from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# R20-F2: invisible Lo/So codepoints in a settings context_key
# ---------------------------------------------------------------------------

# Each codepoint renders as a fully blank/invisible glyph but is NOT in
# category Cf/Zl/Zp/Cs (the json_utils sanitizer chokepoint) and is NOT
# in the hand-enumerated _DISALLOWED_KEY_CHAR_RE denylist either.
_INVISIBLE_LO_SO_CODEPOINTS: list[tuple[str, str]] = [
    ("hangul_choseong_filler", "ᅟ"),   # Lo
    ("hangul_jungseong_filler", "ᅠ"),  # Lo
    ("hangul_filler", "ㅤ"),            # Lo
    ("halfwidth_hangul_filler", "ﾠ"),  # Lo
    ("braille_pattern_blank", "⠀"),    # So
]


def _bad_settings_key(char: str) -> str:
    # Must still start with "config_" to reach the key-format tool
    # gate at all -- the vulnerability is in the REST-layer allowlist,
    # not the config_* prefix check.
    return f"config_test_{char}_key"


@pytest.mark.parametrize(
    "label,char", _INVISIBLE_LO_SO_CODEPOINTS,
    ids=[t[0] for t in _INVISIBLE_LO_SO_CODEPOINTS],
)
async def test_post_settings_rejects_invisible_lo_so_key(
    tmp_path, label: str, char: str,
) -> None:
    """POST /api/settings with an invisible Lo/So codepoint in the
    body-supplied context_key must 400, and the key must never land in
    the store."""
    async with mcp_session(tmp_path) as admin:
        bad_key = _bad_settings_key(char)
        r = admin.post(
            "/api/settings",
            json={"context_key": bad_key, "context_value": True},
        )
        assert r.status_code == 400, (
            f"{label}: expected 400 for invisible-glyph key {bad_key!r}, "
            f"got {r.status_code} body={r.text!r}"
        )
        assert char not in r.text

        read = admin.get("/api/settings-data")
        rows = read.json()["settings"]
        assert all(row["context_key"] != bad_key for row in rows), (
            f"{label}: 400 returned but the row landed in project_settings"
        )


@pytest.mark.parametrize(
    "label,char", _INVISIBLE_LO_SO_CODEPOINTS,
    ids=[t[0] for t in _INVISIBLE_LO_SO_CODEPOINTS],
)
async def test_put_settings_rejects_invisible_lo_so_key_in_url(
    tmp_path, label: str, char: str,
) -> None:
    """PUT /api/settings/<key> with an invisible Lo/So codepoint in the
    URL path must 400 too -- the URL path segment never passes through
    the JSON-body sanitizer chokepoint at all, so this is the more
    direct of the two exploit surfaces."""
    async with mcp_session(tmp_path) as admin:
        bad_key = _bad_settings_key(char)
        encoded = urllib.parse.quote(bad_key, safe="")
        r = admin.request(
            "PUT", f"/api/settings/{encoded}", json={"context_value": True},
        )
        assert r.status_code == 400, (
            f"{label}: expected 400 for invisible-glyph key {bad_key!r}, "
            f"got {r.status_code} body={r.text!r}"
        )
        assert char not in r.text

        read = admin.get("/api/settings-data")
        rows = read.json()["settings"]
        assert all(row["context_key"] != bad_key for row in rows), (
            f"{label}: 400 returned but the row landed in project_settings"
        )


async def test_delete_settings_rejects_invisible_lo_so_key_in_url(
    tmp_path,
) -> None:
    """DELETE /api/settings/<key> shares the same URL-path validation
    gate as PUT; pin one representative codepoint here."""
    async with mcp_session(tmp_path) as admin:
        char = _INVISIBLE_LO_SO_CODEPOINTS[0][1]
        bad_key = _bad_settings_key(char)
        encoded = urllib.parse.quote(bad_key, safe="")
        r = admin.request("DELETE", f"/api/settings/{encoded}", json={})
        assert r.status_code == 400, (
            f"expected 400 for invisible-glyph key {bad_key!r}, "
            f"got {r.status_code} body={r.text!r}"
        )


async def test_settings_allowlist_rejection_uses_setting_key_error(
    tmp_path,
) -> None:
    """The rejection envelope names itself as a setting-key error (not
    the memory-flavoured message memories.py uses) -- confirms the
    settings router got its own sibling constant, not a copy-paste of
    the memories one."""
    async with mcp_session(tmp_path) as admin:
        bad_key = _bad_settings_key(_INVISIBLE_LO_SO_CODEPOINTS[0][1])
        r = admin.post(
            "/api/settings",
            json={"context_key": bad_key, "context_value": True},
        )
        assert r.status_code == 400
        body = r.json()
        assert body.get("error") == "invalid_key_character", body
        assert "Setting key" in body.get("message", ""), body


# --- Regression: normal ASCII config_* keys are unaffected -----------------


async def test_post_settings_normal_ascii_key_still_accepted(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        r = admin.post(
            "/api/settings",
            json={
                "context_key": "config_message_retention_days",
                "context_value": 14,
            },
        )
        assert r.status_code == 200, r.text


async def test_put_settings_normal_ascii_key_round_trips(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        r = admin.request(
            "PUT",
            "/api/settings/config_allow_worker_to_worker",
            json={"context_value": True},
        )
        assert r.status_code == 200, r.text

        read = admin.get("/api/settings-data")
        rows = read.json()["settings"]
        assert any(
            row["context_key"] == "config_allow_worker_to_worker"
            for row in rows
        )


async def test_delete_settings_normal_ascii_key_still_accepted(
    tmp_path,
) -> None:
    async with mcp_session(tmp_path) as admin:
        r = admin.request(
            "PUT",
            "/api/settings/config_allow_worker_to_worker",
            json={"context_value": True},
        )
        assert r.status_code == 200, r.text

        r = admin.request(
            "DELETE", "/api/settings/config_allow_worker_to_worker", json={},
        )
        assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# R20-F3: deeply-nested JSON body no longer leaks the raw RecursionError
# ---------------------------------------------------------------------------

# ~6000-deep nested JSON array: structurally valid JSON, but parsing it
# blows the interpreter recursion limit -> RecursionError inside
# json.loads() (mirrors tests/router/test_sec_r20_json_recursion_depth.py's
# depth choice of comfortably clearing the default limit).
_DEEP_DEPTH = 6000
_DEEP_JSON_BODY = "[" * _DEEP_DEPTH + "]" * _DEEP_DEPTH


async def test_post_settings_deep_json_body_no_recursion_leak(
    tmp_path,
) -> None:
    """POST /api/settings with a ~6000-deep nested JSON body must 400
    with a clean static message -- not the raw interpreter
    "maximum recursion depth exceeded" text."""
    async with mcp_session(tmp_path) as admin:
        headers = dict(admin.forwarding_header())
        headers["Content-Type"] = "application/json"
        r = admin.client.post(
            "/api/settings", content=_DEEP_JSON_BODY, headers=headers,
        )
        assert r.status_code == 400, (
            f"deep-nested body must be a clean 400, got "
            f"{r.status_code}: {r.text}"
        )
        assert "recursion" not in r.text.lower(), (
            f"raw RecursionError text leaked into the response: {r.text}"
        )


async def test_post_memories_deep_json_body_no_recursion_leak(
    tmp_path,
) -> None:
    """Same guard on a second get_sanitized_json_body call site
    (/api/memories) -- the fix lives in the ONE shared helper, so both
    routers must be covered by the same behavior."""
    async with mcp_session(tmp_path) as admin:
        headers = dict(admin.forwarding_header())
        headers["Content-Type"] = "application/json"
        r = admin.client.post(
            "/api/memories", content=_DEEP_JSON_BODY, headers=headers,
        )
        assert r.status_code == 400, (
            f"deep-nested body must be a clean 400, got "
            f"{r.status_code}: {r.text}"
        )
        assert "recursion" not in r.text.lower(), (
            f"raw RecursionError text leaked into the response: {r.text}"
        )


# --- Regression: normal, reasonably-nested bodies still parse fine ---------


async def test_post_settings_normal_body_still_parses(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        r = admin.post(
            "/api/settings",
            json={
                "context_key": "config_message_retention_days",
                "context_value": {"nested": {"a": [1, 2, {"b": 3}]}},
            },
        )
        assert r.status_code == 200, r.text


async def test_post_memories_normal_body_still_parses(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        r = admin.post(
            "/api/memories",
            json={
                "context_key": "team_motto",
                "context_value": {"nested": {"a": [1, 2, {"b": 3}]}},
            },
        )
        assert r.status_code == 200, r.text
