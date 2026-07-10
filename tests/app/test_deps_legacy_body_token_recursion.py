"""PF-R20-2: ``_legacy_body_token`` must not let a deep-nested JSON body
escape as a ``RecursionError`` → HTTP 500.

``_legacy_body_token`` (``agent_mcp/app/deps.py``) is the body-token
fallback source consulted by ``require_operator_session`` — the FastAPI
auth dep on the backend ``app/routers/*`` endpoints. It parses the raw
request body with ``json.loads`` inside a narrow
``except (ValueError, json.JSONDecodeError)`` guard.

A ~10k-deep nested JSON body makes ``json.loads`` raise
``RecursionError`` — a ``RuntimeError`` subclass, NOT a ``ValueError``
subclass — which the narrow guard does NOT catch, so it propagates out
of the auth dep and surfaces as HTTP 500 *before* any handler runs.

The helper's contract is "return the body's ``token`` field or None on
any parse failure". A deep-nested body that is not a valid token body
should just yield "no legacy body token" (None) and fall through to the
other auth methods — never a 500. These tests pin that contract.

This is the backend-Starlette-tier sibling of PF-R20-1 (the two aiohttp
router-tier parsers, PR #367).
"""

from __future__ import annotations

import json

import pytest
from starlette.requests import Request

from agent_mcp.app.deps import _legacy_body_token


pytestmark = pytest.mark.asyncio


def _request_with_body(body: bytes) -> Request:
    """A minimal Starlette ``Request`` whose ASGI receive channel
    yields ``body`` — so ``await request.body()`` inside
    ``_legacy_body_token`` sees exactly these bytes. No cookie, no
    Authorization header: the body-token fallback is the path.
    """
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "path": "/api/agents",
        "raw_path": b"/api/agents",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
        "client": ("127.0.0.1", 12345),
        "server": ("localhost", 80),
        "scheme": "http",
    }

    async def _receive() -> dict:
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, _receive)


def _deep_nested_body(depth: int = 10_000) -> bytes:
    """A syntactically-valid JSON array nested ``depth`` levels deep —
    parseable in principle, but ``json.loads`` blows the C recursion
    limit long before it finishes, raising ``RecursionError``.
    """
    return b"[" * depth + b"]" * depth


async def test_deep_nested_body_returns_none_not_recursionerror():
    """RED on origin/main: the deep-nested body raises ``RecursionError``
    out of ``_legacy_body_token`` (narrow except misses it).
    GREEN after: the helper swallows it and returns None.
    """
    request = _request_with_body(_deep_nested_body())

    # Must NOT raise (RecursionError today); must return None.
    result = await _legacy_body_token(request)

    assert result is None


async def test_deep_nested_object_body_returns_none():
    """Same class via nested objects rather than arrays."""
    depth = 10_000
    body = b'{"a":' * depth + b"1" + b"}" * depth
    request = _request_with_body(body)

    assert await _legacy_body_token(request) is None


# ── Regressions: the graceful-return behaviour is unchanged ─────────


async def test_valid_token_body_still_extracts_token():
    request = _request_with_body(json.dumps({"token": "tok-abc123"}).encode())

    assert await _legacy_body_token(request) == "tok-abc123"


async def test_malformed_body_still_returns_none():
    request = _request_with_body(b"{not valid json")

    assert await _legacy_body_token(request) is None


async def test_non_dict_body_still_returns_none():
    request = _request_with_body(json.dumps([1, 2, 3]).encode())

    assert await _legacy_body_token(request) is None


async def test_dict_without_token_returns_none():
    request = _request_with_body(json.dumps({"other": "field"}).encode())

    assert await _legacy_body_token(request) is None


async def test_non_string_token_returns_none():
    request = _request_with_body(json.dumps({"token": 12345}).encode())

    assert await _legacy_body_token(request) is None


async def test_empty_body_returns_none():
    request = _request_with_body(b"")

    assert await _legacy_body_token(request) is None
