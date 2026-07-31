"""Tests for Phase 4 runtime asset-prefix substitution.

The dashboard build emits a literal sentinel string
``__AGENT_MCP_ASSET_PREFIX__`` wherever Next.js would normally bake in
the build-time ``assetPrefix``. The router substitutes the configured
prefix into served HTML/JS/CSS bodies at serve time so a single build
artifact can be deployed at any URL prefix without rebuilding.

These tests pin three guarantees:

* The sentinel IS replaced in ``text/html``, ``application/javascript``
  and ``text/css`` responses.
* The sentinel is NOT touched in ``application/json`` or binary
  (``image/png``, etc.) responses — those would otherwise corrupt
  REST API responses or binary asset bytes that happen to contain the
  sentinel sequence by chance.
* Setting the configured prefix to ``/tools`` (the canonical example
  of a reverse-proxy remount) makes the served HTML reference
  ``/tools/_next/...`` URLs.

The substitution module under test is ``agent_mcp.router.asset_prefix``
(import will fail until the module lands — that's the RED phase).
The router-wiring assertions exercise ``dashboard_handler`` and
``dashboard_assets_handler`` so the substitution actually fires on
real served bodies, not just direct function calls.
"""

from __future__ import annotations

import pytest


# Async tests below get explicit @pytest.mark.asyncio decorators rather
# than a module-level pytestmark, because the first batch of tests is
# synchronous (pure function under test) and the asyncio mark on a
# non-async function emits a noisy warning under pytest-asyncio.


# ── Pure-function tests ─────────────────────────────────────────────


def test_substitute_replaces_sentinel_with_configured_prefix() -> None:
    from agent_mcp.router.asset_prefix import (
        SENTINEL,
        substitute_asset_prefix,
    )

    payload = (
        b"<html><script src=\"" + SENTINEL.encode()
        + b"/_next/static/chunks/main.js\"></script></html>"
    )

    out = substitute_asset_prefix(payload, "/agent-mcp/assets")

    assert SENTINEL.encode() not in out
    # PR-B: assets moved out from under /__dashboard/_next/ to the
    # top-level /assets/ prefix; substitution emits the new shape.
    assert b"/agent-mcp/assets/_next/static/chunks/main.js" in out


def test_substitute_handles_multiple_sentinel_occurrences() -> None:
    from agent_mcp.router.asset_prefix import (
        SENTINEL,
        substitute_asset_prefix,
    )

    sent = SENTINEL.encode()
    payload = b"a=" + sent + b"; b=" + sent + b"; c=" + sent

    out = substitute_asset_prefix(payload, "/x")

    assert out == b"a=/x; b=/x; c=/x"


def test_substitute_no_op_when_sentinel_absent() -> None:
    from agent_mcp.router.asset_prefix import substitute_asset_prefix

    body = b"nothing to see here"
    assert substitute_asset_prefix(body, "/anything") == body


def test_substitute_with_alternate_prefix_tools() -> None:
    """An operator deploying behind a reverse proxy mounted at ``/tools/``
    just changes the configured prefix; no rebuild needed."""
    from agent_mcp.router.asset_prefix import (
        SENTINEL,
        substitute_asset_prefix,
    )

    payload = (
        b'<link rel="stylesheet" href="' + SENTINEL.encode()
        + b'/_next/static/css/app.css">'
    )

    out = substitute_asset_prefix(payload, "/tools")

    assert b"/tools/_next/static/css/app.css" in out
    assert SENTINEL.encode() not in out


# ── Content-Type gating, exercised via the router handlers ──────────


@pytest.mark.asyncio
async def test_html_response_has_sentinel_substituted(
    aiohttp_client, router_app, write_dashboard_file,
) -> None:
    """``GET /agent-mcp/app/`` rewrites HTML bodies.

    PR-B: default ASSET_PREFIX is /agent-mcp/assets (top-level segment,
    decoupled from the /app/ pages prefix); substituted output points
    at the new asset URL shape."""
    write_dashboard_file(
        "index.html",
        '<html><body>'
        '<script src="__AGENT_MCP_ASSET_PREFIX__/_next/main.js"></script>'
        '</body></html>',
    )
    client = await aiohttp_client(router_app)

    resp = await client.get("/agent-mcp/app/")
    assert resp.status == 200
    body = await resp.text()
    assert "__AGENT_MCP_ASSET_PREFIX__" not in body
    assert "/agent-mcp/assets/_next/main.js" in body


@pytest.mark.asyncio
async def test_js_response_has_sentinel_substituted(
    aiohttp_client, router_app, write_dashboard_file,
) -> None:
    """``GET /agent-mcp/assets/_next/<path>.js`` rewrites JS bodies.

    Next.js bakes the asset prefix into webpack's __webpack_require__
    runtime in chunk JS, so the JS itself contains the sentinel and
    must be substituted on serve."""
    write_dashboard_file(
        "_next/static/chunks/main-abc123.js",
        'var __webpack_public_path__ = "__AGENT_MCP_ASSET_PREFIX__/_next/";',
    )
    client = await aiohttp_client(router_app)

    resp = await client.get(
        "/agent-mcp/assets/_next/static/chunks/main-abc123.js"
    )
    assert resp.status == 200
    body = await resp.text()
    assert "__AGENT_MCP_ASSET_PREFIX__" not in body
    assert '"/agent-mcp/assets/_next/"' in body


@pytest.mark.asyncio
async def test_css_response_has_sentinel_substituted(
    aiohttp_client, router_app, write_dashboard_file,
) -> None:
    """CSS bodies — e.g. ``url(...)`` references — also get substituted."""
    write_dashboard_file(
        "_next/static/css/app-def456.css",
        '.logo{background:url("__AGENT_MCP_ASSET_PREFIX__/_next/static/media/logo.svg");}',
    )
    client = await aiohttp_client(router_app)

    resp = await client.get(
        "/agent-mcp/assets/_next/static/css/app-def456.css"
    )
    assert resp.status == 200
    body = await resp.text()
    assert "__AGENT_MCP_ASSET_PREFIX__" not in body
    assert "/agent-mcp/assets/_next/static/media/logo.svg" in body


@pytest.mark.asyncio
async def test_rsc_txt_response_has_sentinel_substituted(
    aiohttp_client, router_app, write_dashboard_file,
) -> None:
    """Next.js RSC flight payloads (served from ``<page>.txt``) carry the
    sentinel inline — both as the page's CSS-preload ``href`` and as the
    runtime ``assetPrefix`` value. The browser fetches them during
    client-side navigation and constructs CSS/JS URLs from the payload,
    so the substitution must fire on ``text/plain`` bodies too.
    Regression: bare RSC ``.txt`` payloads were passing through
    unchanged because the default extension MIME ``text/plain`` was not
    in the substitutable types tuple. Surfaced via Firefox-MCP
    click-through on 2026-06-17."""
    write_dashboard_file(
        "index.txt",
        ':HL["__AGENT_MCP_ASSET_PREFIX__/_next/static/css/app.css","style"]\n'
        '0:{"p":"__AGENT_MCP_ASSET_PREFIX__","c":["",""]}\n',
    )
    client = await aiohttp_client(router_app)

    resp = await client.get("/agent-mcp/app/e2e/index.txt")
    assert resp.status == 200
    body = await resp.text()
    assert "__AGENT_MCP_ASSET_PREFIX__" not in body
    assert "/agent-mcp/assets/_next/static/css/app.css" in body
    assert '"p":"/agent-mcp/assets"' in body


@pytest.mark.asyncio
async def test_json_api_response_is_not_substituted(
    aiohttp_client, router_app,
) -> None:
    """A JSON endpoint like ``/agent-mcp/api/router/projects`` must
    not get its body rewritten — JSON API consumers see exact bytes
    the handler produced. We use the admin projects route which
    always returns JSON."""
    client = await aiohttp_client(router_app)

    resp = await client.get(
        "/agent-mcp/api/router/projects",
        headers={"Accept": "application/vnd.agent-mcp.v1+json"},
    )
    assert resp.status == 200
    assert resp.headers["Content-Type"].startswith("application/json")
    body = await resp.text()
    # The body is JSON; the sentinel-substitution middleware would
    # have no business touching it. (The body wouldn't contain the
    # sentinel anyway; the assertion is that nothing wrapped this
    # response with substitution-enabled headers.)
    assert "__AGENT_MCP_ASSET_PREFIX__" not in body


@pytest.mark.asyncio
async def test_binary_png_response_is_passed_through_unchanged(
    aiohttp_client, router_app, write_dashboard_file,
) -> None:
    """A static PNG under the dashboard tree must pass through byte-
    for-byte even if its bytes happen to contain the sentinel ASCII
    sequence — substitution would silently corrupt the image.

    We embed the sentinel bytes inside a synthetic PNG-shaped payload
    so we can observe whether the middleware touched the body."""
    # 8-byte PNG signature + sentinel sequence + trailing arbitrary bytes.
    png_sig = b"\x89PNG\r\n\x1a\n"
    fake_png = png_sig + b"__AGENT_MCP_ASSET_PREFIX__" + b"\x00\x01\x02IDAT"
    target = write_dashboard_file("_next/static/media/badge.png", "")
    target.write_bytes(fake_png)

    client = await aiohttp_client(router_app)
    resp = await client.get(
        "/agent-mcp/assets/_next/static/media/badge.png"
    )
    assert resp.status == 200
    body = await resp.read()
    assert body == fake_png, (
        "PNG bodies must pass through verbatim; substitution would "
        "corrupt the image bytes"
    )


@pytest.mark.asyncio
async def test_alternate_configured_prefix_propagates_to_served_html(
    aiohttp_client, router_module, write_dashboard_file, monkeypatch,
) -> None:
    """ADR-0020: the served HTML's asset prefix follows the REQUEST
    MOUNT (per-request), not the static ASSET_PREFIX env. A request under
    /agent-mcp/ yields /agent-mcp/assets/… — even with ASSET_PREFIX
    monkeypatched to /tools, proving the per-request derivation wins (the
    env is now only a fallback for non-request callers)."""
    monkeypatch.setattr(router_module, "ASSET_PREFIX", "/tools")
    # Clear the rewrite cache (filled by previous tests using the default).
    from agent_mcp.router import asset_prefix as ap_module
    ap_module._CACHE.clear()

    write_dashboard_file(
        "index.html",
        '<html><script src="__AGENT_MCP_ASSET_PREFIX__/_next/x.js"></script></html>',
    )
    app = router_module.make_app()
    client = await aiohttp_client(app)

    resp = await client.get("/agent-mcp/app/")
    assert resp.status == 200
    body = await resp.text()
    # Request mount wins over the env override.
    assert "/agent-mcp/assets/_next/x.js" in body, body
    assert "/tools/" not in body, body
    assert "__AGENT_MCP_ASSET_PREFIX__" not in body
