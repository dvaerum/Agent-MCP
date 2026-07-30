"""Regression guards: dashboard subscribes to MCP notifications on GET /mcp.

Background
----------

PR #79 (Candidate A) wired session_registry into the transport, so
`notifications/resources/updated`, `notifications/prompts/list_changed`,
and `notifications/tools/list_changed` actually arrive on GET /mcp SSE
streams for the bearer's agent_id.

Candidate E (this PR) wires the dashboard to subscribe. When a
notification arrives, the dashboard invalidates the relevant zustand
store slices so other tabs / the same tab see fresh data within seconds
instead of waiting up to 60s for the data-store auto-poll tick.

URL plumbing
------------

The dashboard derives `projectName` from `window.location.pathname`
(`/agent-mcp/__dashboard/<name>/...`) in `lib/project-context.ts`.
The MCP Streamable HTTP endpoint for that project lives at
`/agent-mcp/<name>/mcp` — note this is NOT the `/__api/` REST prefix;
it's the router's MCP path served as the wrapped backend's `/mcp`.

The `apiClient.createEventSource('/mcp')` helper would resolve to
`{baseUrl}/mcp` = `/agent-mcp/__api/<name>/mcp` which the router does
not expose. We therefore build the MCP URL separately from `baseUrl`
(see `mcpUrlForProject()` in `lib/mcp-notifications.ts`).

Auth
----

`EventSource` (the browser primitive) cannot send custom headers and
won't carry cookies cross-origin reliably, so the dashboard uses
`fetch` + a ReadableStream reader instead.

Wave 2 (cleanup-wave-2, 2026-06-20) migrated the subscription off
bearer auth onto cookie auth. The fetch sends `credentials: "include"`;
the router's `backend_mcp_handler` validates the `agent_mcp_session`
cookie + project membership and injects the project's admin token
upstream so the backend's `AuthHeaderMiddleware` (still bearer-only)
sees a valid bearer. No admin token ever lives in JS memory anymore.

These tests are text-level (the fork has no jsdom/RTL infrastructure
for behavioural dashboard tests). Runtime is verified by `npm run
build` + the smoke test in the PR body.
"""

from __future__ import annotations

import re
from pathlib import Path


_DASHBOARD = Path(__file__).resolve().parents[1] / "agent_mcp" / "dashboard"
_MCP_NOTIF = _DASHBOARD / "lib" / "mcp-notifications.ts"


def _read(rel: str) -> str:
    return (_DASHBOARD / rel).read_text(encoding="utf-8")


# -- module existence -----------------------------------------------------


def test_mcp_notifications_module_exists() -> None:
    """The subscription lives in its own module so the wiring is
    discoverable + the listener-shape decisions are reviewable in
    isolation from the rest of `lib/api.ts`."""
    assert _MCP_NOTIF.exists(), (
        f"expected {_MCP_NOTIF} to exist — Candidate E adds a dedicated "
        "module that opens a GET /mcp SSE subscription and routes the "
        "JSON-RPC notification frames into the right store invalidations"
    )


# -- transport shape ------------------------------------------------------


def test_uses_fetch_not_eventsource_for_cookie_auth() -> None:
    """EventSource can't reliably ride a cross-origin cookie, so the
    Wave-2 subscription uses `fetch` (+ ReadableStream) with
    `credentials: "include"` to send the operator session cookie.

    Wave 2 (cleanup-wave-2, 2026-06-20) replaced the bearer-header
    construction with cookie auth — see lib/mcp-notifications.ts.
    The router's `backend_mcp_handler` resolves the cookie to the
    project's admin token and injects the bearer upstream so the
    backend's `AuthHeaderMiddleware` (still bearer-only) accepts the
    request.
    """
    src = _read("lib/mcp-notifications.ts")
    assert "fetch(" in src, (
        "expected `fetch(` in lib/mcp-notifications.ts — EventSource "
        "can't carry a cookie cross-origin reliably"
    )
    assert "getReader" in src or "ReadableStream" in src, (
        "expected a ReadableStream reader (`getReader()`) — the "
        "subscription consumes the response body as a stream and parses "
        "SSE frames manually"
    )
    # Cookie auth: credentials: "include" must be set on the fetch.
    assert re.search(r"credentials\s*:\s*['\"]include['\"]", src), (
        "expected `credentials: \"include\"` on the /mcp fetch — the "
        "operator session cookie carries auth post-Wave-2"
    )
    # And the legacy bearer construction must be gone — a stray
    # `Authorization: Bearer ${...}` would re-leak the admin token
    # back into JS memory and bypass the cookie path.
    assert not re.search(r"Authorization\s*:\s*[`'\"]Bearer", src), (
        "lib/mcp-notifications.ts must not construct an `Authorization: "
        "Bearer` header — Wave 2 moved auth to the session cookie"
    )


def test_mcp_url_uses_project_prefix_not_api_prefix() -> None:
    """The MCP path is `/agent-mcp/<name>/mcp`, NOT
    `/agent-mcp/__api/<name>/mcp` (the latter is the REST proxy
    prefix). Catches the bug where someone would naively call
    `apiClient.createEventSource('/mcp')` and get a 404."""
    src = _read("lib/mcp-notifications.ts")
    # The path-prefix literal — split-string forms are OK too, so we
    # search for the segments rather than a single full literal.
    assert "/agent-mcp/" in src, (
        "expected `/agent-mcp/` URL segment in mcp-notifications.ts — "
        "the path-prefixed deployment serves MCP at this root"
    )
    assert "/mcp" in src, (
        "expected `/mcp` URL segment in mcp-notifications.ts"
    )
    # The most common bug: building `${baseUrl}/mcp` which resolves to
    # `/agent-mcp/__api/<name>/mcp` (404).
    bad = re.search(r"\$\{(?:[\w.]+\.)?baseUrl\}/mcp", src)
    assert bad is None, (
        "found `${...baseUrl}/mcp` in mcp-notifications.ts — that "
        "resolves to /agent-mcp/__api/<name>/mcp (404). Use "
        "/agent-mcp/<projectName>/mcp instead"
    )
    # And don't accidentally embed the __api prefix in the MCP URL.
    bad_api = re.search(r"/__api/[^'\"`]*\bmcp\b", src)
    assert bad_api is None, (
        "found `/__api/.../mcp` literal in mcp-notifications.ts — the "
        "MCP transport is mounted under /agent-mcp/<name>/mcp directly, "
        "not under the REST prefix"
    )


# -- notification dispatch ------------------------------------------------


def test_dispatches_prompts_list_changed_to_data_store() -> None:
    """`notifications/prompts/list_changed` triggers the existing
    `notifyPromptsListChanged()` helper (added in PR #70). The path
    `data-store.ts:invalidatePromptsCatalog` exists already; this PR
    only needs to call into it."""
    src = _read("lib/mcp-notifications.ts")
    assert "prompts/list_changed" in src, (
        "expected handling of `notifications/prompts/list_changed` "
        "method in mcp-notifications.ts"
    )
    assert "notifyPromptsListChanged" in src or "invalidatePromptsCatalog" in src, (
        "expected the prompts-changed branch to call "
        "notifyPromptsListChanged() (exported from lib/stores/data-store.ts) "
        "OR invalidatePromptsCatalog() directly"
    )


def test_dispatches_resources_updated_to_data_store_refresh() -> None:
    """`notifications/resources/updated` with
    `params.uri = agent-mcp://inbox/<agent_id>` (or status/...)
    must trigger a data-store refresh so message counters + ambient
    state update without waiting for the 60s poll."""
    src = _read("lib/mcp-notifications.ts")
    assert "resources/updated" in src, (
        "expected handling of `notifications/resources/updated` method"
    )
    # The agent-mcp:// URI scheme is the load-bearing namespace for
    # inbox / status resources.
    assert "agent-mcp://" in src or "inbox" in src or "refreshData" in src, (
        "expected the resources-updated branch to refresh data-store "
        "(e.g. call useDataStore.getState().refreshData()) so the "
        "messages list + agent counters update in real time"
    )


def test_dispatches_tools_list_changed() -> None:
    """`notifications/tools/list_changed` is the third notification
    surface. Even if the dashboard doesn't yet render a tool catalogue,
    the listener must recognise the method so future tool-aware UI
    work can hook into the existing dispatch table without re-touching
    this module."""
    src = _read("lib/mcp-notifications.ts")
    assert "tools/list_changed" in src, (
        "expected handling of `notifications/tools/list_changed` method "
        "in mcp-notifications.ts (even a no-op or debug-log handler "
        "documents that the dispatch table covers all three notification "
        "kinds the backend currently emits)"
    )


# -- resilience -----------------------------------------------------------


def test_subscribes_with_reconnect_and_backoff() -> None:
    """The connection drops happen (server restart, transient network
    error, lazily-spawned backend going to sleep). The subscription
    must reconnect with exponential backoff capped at 30s."""
    src = _read("lib/mcp-notifications.ts")
    # Reconnect loop. Accept any of: setTimeout-based scheduler with
    # doubling delay, while(true) try/catch, or an explicit `reconnect`
    # / `backoff` symbol.
    has_setTimeout = "setTimeout" in src
    assert has_setTimeout, (
        "expected `setTimeout(...)` to schedule reconnect attempts"
    )
    # Exponential growth marker. Accept `* 2`, `** attempt`, `Math.pow`.
    has_exp = (
        "* 2" in src or "** " in src or "Math.pow" in src or "Math.min" in src
    )
    assert has_exp, (
        "expected exponential growth on the reconnect delay (e.g. "
        "`delay = Math.min(maxDelay, delay * 2)` or `base ** attempt`)"
    )
    # The 30s cap.
    assert "30000" in src or "30_000" in src, (
        "expected the 30000ms (30s) cap on reconnect backoff — without "
        "it a long outage produces minutes-long retry gaps"
    )


def test_subscribe_opens_stream_against_cookie_sse_endpoint() -> None:
    """Re-enabled after the operator SSE endpoint shipped
    (``GET /api/events``, cookie-authenticated — the operator
    live-update channel). The verify-all-v8 no-op guard held only
    *until* a cookie-auth SSE endpoint existed; now that it does,
    ``subscribeMcpNotifications`` opens the notification stream on mount
    AND attaches a visibilitychange listener (battery-saver: pause the
    stream when the tab is hidden, resume when visible).

    The lower-level ``openMcpNotificationStream`` stays exported with its
    cookie+fetch+backoff shape (the reconnect/backoff + fetch-not-
    EventSource regression tests pin it); ``subscribeMcpNotifications``
    is the run-loop wrapper this test guards.
    """
    src = _read("lib/mcp-notifications.ts")

    # Pin the no-op body shape: an empty function body or a body that
    # only returns a no-op cleanup. We do that by asserting the
    # ``subscribeMcpNotifications`` function body does NOT open a
    # stream (no ``openMcpNotificationStream(`` call inside it) and
    # does NOT register a visibilitychange listener.
    fn_match = re.search(
        r"export\s+function\s+subscribeMcpNotifications\s*\("
        r"[^)]*\)\s*:\s*\(\)\s*=>\s*void\s*\{(.*?)\n\}",
        src,
        re.DOTALL,
    )
    assert fn_match is not None, (
        "expected `export function subscribeMcpNotifications(): () => void"
        " { ... }` declaration in lib/mcp-notifications.ts"
    )
    body = fn_match.group(1)
    assert "openMcpNotificationStream(" in body, (
        "subscribeMcpNotifications must open the notification stream now "
        "that the cookie-auth GET /api/events endpoint exists (operator "
        "SSE live-update channel). Body:\n"
        + body
    )
    assert "visibilitychange" in body, (
        "subscribeMcpNotifications must attach a visibilitychange listener "
        "to pause the live stream when the tab is hidden and resume it "
        "when visible. Body:\n" + body
    )

    # Belt-and-braces: the per-URL opener IS still exported (its
    # cookie+backoff shape is exercised by the two regression tests
    # above; keeping it lets a future endpoint plug back in without
    # rewriting the run loop).
    assert "export function openMcpNotificationStream" in src, (
        "expected openMcpNotificationStream to remain exported so a "
        "future cookie-authenticated SSE notification endpoint can "
        "wire back in without re-implementing the run loop"
    )


# -- wiring into the app --------------------------------------------------


def test_wired_from_a_dashboard_provider() -> None:
    """The subscription has to be started somewhere. The natural seam
    is a client-side provider that boots on app mount (mirrors how
    project-context-provider wires the path-prefix singleton).

    Wave 2 (cleanup-wave-2, 2026-06-20): the provider no longer needs
    an admin token from the data-store — the operator session cookie
    is sent automatically once the operator has logged in. The
    provider just calls `subscribeMcpNotifications()` from a
    useEffect on mount.
    """
    # Either a dedicated provider or a hook called from layout. Accept
    # either pattern but pin that the wiring exists.
    candidates = list(
        (_DASHBOARD / "components" / "providers").glob("*notification*")
    ) + list(
        (_DASHBOARD / "components" / "providers").glob("*mcp*")
    )
    if not candidates:
        # Fallback: maybe the subscription is auto-started by the
        # module itself (an IIFE / module-load side effect). Accept
        # that pattern by checking the module references its public
        # entry point from somewhere reachable.
        src = _read("lib/mcp-notifications.ts")
        assert "subscribeMcpNotifications" in src, (
            "expected either a *notification*-named provider in "
            "components/providers/ OR a self-bootstrapping module. "
            "Found neither."
        )
        return
    # If there is a provider, it must be rendered from app/layout.tsx
    # (otherwise it never mounts).
    layout_src = _read("app/layout.tsx")
    provider_names = [c.stem for c in candidates]
    assert any(
        name in layout_src
        or name.replace("-", "").lower() in layout_src.replace("-", "").lower()
        for name in provider_names
    ), (
        f"found provider files {provider_names} but none of them is "
        "rendered from app/layout.tsx — the subscription will never "
        "start"
    )
