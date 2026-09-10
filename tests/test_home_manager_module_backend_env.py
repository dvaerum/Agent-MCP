"""Regression guard: the home-manager module's per-project backend
service declares the env vars its OWN implementation actually needs —
and, now that the implementation changed, does NOT carry over an env
var whose need was specific to the retired one.

Background (Python-backend era)
--------------------------------

PR #223 fixed ``agent-mcp-router.service`` by setting
``AGENT_MCP_ROUTER_DB=${config.xdg.dataHome}/agent-mcp/router.db``. The
same drift then bit the per-project backend template
(``agent-mcp@<project>.service``), which never set the env var at all.

Real reproduction on the deployed system (2026-06-24)::

    $ curl -b cookies '.../api/<project>/all-data' \\
        -H 'Accept: application/vnd.agent-mcp.v1+json'
    -> 401 {"detail":{"error":"login_required", ...}}

    $ journalctl --user -u 'agent-mcp@<project>.service'
    agent_mcp.app.deps - WARNING - operator-session resolution failed
      for session '...'; treating as anonymous

Root cause: the Python backend's ``_resolve_session_user`` lazily
imported ``..router.identity`` and opened the SAME sqlite router.db the
router process used, to resolve a forwarded operator-session cookie
directly against it. Without ``AGENT_MCP_ROUTER_DB`` set, that open hit
the ``/var/lib/agent-mcp/router.db`` default, which user-mode units
cannot read.

Retirement note — this requirement does not carry over to conexus@
-----------------------------------------------------------------------

The Python backend (``agent-mcp@<name>.service``) was retired together
with the rest of the Python implementation; ``conexus@<name>.service``
(Rust) is the sole per-project backend now, and it does NOT open
router.db at all — see ``home-manager-module.nix``'s own comment on the
``conexus@`` unit: "conexus-backend doesn't touch the router.db at all
... matching Python's own documented behavior for this seam" (Wave 3
already deleted the parallel ``--system-token-out`` plumbing; the
forwarding-HMAC signature the router signs into its proxied requests is
the ONLY remaining router->backend auth channel). So the invariant this
file now pins is the opposite of its original one: `conexus@` must NOT
carry an `AGENT_MCP_ROUTER_DB` entry that would be dead weight
suggesting a code path that doesn't exist in this implementation.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_HM_MODULE = _REPO_ROOT / "nix" / "home-manager-module.nix"


def _extract_backend_service_block(text: str) -> str:
    """Return the raw nix source of the ``"conexus@" = lib.mkIf … { … };``
    entry inside ``systemd.user.services``, tracked by brace depth (the
    block contains its own nested ``{ … }``, so a naive "next sibling
    key" string search is not a safe anchor)."""
    marker = '"conexus@" = lib.mkIf'
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


def _extract_backend_environment_block(text: str) -> str:
    """Return the contents of the backend template's
    ``Environment = [ ... ]`` list, or ``""`` if it has none (which is
    the current, correct shape for `conexus@` — see module docstring)."""
    block = _extract_backend_service_block(text)
    if "Environment = [" not in block:
        return ""
    env_idx = block.index("Environment = [")
    end = block.index("];", env_idx) + len("];")
    return block[env_idx:end]


def test_backend_service_block_is_conexus() -> None:
    """Sanity anchor: the per-project backend template is `conexus@`.

    If this ever stops matching, every other assertion in this file is
    silently vacuous (the extraction helper would raise ValueError
    first, but this gives a clearer failure message for the common
    case of the marker string drifting).
    """
    text = _HM_MODULE.read_text()
    assert '"conexus@" = lib.mkIf' in text, (
        "expected the per-project backend template to be named "
        '"conexus@" in nix/home-manager-module.nix'
    )


def test_backend_does_not_set_AGENT_MCP_ROUTER_DB() -> None:
    """`conexus@` must NOT set `AGENT_MCP_ROUTER_DB`.

    Unlike the retired Python backend, `conexus-backend` never opens
    router.db directly (see module docstring) — the forwarding-HMAC
    signature is its only router-trust channel. Setting this var here
    would be dead weight at best, and at worst a signal that someone
    is trying to re-add a router.db-opening code path to the backend
    without updating the auth architecture doc alongside it.
    """
    text = _HM_MODULE.read_text()
    env_block = _extract_backend_environment_block(text)
    assert "AGENT_MCP_ROUTER_DB" not in env_block, (
        "conexus@ backend template sets AGENT_MCP_ROUTER_DB, but "
        "conexus-backend does not open router.db (forwarding-HMAC is "
        "its only router-trust channel) — either this is dead "
        "configuration, or the backend gained a new router.db-opening "
        "code path that this test (and the module's own comment on "
        "the conexus@ unit) needs updating to reflect."
    )


def test_backend_environment_has_no_var_lib_defaults() -> None:
    """Defense in depth: no env var in the user-mode backend template
    may point under /var/lib/*. User-mode systemd cannot read or write
    there (the per-user systemd manager runs as the operator, not root)."""
    text = _HM_MODULE.read_text()
    env_block = _extract_backend_environment_block(text)
    var_lib_matches = re.findall(
        r'"AGENT_MCP_[A-Z_]+=/var/lib[^"]*"', env_block
    )
    assert var_lib_matches == [], (
        "User-mode backend template env vars must not point under "
        f"/var/lib: {var_lib_matches}"
    )
