"""SEC-R8-1: returned ``Failed`` ToolResult must not reflect raw
exceptions to the client.

Round-7 SD-R7-1 genericized the RAISED-exception paths. But ~40 tool
impls CATCH their own exception and RETURN ``Failed(message=f"…{e}")``
(embedding ``sqlite3.Error`` / ``SQLAlchemyError`` schema+SQL detail),
which the raised-exception net never sees. Both RENDER choke-points
reflected ``result.message`` verbatim:

* MCP wire: ``render_as_text_content`` → ``f"Error: {result.message}"``.
* REST: ``_dispatch_through_tool`` → ``{"message": result.message}``.

The fix genericizes ONLY the ``Failed`` variant at both render sites
(static client string, real detail logged server-side). Every other
variant carries deliberate, controlled, user-facing text and must
render unchanged.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from agent_mcp.app.rest_principal import RestPrincipal
from agent_mcp.core.tool_result import (
    Conflict,
    Failed,
    Invalid,
    NotFound,
    Ok,
    PermissionDenied,
    is_error_result,
    render_as_text_content,
)

# A ``Failed`` message shaped exactly like the ones the ~40 tool impls
# build from a caught sqlite3/SQLAlchemy error — schema disclosure.
_LEAKY = "Database error: no such column: users.secret_col"
_GENERIC = "Operation failed"


# ── MCP wire render site (render_as_text_content) ────────────────────


def test_mcp_failed_render_is_generic_not_raw():
    """The MCP text block must be the static generic string — never the
    raw exception message the tool stuffed into ``Failed``."""
    with patch("agent_mcp.core.tool_result.logger") as mock_logger:
        blocks = render_as_text_content(Failed(message=_LEAKY))

    assert len(blocks) == 1
    text = blocks[0].text
    assert "secret_col" not in text
    assert "no such column" not in text
    assert _LEAKY not in text
    assert _GENERIC in text
    # The real detail is retained server-side.
    assert mock_logger.error.called or mock_logger.warning.called
    logged = " ".join(
        str(a)
        for call in mock_logger.error.call_args_list
        + mock_logger.warning.call_args_list
        for a in call.args
    )
    assert _LEAKY in logged


def test_mcp_failed_still_flagged_is_error():
    """isError fidelity (rounds 3/4/7): a genericized Failed is still an
    error variant so the MCP handler sets ``isError=True``."""
    assert is_error_result(Failed(message=_LEAKY)) is True


# ── REST render site (_dispatch_through_tool) ────────────────────────


@pytest.mark.asyncio
async def test_rest_failed_render_is_generic_not_raw():
    """The REST JSON body's ``message`` for a ``Failed`` result must be
    the static generic string, HTTP 500, detail logged server-side."""
    import json as _json

    from agent_mcp.app import _dispatch_helpers as dh

    async def _fake_dispatch(*_a, **_k):
        return Failed(message=_LEAKY)

    with patch.object(dh, "dispatch_tool_call", _fake_dispatch), patch.object(
        dh, "logger"
    ) as mock_logger:
        resp = await dh._dispatch_through_tool(
            "some_tool",
            {},
            bearer_token=None,
            auth=RestPrincipal(kind="session", user={"username": "admin"}),
        )

    assert resp.status_code == 500
    body = _json.loads(bytes(resp.body))
    assert body["success"] is False
    assert body["error"] == "failed"
    assert body["message"] == _GENERIC
    assert "secret_col" not in _json.dumps(body)
    assert _LEAKY not in _json.dumps(body)
    # Detail retained server-side.
    assert mock_logger.error.called or mock_logger.warning.called
    logged = " ".join(
        str(a)
        for call in mock_logger.error.call_args_list
        + mock_logger.warning.call_args_list
        for a in call.args
    )
    assert _LEAKY in logged


# ── Regression: every OTHER variant renders its intended message ─────


def test_mcp_other_variants_unchanged():
    assert render_as_text_content(Ok(message="created"))[0].text == "created"
    assert (
        render_as_text_content(NotFound(resource="task", identifier="t-9"))[0].text
        == "Error: task 't-9' not found."
    )
    assert (
        render_as_text_content(PermissionDenied(reason="not the author"))[0].text
        == "Unauthorized: not the author"
    )
    assert (
        render_as_text_content(Conflict(reason="duplicate agent_id"))[0].text
        == "Error: conflict: duplicate agent_id"
    )
    # Invalid is deliberate validation feedback — rendered verbatim.
    assert (
        render_as_text_content(Invalid(message="must be positive", field="count"))[
            0
        ].text
        == "Error: invalid count: must be positive"
    )
    assert (
        render_as_text_content(Invalid(message="bad shape"))[0].text
        == "Error: invalid input: bad shape"
    )


@pytest.mark.asyncio
async def test_rest_other_variants_unchanged():
    import json as _json

    from agent_mcp.app import _dispatch_helpers as dh

    async def _ret(result):
        async def _fake(*_a, **_k):
            return result

        return _fake

    cases = [
        (NotFound(resource="task", identifier="t-9"), 404, "not_found"),
        (PermissionDenied(reason="not the author"), 403, "permission_denied"),
        (Invalid(message="must be positive", field="count"), 400, "invalid"),
        (Conflict(reason="dup"), 409, "conflict"),
    ]
    for result, status, err in cases:
        with patch.object(dh, "dispatch_tool_call", await _ret(result)):
            resp = await dh._dispatch_through_tool(
                "some_tool",
                {},
                bearer_token=None,
                auth=RestPrincipal(kind="session", user={"username": "admin"}),
            )
        assert resp.status_code == status
        body = _json.loads(bytes(resp.body))
        assert body["error"] == err

    # Invalid still carries the caller's validation message verbatim.
    with patch.object(
        dh,
        "dispatch_tool_call",
        await _ret(Invalid(message="must be positive", field="count")),
    ):
        resp = await dh._dispatch_through_tool(
            "some_tool", {}, bearer_token=None, auth=RestPrincipal(kind="session", user={"username": "admin"}),
        )
    body = _json.loads(bytes(resp.body))
    assert body["message"] == "must be positive"
    assert body["field"] == "count"
