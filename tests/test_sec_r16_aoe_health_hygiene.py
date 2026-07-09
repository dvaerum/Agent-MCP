"""SD-R16-1: GET /api/aoe/health must not leak internal detail to a
viewer-tier operator.

``require_operator_session`` admits viewer-tier operators on GET (the
per-project backend cannot resolve the caller's project role), so the
AoE-reachability probe is viewer-reachable. Two disclosure vectors:

  1. the handler's outer ``except`` reflected ``str(e)`` into the client
     body (``f"probe crashed: {e}"``);
  2. ``check_health()``'s ``message`` strings embed the internal AoE
     ``base_url`` (topology disclosure) and raw httpx/exception text, and
     the "ok" path returned ``base_url`` outright.

The handler now sanitizes ``check_health``'s result at the response
boundary: the coarse ``status`` (+ ``session_count``) survive; the raw
``message``, ``base_url``, and any exception text do not. Detail stays in
the server log.

The test drives ``admin.get`` — the signed-forwarding path, which is the
viewer-reachable path (tier is unverifiable there), matching the threat
model.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from tests.harness import mcp_session


pytestmark = pytest.mark.asyncio


# Recognisable markers: if either survives into the client body, the
# boundary sanitiser failed.
_SECRET_URL = "http://SECRET-internal:9999"
_SECRET_EXC = "boom-SECRET-token-abc123"


async def test_probe_crash_does_not_reflect_exception_text(tmp_path) -> None:
    """When ``check_health`` raises, the viewer-reachable body must carry
    a STATIC message — never ``str(e)`` (vector 1)."""
    async with mcp_session(tmp_path) as admin:
        with patch(
            "agent_mcp.features.aoe_notify.check_health",
            side_effect=RuntimeError(_SECRET_EXC),
        ):
            r = admin.get("/api/aoe/health")
        assert r.status_code == 200, r.text
        assert _SECRET_EXC not in r.text, (
            f"probe-crash body reflects str(e): {r.text!r}"
        )
        assert r.json().get("status") == "unreachable"


async def test_unreachable_result_does_not_leak_base_url_or_httpx_text(
    tmp_path,
) -> None:
    """An ``unreachable`` result whose ``message`` embeds the internal
    ``base_url`` + httpx error text must be sanitised at the boundary —
    neither reaches the client (vector 2)."""
    leaky = {
        "status": "unreachable",
        "message": f"AoE at {_SECRET_URL} unreachable: {_SECRET_EXC}",
    }
    async with mcp_session(tmp_path) as admin:
        with patch(
            "agent_mcp.features.aoe_notify.check_health",
            return_value=leaky,
        ):
            r = admin.get("/api/aoe/health")
        assert r.status_code == 200, r.text
        assert _SECRET_URL not in r.text, f"base_url leaked: {r.text!r}"
        assert _SECRET_EXC not in r.text, f"httpx exc text leaked: {r.text!r}"
        assert r.json().get("status") == "unreachable"


async def test_timeout_result_does_not_leak_base_url(tmp_path) -> None:
    """The timeout path (``f"AoE timed out at {cfg.base_url}"``) is the
    other ``base_url``-bearing message — also sanitised."""
    leaky = {
        "status": "unreachable",
        "message": f"AoE timed out at {_SECRET_URL}",
    }
    async with mcp_session(tmp_path) as admin:
        with patch(
            "agent_mcp.features.aoe_notify.check_health",
            return_value=leaky,
        ):
            r = admin.get("/api/aoe/health")
        assert r.status_code == 200, r.text
        assert _SECRET_URL not in r.text, f"base_url leaked: {r.text!r}"


async def test_ok_result_does_not_leak_base_url_but_keeps_coarse_status(
    tmp_path,
) -> None:
    """Regression: the healthy path returned ``base_url`` outright
    (topology). It must be stripped, while the coarse ``status`` +
    ``session_count`` an operator needs survive."""
    ok = {"status": "ok", "session_count": 3, "base_url": _SECRET_URL}
    async with mcp_session(tmp_path) as admin:
        with patch(
            "agent_mcp.features.aoe_notify.check_health",
            return_value=ok,
        ):
            r = admin.get("/api/aoe/health")
        assert r.status_code == 200, r.text
        body = r.json()
        assert _SECRET_URL not in r.text, (
            f"base_url leaked on ok path: {r.text!r}"
        )
        assert body.get("status") == "ok"
        assert body.get("session_count") == 3
