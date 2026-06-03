"""Path-prefix deployments must show a connected dashboard without
the `proxy:0 Disconnected` ghost entry.

Regression context. PR #23 added `apiClient.setBaseUrl` so dashboard
fetches under path-prefixed deployments (`/agent-mcp/__dashboard/<name>/`)
go through the router proxy. PR's `ApiClientInitializer` then auto-seeds
a `server-store` entry so the upstream sidebar gate is satisfied.
The seed uses placeholder `host: 'proxy', port: 0` because the entry
needs *something* for the existing `setServer(host, port)` flow.

That breaks two ways in production:

1. **Connection bug.** `serverStore.setActiveServer` calls
   `apiClient.setServer(host, port)`, which overwrites baseUrl to
   `http://proxy:0/api`. The earlier `setBaseUrl('/agent-mcp/__api/<name>')`
   from the seed is lost. `checkServerHealth` then fails (no
   `proxy:0` host) and the entry status flips to `'error'`. The
   cold-start retry loop re-triggers setActiveServer → same overwrite
   → never connects. Dennis sees "Disconnected" forever.

2. **Display bug.** The sidebar (`server-connection.tsx`, the
   management modal, overview-dashboard) renders `{server.host}:{server.port}`
   verbatim — so the user sees the literal string `proxy:0` under
   the project name. Pure cosmetic, but visible and confusing.

Fix shape. Add an optional `baseUrl?: string` field to `MCPServer`.
When set, `setActiveServer` and `checkServerHealth` call
`apiClient.setBaseUrl(server.baseUrl)` instead of
`apiClient.setServer(host, port)`. Display components hide host:port
when `baseUrl` is present. The auto-seed in `ApiClientInitializer`
passes `baseUrl: '/agent-mcp/__api/<name>'` when seeding.

These are regression guards — they parse the .ts/.tsx as text rather
than execute it. The fix is verified end-to-end via `npm run build`
plus a Firefox MCP click-through documented on the PR.
"""

from __future__ import annotations

from pathlib import Path

STORE = Path("agent_mcp/dashboard/lib/stores/server-store.ts")
# Candidate C refactor moved the auto-seed side effect out of the
# old `api-client-initializer.tsx` and into the module-load body of
# `lib/project-context.ts`. The seed still happens — just synchronously
# at module import (gated on `persist.onFinishHydration` as before)
# instead of via a React effect.
INIT = Path("agent_mcp/dashboard/lib/project-context.ts")
CONN = Path("agent_mcp/dashboard/components/server/server-connection.tsx")
MODAL = Path("agent_mcp/dashboard/components/server/server-management-modal.tsx")
OVERVIEW = Path("agent_mcp/dashboard/components/dashboard/overview-dashboard.tsx")


def test_mcp_server_type_has_optional_baseurl_field() -> None:
    """`MCPServer` interface must declare `baseUrl?: string` so
    path-prefix entries can carry the router-proxied API root without
    abusing `host`/`port`."""
    src = STORE.read_text()
    assert "baseUrl?: string" in src, (
        "expected `baseUrl?: string` on the MCPServer interface in "
        "server-store.ts — path-prefix entries need an explicit field "
        "rather than the `host: 'proxy', port: 0` sentinel that "
        "currently leaks into the UI as 'proxy:0 Disconnected'."
    )


def test_set_active_server_respects_explicit_baseurl() -> None:
    """`setActiveServer` must call `apiClient.setBaseUrl(server.baseUrl)`
    when the entry carries one, instead of the host:port-derived
    `apiClient.setServer(host, port)` that overwrites it."""
    src = STORE.read_text()
    assert "setBaseUrl(server.baseUrl" in src or "setBaseUrl(activeServer.baseUrl" in src or "setBaseUrl(s.baseUrl" in src, (
        "expected `setActiveServer` in server-store.ts to call "
        "`apiClient.setBaseUrl(server.baseUrl)` when the entry has one. "
        "Otherwise the path-prefix override from "
        "ApiClientInitializer is clobbered and the dashboard fetches "
        "from `http://proxy:0/api`."
    )


def test_check_server_health_respects_explicit_baseurl() -> None:
    """`checkServerHealth` likewise must not overwrite an explicit
    baseUrl with host:port — same bug, different code path
    (refresh, auto-detect)."""
    src = STORE.read_text()
    # Allow either pattern: branch on baseUrl, or a single
    # `setBaseUrl(server.baseUrl ?? \`http://...\`)` ternary.
    healthy_branch = (
        "server.baseUrl" in src
        and src.count("setBaseUrl") >= 2  # at least one in setActiveServer + one in checkServerHealth
    )
    assert healthy_branch, (
        "expected `checkServerHealth` in server-store.ts to honor "
        "`server.baseUrl` (call `apiClient.setBaseUrl(...)`) rather "
        "than calling `setServer(host, port)` unconditionally."
    )


def test_auto_seed_passes_baseurl_to_add_server() -> None:
    """The PathPrefix singleton's auto-seed must pass the router-proxied
    API root as `baseUrl` so subsequent setActiveServer/health checks
    keep using it instead of `proxy:0`."""
    src = INIT.read_text()
    # PR-B centralised the API URL build in lib/urls.ts; the singleton
    # now goes through `apiUrl()` instead of templating the URL inline.
    # The literal lives in lib/urls.ts, the import lives here.
    assert "apiUrl" in src, (
        "expected lib/project-context.ts to import the apiUrl() helper "
        "from lib/urls.ts (PR-B centralisation)"
    )
    from pathlib import Path as _P
    urls_src = _P("agent_mcp/dashboard/lib/urls.ts").read_text()
    assert "/agent-mcp/api" in urls_src, (
        "expected the path-prefix API root literal /agent-mcp/api in "
        "lib/urls.ts — needed so the persisted server entry knows "
        "where to fetch via the router proxy."
    )
    assert "baseUrl" in src and "addServer" in src, (
        "expected the auto-seed `addServer(...)` call to include a "
        "baseUrl field; otherwise the persisted entry only carries "
        "the placeholder host:port and the connection loop fails."
    )


def test_display_components_hide_host_port_for_path_prefix_entries() -> None:
    """Sidebar / modal / overview must not render `{host}:{port}` when
    `baseUrl` is set — that's how the literal string 'proxy:0' was
    leaking into the UI."""
    for path in (CONN, MODAL, OVERVIEW):
        src = path.read_text()
        # Either guard the host:port render with `!server.baseUrl`, or
        # show server.baseUrl instead, or drop the render entirely
        # when baseUrl exists. Accept any signal that the file is
        # aware of the field.
        assert "baseUrl" in src, (
            f"expected {path} to consult `server.baseUrl` and hide "
            f"`host:port` when set; otherwise path-prefix entries "
            f"render as 'proxy:0' in the UI."
        )
