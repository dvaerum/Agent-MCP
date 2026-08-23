"""N1 bypass #3: the MCP ``clientInfo`` decode point skipped the
sanitizer.

``_McpAsgiApp.__call__`` sniffs every POST /mcp body for an
``initialize`` request and records ``params.clientInfo.name`` /
``.version`` against the bearer's agent_id
(``core.client_info_registry``). That name is a fully client-controlled
string, and it is RENDERED — the dashboard's agent view shows which
client an agent connected with, and the hold-strategy resolver keys on
it.

Pre-fix, ``_maybe_record_client_info`` did a bare ``json.loads(body)``,
so it was one of the five decode points that never met
``utils.json_utils``'s hidden-Unicode/control-byte strip: an RTL
override, a zero-width space or an ANSI-escape control byte in the
announced client name was stored and displayed verbatim. It now decodes
through ``json_utils.decode_untrusted_body`` like every other request
body — the structural half of that is
``tests/router/test_arch_enforced_sanitization.py``, which fails if this
call site ever goes back to a raw decode.
"""

from __future__ import annotations

import json

import pytest


class _FakeManager:
    """Minimal stand-in for the StreamableHTTP session manager (mirrors
    ``tests/test_client_info_recording_disconnect.py``)."""

    async def handle_request(self, scope, receive, send):
        while True:
            msg = await receive()
            if msg.get("type") == "http.disconnect":
                break
            if not msg.get("more_body", False):
                break


async def _post_initialize(monkeypatch, client_info: dict) -> None:
    """Drive one POST /mcp ``initialize`` through the real ASGI wrapper
    and leave the result in ``client_info_registry``."""
    from agent_mcp.app import main_app

    monkeypatch.setattr(
        main_app, "get_agent_id", lambda t: "n1-agent" if t else None,
    )
    app = main_app._McpAsgiApp(_FakeManager())
    body = json.dumps({
        "jsonrpc": "2.0", "id": 0, "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": client_info,
        },
    }).encode()

    messages = [{"type": "http.request", "body": body, "more_body": False}]
    it = iter(messages)

    async def receive():
        try:
            return next(it)
        except StopIteration:
            return {"type": "http.disconnect"}

    async def send(_message):
        return None

    await app(
        {
            "type": "http", "method": "POST", "path": "/",
            "headers": [(b"authorization", b"Bearer tok-n1")],
        },
        receive,
        send,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "announced, expected",
    [
        # R13-F2/R14-F3: zero-width space and RTL override — the
        # classic display-spoofing primitives. "Claude\u202eedoC-edualC"
        # renders right-to-left from the override onward.
        ("claude\u200b-code", "claude-code"),
        ("Claude\u202eedoc", "Claudeedoc"),
        # R4-F3/R5-F8: C0 ESC (ANSI escape) and C1 CSI.
        ("claude\x1b[31m-code", "claude[31m-code"),
        ("claude\x9b31m-code", "claude31m-code"),
        # R15-F1: lone unpaired surrogate — crashes SQLite's TEXT bind.
        ("claude\ud800-code", "claude-code"),
        # R5-F9: variation selector.
        ("claude\ufe0f-code", "claude-code"),
    ],
    ids=["zwsp", "rtlo", "c0-esc", "c1-csi", "lone-surrogate", "vs16"],
)
async def test_client_info_name_is_sanitized(
    monkeypatch, announced: str, expected: str,
) -> None:
    from agent_mcp.core import client_info_registry as reg

    reg.clear()
    await _post_initialize(monkeypatch, {"name": announced, "version": "1.0"})
    assert reg.get_client_name("n1-agent") == expected, (
        f"clientInfo.name was recorded as "
        f"{reg.get_client_name('n1-agent')!r}; the MCP initialize body "
        f"must decode through json_utils.decode_untrusted_body so a "
        f"client-announced name cannot carry hidden-format Unicode into "
        f"the dashboard (N1)."
    )


@pytest.mark.asyncio
async def test_ordinary_client_name_is_untouched(monkeypatch) -> None:
    """No-policy-change guard: the sanitizer strips hidden-format
    classes only, so a normal client name — including a non-Latin one —
    is recorded byte-identical."""
    from agent_mcp.core import client_info_registry as reg

    for name in ("claude-code", "Cursor 0.42", "クライアント", "café-cli"):
        reg.clear()
        await _post_initialize(monkeypatch, {"name": name, "version": "1.0"})
        assert reg.get_client_name("n1-agent") == name


@pytest.mark.asyncio
async def test_oversized_body_is_skipped_not_sanitized(monkeypatch) -> None:
    """The sniff's cost guard: routing this hot path through the seam
    costs a per-character Unicode walk, and the router allows /mcp
    bodies up to 1 MiB. A body past
    ``_MAX_CLIENT_INFO_SNIFF_BYTES`` is not sniffed at all — best-effort
    by design, and the fallback (feature detection in the hold-strategy
    resolver) is the same one a missing clientInfo already triggers."""
    from agent_mcp.app import main_app
    from agent_mcp.core import client_info_registry as reg

    reg.clear()
    padding = "x" * (main_app._MAX_CLIENT_INFO_SNIFF_BYTES + 1)
    await _post_initialize(
        monkeypatch, {"name": "claude-code", "version": padding},
    )
    assert reg.get_client_name("n1-agent") is None


@pytest.mark.asyncio
async def test_non_initialize_body_is_not_recorded(monkeypatch) -> None:
    """Non-regression on the pre-filter: an ordinary tool-call POST
    (which never contains the literal ``initialize``) records nothing
    and, importantly, is not put through the sanitizing walk."""
    from agent_mcp.app import main_app
    from agent_mcp.core import client_info_registry as reg

    monkeypatch.setattr(
        main_app, "get_agent_id", lambda t: "n1-agent" if t else None,
    )
    reg.clear()
    app = main_app._McpAsgiApp(_FakeManager())
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "view_tasks", "arguments": {}},
    }).encode()
    it = iter([{"type": "http.request", "body": body, "more_body": False}])

    async def receive():
        try:
            return next(it)
        except StopIteration:
            return {"type": "http.disconnect"}

    async def send(_message):
        return None

    await app(
        {
            "type": "http", "method": "POST", "path": "/",
            "headers": [(b"authorization", b"Bearer tok-n1")],
        },
        receive,
        send,
    )
    assert reg.get_client_name("n1-agent") is None
