"""PF-R14-1: numeric-coercion type-confusion on ``/api/messages/query``.

``list_messages_api_route`` coerces ``limit`` / ``offset`` with
``int(data.get(...))``. A non-numeric STRING (e.g. ``"abc"``) raises
``ValueError`` and is caught → clean 400. But a list/dict value (e.g.
``{"limit": [1, 2]}``) raises ``TypeError``, which the ``except
ValueError`` did NOT catch — it fell through to the generic
``except Exception`` → HTTP 500 (type-confusion → 500 family).

RED on origin/main (500 for the list/dict cases); GREEN after the
handler broadens the guard to ``except (TypeError, ValueError)`` so a
non-numeric-typed limit/offset returns a clean 400 like the string
case. Also asserts a negative offset is clamped (defense-in-depth).
"""

from __future__ import annotations

import pytest

from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio


async def test_query_list_limit_is_400_not_500(tmp_path) -> None:
    """A list-typed ``limit`` must 400 (TypeError coercion), not 500."""
    async with mcp_session(tmp_path) as admin:
        resp = admin.post(
            "/api/messages/query",
            json={"limit": [1, 2]},
        )
        assert resp.status_code == 400, (
            f"list-typed limit must be a clean 400, got "
            f"{resp.status_code}: {resp.text}"
        )


async def test_query_list_offset_is_400_not_500(tmp_path) -> None:
    """A list-typed ``offset`` must 400 (TypeError coercion), not 500."""
    async with mcp_session(tmp_path) as admin:
        resp = admin.post(
            "/api/messages/query",
            json={"offset": [1]},
        )
        assert resp.status_code == 400, (
            f"list-typed offset must be a clean 400, got "
            f"{resp.status_code}: {resp.text}"
        )


async def test_query_dict_limit_is_400_not_500(tmp_path) -> None:
    """A dict-typed ``limit`` must 400 (TypeError coercion), not 500."""
    async with mcp_session(tmp_path) as admin:
        resp = admin.post(
            "/api/messages/query",
            json={"limit": {"x": 1}},
        )
        assert resp.status_code == 400, (
            f"dict-typed limit must be a clean 400, got "
            f"{resp.status_code}: {resp.text}"
        )


async def test_query_string_limit_still_400(tmp_path) -> None:
    """Regression: a non-numeric STRING limit stays a clean 400."""
    async with mcp_session(tmp_path) as admin:
        resp = admin.post(
            "/api/messages/query",
            json={"limit": "abc"},
        )
        assert resp.status_code == 400, (
            f"non-numeric string limit must be 400, got "
            f"{resp.status_code}: {resp.text}"
        )


async def test_query_valid_int_limit_offset_succeeds(tmp_path) -> None:
    """Regression: valid integer limit/offset still returns 2xx."""
    async with mcp_session(tmp_path) as admin:
        resp = admin.post(
            "/api/messages/query",
            json={"limit": 10, "offset": 0},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["limit"] == 10
        assert body["offset"] == 0


async def test_query_negative_offset_is_clamped(tmp_path) -> None:
    """A negative offset is clamped to the 0 floor (defense-in-depth)."""
    async with mcp_session(tmp_path) as admin:
        resp = admin.post(
            "/api/messages/query",
            json={"offset": -5},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["offset"] == 0, (
            f"negative offset must clamp to 0, got {resp.json()['offset']}"
        )
