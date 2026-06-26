"""Regression guard: the home-manager module's router service must
declare every env var the router needs to run under a non-root user.

Background: from at least 2026-06-23 onward, real home-manager deploys
hit a restart-loop on agent-mcp-router.service because
``nix/home-manager-module.nix`` did not set ``AGENT_MCP_ROUTER_DB``.
The Python default in ``agent_mcp.router.migrations_runner`` is
``/var/lib/agent-mcp/router.db``; that path is unwritable by a
user-mode systemd unit running as ``dennis``, so
``run_router_migrations_upgrade()`` raised
``PermissionError: [Errno 13] Permission denied: '/var/lib/agent-mcp'``
on every start.

This test parses the home-manager module's router-service
``Environment`` block and asserts every env var the router reads on
startup is present. Anchoring the assertion on the
``services.agent-mcp.router.*`` config -> Environment mapping prevents
the same shape of regression from sneaking back in (e.g. a new env
var with a ``/var/lib`` default gets read by the router but nobody
remembers to set it in the user-scope unit).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


# Path resolution mirrors what nix sees at flake-eval time: the module
# lives at <repo-root>/nix/home-manager-module.nix.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_HM_MODULE = _REPO_ROOT / "nix" / "home-manager-module.nix"


def _extract_router_environment_block(text: str) -> str:
    """Return the contents of the router service's ``Environment = [ ... ]``
    list, raw nix source. Includes the conditional ``++ lib.optionals``
    additions so SSO env vars are matched too."""
    # The router unit is the second systemd.user.services entry. Find
    # the "agent-mcp-router" key, then the Environment = [ ... ] inside.
    # We don't need a real nix parser — the file is hand-authored with
    # consistent shape and our checks are presence-only.
    marker = '"agent-mcp-router" = {'
    start = text.index(marker)
    # Inside the router block, find the Environment = [ ... ] list.
    env_idx = text.index("Environment = [", start)
    # The list extends until the matching `];` followed by an optional
    # `++ lib.optionals … [ … ]` chain that the existing module uses
    # for SSO vars. Walk forward up to the start of the next attribute
    # in the Service block (we stop at the line that begins
    # ``RuntimeDirectory = `` — a stable anchor that follows the
    # Environment chain in the current module).
    end = text.index("RuntimeDirectory = ", env_idx)
    return text[env_idx:end]


def test_router_environment_sets_AGENT_MCP_ROUTER_DB() -> None:
    """The router service must set ``AGENT_MCP_ROUTER_DB`` to an
    XDG_DATA_HOME path. Without this, user-mode systemd falls back to
    the ``/var/lib/agent-mcp/router.db`` default which it can't write,
    and the router restart-loops forever (see module docstring)."""
    text = _HM_MODULE.read_text()
    env_block = _extract_router_environment_block(text)
    assert "AGENT_MCP_ROUTER_DB" in env_block, (
        "home-manager-module.nix must set AGENT_MCP_ROUTER_DB on "
        "agent-mcp-router.service; the python default "
        "(/var/lib/agent-mcp/router.db) is unwritable by user-mode units."
    )
    # It must point at an XDG_DATA_HOME-style path, not /var/lib/*.
    # Match the line that sets the variable.
    line_match = re.search(
        r'"AGENT_MCP_ROUTER_DB=([^"]+)"', env_block
    )
    assert line_match is not None, (
        "AGENT_MCP_ROUTER_DB must be set via a quoted "
        '"AGENT_MCP_ROUTER_DB=<path>" entry in the Environment list.'
    )
    value = line_match.group(1)
    assert "/var/lib" not in value, (
        f"AGENT_MCP_ROUTER_DB must not point under /var/lib (got {value!r}); "
        "user-mode units cannot write there."
    )
    # XDG_DATA_HOME defaults to ~/.local/share; the home-manager idiom
    # is `${config.xdg.dataHome}/...`. Accept that, or an explicit
    # ~/.local/share interpolation via %h.
    assert "xdg.dataHome" in value or "%h/.local/share" in value, (
        f"AGENT_MCP_ROUTER_DB={value!r} should resolve under "
        "XDG_DATA_HOME (use ${config.xdg.dataHome}/agent-mcp/router.db "
        "or %h/.local/share/agent-mcp/router.db)."
    )


def test_router_environment_has_no_var_lib_defaults() -> None:
    """Defense in depth: no env var in the user-mode router unit may
    point under /var/lib/*. User-mode systemd cannot write there."""
    text = _HM_MODULE.read_text()
    env_block = _extract_router_environment_block(text)
    var_lib_matches = re.findall(r'"AGENT_MCP_[A-Z_]+=/var/lib[^"]*"', env_block)
    assert var_lib_matches == [], (
        f"User-mode router unit env vars must not point under /var/lib: "
        f"{var_lib_matches}"
    )


@pytest.mark.parametrize(
    "var_name",
    [
        # Env vars the router reads at startup. Each MUST be set in
        # the home-manager router unit so the user-mode service has
        # the values it needs without falling back to root-only paths.
        # Sources:
        #   - agent_mcp/router/migrations_runner.py:_DEFAULT_ROUTER_DB
        #   - agent_mcp/router/app.py:PROJECTS_FILE / SOCK_DIR / DASHBOARD_DIR
        #     / ROUTER_PORT / EXTERNAL_URL
        "AGENT_MCP_PROJECTS_FILE",
        "AGENT_MCP_SOCK_DIR",
        "AGENT_MCP_DASHBOARD_DIR",
        "AGENT_MCP_EXTERNAL_URL",
        "AGENT_MCP_DEFAULT_WORKSPACE",
        "AGENT_MCP_ROUTER_PORT",
        "AGENT_MCP_IDLE_SEC",
        "AGENT_MCP_ROUTER_DB",
    ],
)
def test_router_environment_declares_required_var(var_name: str) -> None:
    """Each env var the router reads on startup must be declared in
    the home-manager router service's Environment block."""
    text = _HM_MODULE.read_text()
    env_block = _extract_router_environment_block(text)
    assert var_name in env_block, (
        f"home-manager-module.nix: router service Environment must set "
        f"{var_name} (the router reads it at startup; missing it makes "
        f"the unit fall back to a path/value user-mode cannot satisfy)."
    )
