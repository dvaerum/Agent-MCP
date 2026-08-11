"""Regression guards for the dashboard API client URL convention.

The convention this PR establishes: `ApiClient.baseUrl` IS the API
root (includes the `/api` path segment when present). All URL
construction inside ApiClient just appends the endpoint to baseUrl —
no method hardcodes `/api`. Hand-built fetches outside ApiClient go
through `apiClient.request` rather than concatenating onto
`getServerUrl()`.

These tests parse the .ts(x) files as text rather than executing
JavaScript. They catch regression (someone reintroduces a hardcoded
`/api` segment) but don't test runtime behavior. Runtime is
verified by `npm run build` (which must compile) plus manual
verification in the PR body.
"""

from __future__ import annotations

import re
from pathlib import Path

DASHBOARD = Path("agent_mcp/dashboard")


def _read(rel: str) -> str:
    return (DASHBOARD / rel).read_text()


def test_set_server_appends_api_to_base_url() -> None:
    """setServer must construct baseUrl with `/api` appended."""
    src = _read("lib/api/client.ts")
    # find the setServer method body (until next blank-line/method)
    m = re.search(r"setServer\([^)]*\)\s*\{(.*?)\n\s*\}", src, re.DOTALL)
    assert m, "setServer not found in api.ts"
    body = m.group(1)
    assert "/api" in body, (
        "setServer must set baseUrl with `/api` suffix so request() doesn't "
        "have to hardcode it; got body:\n" + body
    )


def test_api_ts_does_not_hardcode_api_segment_after_base_url() -> None:
    """api.ts must not have `${baseUrl}/api` patterns — baseUrl IS the API root.

    Catches `${this.baseUrl}/api${endpoint}` in request(),
    `${this.baseUrl}/api${endpoint}` in createEventSource(),
    `${this.baseUrl}/api/health` in testCORS(), etc.
    """
    src = _read("lib/api/client.ts")
    # Match either `${baseUrl}/api...` or `${this.baseUrl}/api...`
    bad = re.findall(r"\$\{(?:this\.)?baseUrl\}/api", src)
    assert not bad, (
        f"found {len(bad)} occurrences of `${{baseUrl}}/api` in api.ts; "
        "baseUrl should already include /api, so the literal `/api` is "
        "redundant and causes double-prefix bugs in path-routed deployments. "
        f"Matches: {bad}"
    )


def test_data_store_does_not_hand_build_api_urls() -> None:
    """data-store.ts must route through apiClient.request, not concat /api/."""
    src = _read("lib/stores/data-store.ts")
    # Forbid the specific pattern `apiClient.getServerUrl()...api`
    assert "getServerUrl()}/api" not in src, (
        "data-store.ts must not hand-build URLs with `getServerUrl()/api/...`; "
        "use apiClient.request('/...') instead."
    )
