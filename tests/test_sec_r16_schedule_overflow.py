"""R16-F3 / R16-F4 — numeric-overflow hardening for the scheduler.

Sibling of the already-fixed PF-R18-1 class (``int(<client value>)``
catching only ``(TypeError, ValueError)`` but not ``OverflowError``, plus
a missing upper bound). Two live-repro'd HIGHs:

* **R16-F3** — ``POST/PUT /api/<proj>/schedules`` with an overflowing
  ``interval_seconds`` (``1e400`` → ``float('inf')`` → ``int()`` raises
  ``OverflowError``; or a huge finite int that then overflows
  ``now + timedelta(seconds=interval)``) leaked a raw text/plain HTTP 500.
* **R16-F4** — the same class on ``count`` (huge finite int → SQLite
  INTEGER overflow → generic 500 ``Operation failed``).

The fix coerces + bounds both fields in ``_validate_interval`` /
``_validate_count`` (returning a clean field-level ``Invalid`` → 4xx over
REST, a clean field error over MCP) AND wraps the direct-impl REST
handlers in the standard ``except Exception → JSON 500`` envelope so no
impl bug can ever leak a raw 500 again.
"""

from __future__ import annotations

import pytest

import agent_mcp.tools.scheduled_directive_tools as sdt
from agent_mcp.core.tool_result import Invalid, Ok
from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio


_JSON_HDR = {"content-type": "application/json"}

# JSON tokens that reproduce the class, keyed by the field they target.
# ``1e400`` parses to ``float('inf')`` (OverflowError in ``int()``);
# the 23-digit int overflows SQLite's 64-bit INTEGER; the finite
# ``86400000000000000`` passes ``int()`` + the floor but overflows
# ``timedelta(seconds=...)``.
_OVERFLOW_TOKENS = ("1e400", "99999999999999999999999", "86400000000000000")


def _post_raw(admin, body: str):
    return admin.request(
        "POST", "/api/schedules", content=body, headers=_JSON_HDR
    )


def _put_raw(admin, directive_id: str, body: str):
    return admin.request(
        "PUT", f"/api/schedules/{directive_id}", content=body, headers=_JSON_HDR
    )


# ── REST: interval_seconds overflow (R16-F3) ────────────────────────────


@pytest.mark.parametrize("token", _OVERFLOW_TOKENS)
async def test_rest_post_interval_overflow_is_clean_4xx(tmp_path, token):
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        body = (
            '{"agent_id":"alice","prompt":"x","interval_seconds":' + token + "}"
        )
        r = _post_raw(admin, body)
        assert r.status_code == 400, (token, r.status_code, r.text)
        # A clean JSON error body, never the raw text/plain 500.
        assert "error" in r.json(), r.text


@pytest.mark.parametrize("token", _OVERFLOW_TOKENS)
async def test_rest_put_interval_overflow_is_clean_4xx(tmp_path, token):
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        did = _post_raw(
            admin,
            '{"agent_id":"alice","prompt":"x","interval_seconds":120}',
        ).json()["directive"]["directive_id"]
        r = _put_raw(admin, did, '{"interval_seconds":' + token + "}")
        assert r.status_code == 400, (token, r.status_code, r.text)
        assert "error" in r.json(), r.text


# ── REST: count overflow (R16-F4) ───────────────────────────────────────


@pytest.mark.parametrize("token", _OVERFLOW_TOKENS)
async def test_rest_post_count_overflow_is_clean_4xx(tmp_path, token):
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        body = (
            '{"agent_id":"alice","prompt":"x","interval_seconds":120,'
            '"count":' + token + "}"
        )
        r = _post_raw(admin, body)
        assert r.status_code == 400, (token, r.status_code, r.text)
        assert "error" in r.json(), r.text


@pytest.mark.parametrize("token", _OVERFLOW_TOKENS)
async def test_rest_put_count_overflow_is_clean_4xx(tmp_path, token):
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        did = _post_raw(
            admin,
            '{"agent_id":"alice","prompt":"x","interval_seconds":120}',
        ).json()["directive"]["directive_id"]
        r = _put_raw(admin, did, '{"count":' + token + "}")
        assert r.status_code == 400, (token, r.status_code, r.text)
        assert "error" in r.json(), r.text


# ── REST: controls still hold ───────────────────────────────────────────


async def test_rest_post_valid_interval_still_200(tmp_path):
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        r = _post_raw(
            admin,
            '{"agent_id":"alice","prompt":"x","interval_seconds":60}',
        )
        assert r.status_code == 200, r.text
        assert r.json()["directive"]["interval_seconds"] == 60


async def test_rest_post_non_numeric_interval_still_400(tmp_path):
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("alice")
        r = _post_raw(
            admin,
            '{"agent_id":"alice","prompt":"x","interval_seconds":"abc"}',
        )
        assert r.status_code == 400, r.text


# ── MCP impl fidelity: clean field error, never an unhandled raise ───────


async def test_mcp_create_interval_inf_returns_invalid(tmp_path):
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        res = await sdt.create_scheduled_directive_tool_impl(
            {"prompt": "x", "interval_seconds": float("inf")},
            principal=alice._principal(),
        )
        assert isinstance(res, Invalid), res
        assert res.field == "interval_seconds", res


async def test_mcp_create_interval_finite_overflow_returns_invalid(tmp_path):
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        res = await sdt.create_scheduled_directive_tool_impl(
            {"prompt": "x", "interval_seconds": 86400000000000000},
            principal=alice._principal(),
        )
        assert isinstance(res, Invalid), res
        assert res.field == "interval_seconds", res


async def test_mcp_create_count_inf_returns_invalid(tmp_path):
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        res = await sdt.create_scheduled_directive_tool_impl(
            {"prompt": "x", "interval_seconds": 120, "count": float("inf")},
            principal=alice._principal(),
        )
        assert isinstance(res, Invalid), res
        assert res.field == "count", res


async def test_mcp_create_count_bigint_returns_invalid(tmp_path):
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        res = await sdt.create_scheduled_directive_tool_impl(
            {
                "prompt": "x",
                "interval_seconds": 120,
                "count": 99999999999999999999999,
            },
            principal=alice._principal(),
        )
        assert isinstance(res, Invalid), res
        assert res.field == "count", res


async def test_mcp_create_valid_count_still_ok(tmp_path):
    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        res = await sdt.create_scheduled_directive_tool_impl(
            {"prompt": "x", "interval_seconds": 120, "count": 5},
            principal=alice._principal(),
        )
        assert isinstance(res, Ok), res
        assert res.data["directive"]["max_runs"] == 5
