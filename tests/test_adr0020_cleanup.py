"""ADR-0020 cleanup: mount-aware .mcp.json snippet (the per-request asset
prefix is covered in tests/router/test_adr0020_root_mount.py, which has
the router env). The snippet must stay /agent-mcp on the tailnet + default
and go clean ("") at a root front door.
"""

from __future__ import annotations

import json

import pytest

from tests.harness import make_principal, mcp_session


# ── .mcp.json snippet honours the mount prefix ──────────────────────


def test_snippet_url_mount_prefix_direct() -> None:
    from agent_mcp.tools.admin_tools import _build_mcp_config_snippet

    def url(**kw):
        return json.loads(_build_mcp_config_snippet(**kw))[
            "mcpServers"]["agent-mcp"]["url"]

    # Root front door → no /agent-mcp in the pasteable URL.
    assert url(project="p", token="t", host="https://mm.best.aau.dk",
               mount_prefix="") == "https://mm.best.aau.dk/mcp/p"
    # Tailnet + default (CLI/env-fallback) → /agent-mcp preserved.
    assert url(project="p", token="t", host="https://h.ts.net",
               mount_prefix="/agent-mcp") == "https://h.ts.net/agent-mcp/mcp/p"
    assert url(project="p", token="t", host="https://h.ts.net") == \
        "https://h.ts.net/agent-mcp/mcp/p"


@pytest.mark.asyncio
async def test_register_snippet_root_mount(tmp_path) -> None:
    """register_agent with mount_prefix="" mints a root-mount snippet."""
    from agent_mcp.tools.admin_tools import register_agent_tool_impl
    from agent_mcp.core.tool_result import Ok

    async with mcp_session(tmp_path):
        res = await register_agent_tool_impl(
            {
                "name": "zzz-mp-root",
                "role": "worker",
                "host": "https://mm.best.aau.dk",
                "mount_prefix": "",
            },
            principal=make_principal(
                kind="operator_session", user_id="op",
                project_name="demo", project_role="operator",
            ),
        )
        assert isinstance(res, Ok)
        url = json.loads(res.data["mcp_snippet"])[
            "mcpServers"]["agent-mcp"]["url"]
        assert "/agent-mcp/" not in url, url
        assert url == "https://mm.best.aau.dk/mcp/demo", url
