"""SSE `/sse` must be registered as a Mount, not a Route.

UPSTREAM_ISSUES.md issue A. Today `sse_connection_handler` is
registered as `Route('/sse', endpoint=...)`. That handler streams
events via the ASGI `send` callable directly (through
`sse_transport.connect_sse`) and returns None implicitly. Starlette's
`request_response` wrapper does:

    response = await f(request)            # → None (after stream ends)
    await response(scope, receive, send)   # TypeError: 'NoneType' not callable

starlette's exception handler catches that and tries to send a 500
back over the already-closed stream — the client sees a
`ServerDisconnectedError` mid-flight, and the journal fills with
`TypeError: 'NoneType' object is not callable`. Under multi-session
load the failure rate climbs to nearly every request.

The fix: register `/sse` as `Mount('/sse', app=...)` where the app
is an ASGI callable. `Mount` doesn't go through `request_response`
— the app gets the raw `(scope, receive, send)` and never needs
to return a `Response`. Mirrors how upstream's `/messages/` is
already registered.

In-process integration tests of the symptom are flaky (the bug
needs real concurrent session churn). Structural test instead:
inspect the registered routes and confirm `/sse` is a Mount.
"""

from __future__ import annotations


def test_sse_route_is_mount_not_route(app) -> None:
    """The `/sse` registration must be a `Mount`, not a `Route`."""
    from starlette.routing import Mount, Route

    candidates = [
        r for r in app.routes
        if getattr(r, "path", None) == "/sse"
    ]
    assert len(candidates) == 1, (
        f"expected exactly one /sse route entry; got {len(candidates)}: "
        f"{[type(r).__name__ for r in candidates]}"
    )
    sse = candidates[0]
    assert isinstance(sse, Mount), (
        f"/sse must be registered as a Mount (ASGI app) — got "
        f"{type(sse).__name__}. Route wraps the handler in "
        f"`request_response`, which crashes with TypeError when the "
        f"SSE handler returns None (issue A)."
    )
    assert not isinstance(sse, Route), (
        f"/sse must NOT be a Route — see issue A in UPSTREAM_ISSUES.md"
    )


def test_messages_route_is_also_mount(app) -> None:
    """`/messages` should also be a Mount — same reason as `/sse`,
    plus that's how upstream's SseServerTransport expects it."""
    from starlette.routing import Mount

    candidates = [
        r for r in app.routes
        if getattr(r, "path", None) == "/messages"
    ]
    assert len(candidates) >= 1, "expected at least one /messages route"
    # The MCP SDK uses Mount for /messages/; if upstream's wrapper
    # is a Route, it has the same NoneType bug.
    assert any(isinstance(c, Mount) for c in candidates), (
        "/messages must be registered as a Mount for ASGI streaming "
        "compatibility"
    )
