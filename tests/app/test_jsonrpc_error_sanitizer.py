"""SEC-1: the JSON-RPC error-body sanitizer is a FIXED-ENVELOPE rebuild.

The MCP SDK serialises uncaught server-side detail straight into the
JSON-RPC error ``message`` it sends the client — the catch-all emits
``{"code":-32603,"message":"Error handling POST request: <str(err)>"}``
for any uncaught exception, and a schema-validation failure emits the
full pydantic ``ValidationError`` dump. The earlier sanitizer only
rewrote a message when it matched one of four leak-marker substrings, so
any exception string lacking all four leaked through verbatim.

These tests pin the hardened behaviour: for ANY parseable JSON-RPC
``error`` shape the ``message`` is unconditionally replaced with a terse
string keyed off ``error.code`` — no marker sniff, so no exception
channel can leak.
"""

from __future__ import annotations

import json

from agent_mcp.app.main_app import _sanitize_jsonrpc_error_body


def _err_body(code: int, message: str) -> bytes:
    return json.dumps(
        {"jsonrpc": "2.0", "id": 1, "error": {"code": code, "message": message}}
    ).encode("utf-8")


def test_internal_error_catch_all_is_terse() -> None:
    """The SDK's -32603 catch-all (``Error handling POST request: …``)
    is rebuilt to a detail-free ``Internal error``."""
    raw = _err_body(
        -32603,
        "Error handling POST request: KeyError('secret_internal_field')",
    )
    out = _sanitize_jsonrpc_error_body(raw)
    assert out is not None
    payload = json.loads(out)
    assert payload["error"]["code"] == -32603
    assert payload["error"]["message"] == "Internal error"
    text = out.decode()
    assert "Error handling POST request" not in text
    assert "secret_internal_field" not in text
    # jsonrpc / id envelope preserved.
    assert payload["jsonrpc"] == "2.0"
    assert payload["id"] == 1


def test_recursionerror_message_without_marker_is_rebuilt() -> None:
    """Regression for the marker-denylist gap: an exception string that
    matches none of the old markers (e.g. a live RecursionError from a
    deep-nested-JSON body) must STILL be rebuilt, not passed through."""
    raw = _err_body(
        -32603,
        "Error handling POST request: maximum recursion depth exceeded",
    )
    out = _sanitize_jsonrpc_error_body(raw)
    assert out is not None
    payload = json.loads(out)
    assert payload["error"]["message"] == "Internal error"
    assert "recursion" not in out.decode().lower()


def test_schema_validation_remapped_to_invalid_request() -> None:
    """A -32602 pydantic-dump validation failure is remapped to -32600
    Invalid Request with a fixed message (no pydantic internals)."""
    raw = _err_body(
        -32602,
        "1 validation error for JSONRPCMessage\ninput_value=42, "
        "for further information visit https://errors.pydantic.dev/",
    )
    out = _sanitize_jsonrpc_error_body(raw)
    assert out is not None
    payload = json.loads(out)
    assert payload["error"]["code"] == -32600
    assert payload["error"]["message"] == "Invalid Request"
    text = out.decode().lower()
    assert "pydantic" not in text
    assert "input_value" not in text


def test_parse_error_code_preserved() -> None:
    """A genuine -32700 keeps its code and gets the terse Parse error."""
    out = _sanitize_jsonrpc_error_body(_err_body(-32700, "whatever detail"))
    assert out is not None
    payload = json.loads(out)
    assert payload["error"]["code"] == -32700
    assert payload["error"]["message"] == "Parse error"


def test_unmapped_code_falls_back_to_internal_error() -> None:
    """An unrecognised / vendor error code collapses to -32603 Internal
    error so no unmapped channel can leak its detail."""
    out = _sanitize_jsonrpc_error_body(_err_body(-31999, "leaky vendor detail"))
    assert out is not None
    payload = json.loads(out)
    assert payload["error"]["code"] == -32603
    assert payload["error"]["message"] == "Internal error"
    assert "leaky vendor detail" not in out.decode()


def test_clean_message_still_rebuilt() -> None:
    """Even a benign-looking message is rebuilt (unconditional) — the
    sanitizer no longer trusts the message content at all."""
    out = _sanitize_jsonrpc_error_body(_err_body(-32603, "anything at all"))
    assert out is not None
    assert json.loads(out)["error"]["message"] == "Internal error"


def test_non_jsonrpc_bodies_untouched() -> None:
    """Non-JSON, non-dict, and non-error payloads are left alone
    (return None) so a healthy response is never mangled."""
    assert _sanitize_jsonrpc_error_body(b"not json at all") is None
    assert _sanitize_jsonrpc_error_body(b'"just a string"') is None
    assert _sanitize_jsonrpc_error_body(b'{"result": {"ok": true}}') is None
    assert _sanitize_jsonrpc_error_body(b'{"error": "not-a-dict"}') is None
