"""Memory keys are constrained to ``^[A-Za-z0-9._/-]+$`` (letters, digits,
``. _ / -``). ``/`` stays allowed (the namespacing convention). New writes
with a disallowed character are rejected up front; the sanitizing migration
(0017) converts any legacy non-conforming key's bad chars to ``_``.

RED before enforcement (bad keys create with 200); GREEN after.
"""

from __future__ import annotations

import pytest

from tests.harness import mcp_session
from agent_mcp.utils.string_utils import (
    is_valid_memory_key,
    sanitize_memory_key,
)


# ---- unit: predicate ----
@pytest.mark.parametrize(
    "key",
    ["ios/improvements-doc", "backend-dev/status", "a.b.c",
     "UPPER_lower-123", "a/b/c/deep", "x"],
)
def test_valid_keys(key) -> None:
    assert is_valid_memory_key(key)


@pytest.mark.parametrize(
    "key",
    ["", "ns:key", "has space", "emoji\U0001F680", "café",
     "a@b", "q?x", "a#b", None, 123, "tab\tkey"],
)
def test_invalid_keys(key) -> None:
    assert not is_valid_memory_key(key)


# ---- unit: sanitizer ----
@pytest.mark.parametrize(
    "raw,clean",
    [
        ("ios/improvements-doc", "ios/improvements-doc"),   # unchanged
        ("ns:key with space", "ns_key_with_space"),
        ("a@b#c", "a_b_c"),
        ("café.config", "caf_.config"),
        ("emoji\U0001F680key", "emoji_key"),
    ],
)
def test_sanitize(raw, clean) -> None:
    assert sanitize_memory_key(raw) == clean
    assert is_valid_memory_key(sanitize_memory_key(raw))


# ---- integration: create rejects a disallowed key ----
pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("bad", ["ns:key", "has space", "a@b", "q?x"])
async def test_create_rejects_disallowed_key(tmp_path, bad) -> None:
    async with mcp_session(tmp_path) as admin:
        r = admin.post(
            "/api/memories",
            json={
                "context_key": bad,
                "context_value": "v",
                "description": "d",
            },
        )
        assert r.status_code == 400, (
            f"disallowed key {bad!r} should be 400, got {r.status_code}: {r.text}"
        )


async def test_create_allows_slashed_and_normal_keys(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        for key in ("ns/allowed-key", "plain_key.v2", "backend-dev/status"):
            r = admin.post(
                "/api/memories",
                json={
                    "context_key": key,
                    "context_value": "v",
                    "description": "d",
                },
            )
            assert r.status_code == 200, f"{key!r} should be allowed: {r.text}"
