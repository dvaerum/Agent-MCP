"""Regression guards: dashboard ApiClient + ApiClientInitializer
support explicit base URLs for path-prefixed deployments.

Today's flow: ApiClientInitializer auto-seeds a synthetic server
entry with `(host='proxy', port=0)` when the dashboard URL matches
`/agent-mcp/__dashboard/<name>/`, then calls setServer(proxy, 0).
The fork's PR #7 setServer produces `http://proxy:0/api` — broken.

Two changes here let deployments avoid the broken URL without an
out-of-tree patch:

1. New ApiClient.setBaseUrl(url: string) accepts an explicit base
   URL. Used as an alternative to setServer(host, port) when the
   caller already knows the API root.

2. ApiClientInitializer, when the path-prefix matches, calls
   apiClient.setBaseUrl with the derived URL
   (`/agent-mcp/__api/<name>`) so subsequent fetches resolve through
   the router's proxy instead of the broken `http://proxy:0/api`.
"""

from __future__ import annotations

from pathlib import Path

DASHBOARD = Path("agent_mcp/dashboard")


def _read(rel: str) -> str:
    return (DASHBOARD / rel).read_text()


def test_api_client_has_set_base_url_method() -> None:
    src = _read("lib/api.ts")
    assert "setBaseUrl" in src, (
        "expected ApiClient to expose a setBaseUrl(url: string) method "
        "so deployments can override baseUrl without going through "
        "setServer's host:port construction"
    )


def test_path_prefix_singleton_uses_set_base_url() -> None:
    """Candidate C refactor (2026-06-01) moved this side effect from
    the old api-client-initializer.tsx useEffect into the module-load
    body of `lib/project-context.ts`. When the path-prefix matches,
    the singleton must call `apiClient.setBaseUrl` with the derived
    `/agent-mcp/__api/<name>` URL so the very first fetch already
    routes through the proxy."""
    src = _read("lib/project-context.ts")
    assert "setBaseUrl" in src, (
        "expected lib/project-context.ts to call apiClient.setBaseUrl "
        "with the derived URL when the dashboard URL matches "
        "/agent-mcp/__dashboard/<name>/"
    )
    assert "/agent-mcp/__api/" in src, (
        "expected the path-derived URL (/agent-mcp/__api/...) in "
        "lib/project-context.ts"
    )
