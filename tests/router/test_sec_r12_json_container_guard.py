"""Round-12 security (PF-R12-1): container-level JSON type-confusion.

A top-level *non-dict* JSON body — a bare list ``[1,2,3]``, a JSON
string ``"str"``, or a bare scalar ``42`` — parses cleanly through
``get_sanitized_json_body`` then hits ``.get()`` / ``data[key]`` in the
FastAPI per-project routers and raises ``AttributeError`` /
``TypeError``, surfacing as an uncaught **500** instead of a clean
**400**.

The aiohttp ``router/`` tier is already immune (its ``_parse_json_body``
enforces ``isinstance(parsed, dict)``); the FastAPI ``app/routers/*``
tier is the last unguarded cluster. The root fix makes the shared
``get_sanitized_json_body`` raise ``ValueError`` on a non-object top
level so every caller's existing ``except ValueError -> 400`` closes the
gap.

These tests probe one representative body-reading endpoint per router
(agents / composition / memories / tasks / messages), including the
three "bare" sites whose ``.get()`` sits OUTSIDE the ``ValueError``
try (agents ``/api/agents/register``, composition ``/api/terminate-agent``,
messages ``/api/messages/suggest-subject``). RED on origin/main (500s); GREEN after the
helper guard lands. The regression tests confirm a valid dict body
still works.
"""

from __future__ import annotations

import pytest

from tests.harness import mcp_session


pytestmark = pytest.mark.asyncio


# Non-dict top-level JSON bodies that must never reach a ``.get()``.
_NON_DICT_BODIES = ([1, 2, 3], "just a string", 42, True)


def _post_raw(admin, url: str, body):
    """POST a raw (possibly non-dict) JSON body with the operator
    forwarding header attached (routes are ``require_operator_session``).
    """
    return admin.post(url, json=body)


# ------------------------------------------------------------------- #
# agents.py  — POST /api/agents/register (bare .get() site, ~L246/253) #
# ------------------------------------------------------------------- #


@pytest.mark.parametrize("body", _NON_DICT_BODIES)
async def test_register_agent_non_dict_body_is_400(tmp_path, body) -> None:
    async with mcp_session(tmp_path) as admin:
        r = _post_raw(admin, "/api/agents/register", body)
        assert r.status_code == 400, (
            f"non-dict body {body!r} should be 400 not {r.status_code}: "
            f"{r.text}"
        )


# --------------------------------------------------------------------- #
# composition.py — POST /api/terminate-agent (bare .get() site, ~L682)   #
# --------------------------------------------------------------------- #


@pytest.mark.parametrize("body", _NON_DICT_BODIES)
async def test_terminate_agent_non_dict_body_is_400(tmp_path, body) -> None:
    async with mcp_session(tmp_path) as admin:
        r = _post_raw(admin, "/api/terminate-agent", body)
        assert r.status_code == 400, (
            f"non-dict body {body!r} should be 400 not {r.status_code}: "
            f"{r.text}"
        )


# --------------------------------------------------------------------- #
# composition.py — POST /api/update-task-dashboard                       #
# --------------------------------------------------------------------- #


@pytest.mark.parametrize("body", _NON_DICT_BODIES)
async def test_update_task_dashboard_non_dict_body_is_400(tmp_path, body) -> None:
    async with mcp_session(tmp_path) as admin:
        r = _post_raw(admin, "/api/update-task-dashboard", body)
        assert r.status_code == 400, (
            f"non-dict body {body!r} should be 400 not {r.status_code}: "
            f"{r.text}"
        )


# --------------------------------------------------------------------- #
# memories.py — POST /api/memories                                       #
# --------------------------------------------------------------------- #


@pytest.mark.parametrize("body", _NON_DICT_BODIES)
async def test_create_memory_non_dict_body_is_400(tmp_path, body) -> None:
    async with mcp_session(tmp_path) as admin:
        r = _post_raw(admin, "/api/memories", body)
        assert r.status_code == 400, (
            f"non-dict body {body!r} should be 400 not {r.status_code}: "
            f"{r.text}"
        )


# --------------------------------------------------------------------- #
# tasks.py — POST /api/tasks                                             #
# --------------------------------------------------------------------- #


@pytest.mark.parametrize("body", _NON_DICT_BODIES)
async def test_create_task_non_dict_body_is_400(tmp_path, body) -> None:
    async with mcp_session(tmp_path) as admin:
        r = _post_raw(admin, "/api/tasks", body)
        assert r.status_code == 400, (
            f"non-dict body {body!r} should be 400 not {r.status_code}: "
            f"{r.text}"
        )


# ----------------------------------------------------------------------- #
# messages.py — POST /api/messages/suggest-subject (bare site, ~L215/222)  #
# ----------------------------------------------------------------------- #


@pytest.mark.parametrize("body", _NON_DICT_BODIES)
async def test_suggest_subject_non_dict_body_is_400(tmp_path, body) -> None:
    async with mcp_session(tmp_path) as admin:
        r = _post_raw(admin, "/api/messages/suggest-subject", body)
        assert r.status_code == 400, (
            f"non-dict body {body!r} should be 400 not {r.status_code}: "
            f"{r.text}"
        )


# ----------------------------------------------------------------------- #
# messages.py — POST /api/messages/participants (discard-body site)        #
# The body is discarded (``_ = await get_sanitized_json_body``) but the    #
# handler's only guard was ``except Exception -> 500``; a list body must   #
# still 400, not 500.                                                      #
# ----------------------------------------------------------------------- #


@pytest.mark.parametrize("body", _NON_DICT_BODIES)
async def test_list_participants_non_dict_body_is_400(tmp_path, body) -> None:
    async with mcp_session(tmp_path) as admin:
        r = _post_raw(admin, "/api/messages/participants", body)
        assert r.status_code == 400, (
            f"non-dict body {body!r} should be 400 not {r.status_code}: "
            f"{r.text}"
        )


# ============================ regressions ============================ #


async def test_valid_dict_body_still_registers_agent(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        r = admin.post(
            "/api/agents/register",
            json={"name": "reg-probe"},
        )
        assert r.status_code in (200, 201), r.text


async def test_valid_dict_body_still_creates_task(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        r = admin.post(
            "/api/tasks",
            json={"task_title": "valid dict body"},
        )
        assert r.status_code == 200, r.text
        assert r.json().get("success") is True


async def test_valid_dict_body_still_creates_memory(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        r = admin.post(
            "/api/memories",
            json={
                "context_key": "r12.valid",
                "context_value": "ok",
            },
        )
        assert r.status_code == 200, r.text
