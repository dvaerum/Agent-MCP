"""Regression guard: the home-manager module's router service must
declare every env var / CLI flag the router needs to run under a
non-root user.

Background: from at least 2026-06-23 onward, real home-manager deploys
hit a restart-loop on the (then-Python) router service because
``nix/home-manager-module.nix`` did not set ``AGENT_MCP_ROUTER_DB``.
The Python default was ``/var/lib/agent-mcp/router.db``; that path is
unwritable by a user-mode systemd unit running as ``dennis``, so the
router's schema-migration step raised
``PermissionError: [Errno 13] Permission denied: '/var/lib/agent-mcp'``
on every start.

Retirement note
----------------

The Python router (``agent-mcp-router.service``) was retired together
with the rest of the Python implementation; ``conexus-router`` (Rust)
is now the only router. Most of the config surface this test used to
find as ``Environment`` entries is now a real CLI flag on
``conexus-router`` (see its own ``Cli`` struct doc in
``rust/conexus-router/src/main.rs``) — ``--projects-file``,
``--sock-dir``, ``--dashboard-dir``, ``--external-url``, ``--idle-sec``,
``--port``. A handful have NO CLI-flag equivalent (env-var-only on the
Rust router, mirroring the Python router's own env-var-only knobs) and
must still be set via ``Environment``: ``AGENT_MCP_ROUTER_DB``,
``AGENT_MCP_ROUTER_HOST``, ``AGENT_MCP_DEFAULT_WORKSPACE``.

This test parses the home-manager module's router-service
``Environment`` block for the env-var-only knobs and its ``ExecStart``
for the flag-shaped ones, anchoring on the ``services.agent-mcp.router.*``
config -> unit mapping so the same class of regression (a new
router-startup input added upstream, and nobody remembering to wire it
into the user-scope unit) can't sneak back in.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# Path resolution mirrors what nix sees at flake-eval time: the module
# lives at <repo-root>/nix/home-manager-module.nix.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_HM_MODULE = _REPO_ROOT / "nix" / "home-manager-module.nix"


def _extract_router_service_block(text: str) -> str:
    """Return the raw nix source of the ``"conexus-router" = { … };``
    entry inside ``systemd.user.services``. The block runs to the
    matching close-brace of its own attrset (tracked by brace depth,
    since the router block itself contains nested ``{ … }`` and the
    naive "next sibling key" anchor the old Python-router version of
    this helper used doesn't hold once CLI-flag construction adds its
    own ``let … in`` block before ``ExecStart``)."""
    marker = '"conexus-router" = lib.mkIf'
    start = text.index(marker)
    brace_start = text.index("{", start)
    depth = 1
    i = brace_start + 1
    while depth > 0:
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
        i += 1
    return text[brace_start:i]


def _extract_router_environment_block(text: str) -> str:
    """Return the contents of the router service's ``Environment = [ ... ]``
    list, raw nix source. Includes the conditional ``++ lib.optionals``
    additions so SSO env vars are matched too."""
    block = _extract_router_service_block(text)
    env_idx = block.index("Environment = [")
    # The list extends until the matching `];` followed by an optional
    # `++ lib.optionals … [ … ]` chain the module uses for SSO vars.
    # Walk forward up to the start of the next attribute in the Service
    # block (a stable anchor that follows the Environment chain in the
    # current module).
    end = block.index("RuntimeDirectory = ", env_idx)
    return block[env_idx:end]


def _extract_router_exec_start(text: str) -> str:
    """Return the router service's ``ExecStart = …;`` raw nix source,
    where the flag-shaped config surface (``--port``, ``--projects-file``,
    etc.) lives."""
    block = _extract_router_service_block(text)
    start = block.index("ExecStart =")
    end = block.index("Restart = ", start)
    return block[start:end]


def test_router_environment_sets_AGENT_MCP_ROUTER_DB() -> None:
    """The router service must set ``AGENT_MCP_ROUTER_DB`` to an
    XDG_DATA_HOME path. Without this, user-mode systemd falls back to
    conexus-router's own compiled-in ``/var/lib/agent-mcp/router.db``
    default, which it can't write, and the router restart-loops
    forever (see module docstring)."""
    text = _HM_MODULE.read_text()
    env_block = _extract_router_environment_block(text)
    assert "AGENT_MCP_ROUTER_DB" in env_block, (
        "home-manager-module.nix must set AGENT_MCP_ROUTER_DB on "
        "conexus-router; the compiled-in default "
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
        # Env vars `conexus-router` reads at startup that have NO
        # CLI-flag equivalent (env-var-only, per its own `Cli` struct
        # doc in rust/conexus-router/src/main.rs). Each MUST be set in
        # the home-manager router unit so the user-mode service has
        # the values it needs without falling back to a root-only path
        # or the wrong default.
        "AGENT_MCP_ROUTER_DB",
        "AGENT_MCP_DEFAULT_WORKSPACE",
    ],
)
def test_router_environment_declares_required_var(var_name: str) -> None:
    """Each env-var-only startup input must be declared in the
    home-manager router service's Environment block."""
    text = _HM_MODULE.read_text()
    env_block = _extract_router_environment_block(text)
    assert var_name in env_block, (
        f"home-manager-module.nix: conexus-router Environment must set "
        f"{var_name} (the router reads it at startup, with no CLI-flag "
        "equivalent; missing it makes the unit fall back to a path/value "
        "user-mode cannot satisfy)."
    )


@pytest.mark.parametrize(
    "flag_name",
    [
        # Config surface that IS a real CLI flag on `conexus-router`
        # (see its own `Cli` struct doc). Each MUST appear in the
        # router unit's ExecStart.
        "--port",
        "--projects-file",
        "--sock-dir",
        "--dashboard-dir",
        "--external-url",
        "--idle-sec",
    ],
)
def test_router_exec_start_passes_required_flag(flag_name: str) -> None:
    """Each flag-shaped startup input must appear on conexus-router's
    ExecStart line(s)."""
    text = _HM_MODULE.read_text()
    exec_start = _extract_router_exec_start(text)
    assert flag_name in exec_start, (
        f"home-manager-module.nix: conexus-router ExecStart must pass "
        f"{flag_name} (real CLI flag on conexus-router; missing it "
        "makes the unit fall back to conexus-router's own compiled-in "
        "default)."
    )
