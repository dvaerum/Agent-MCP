"""Regression guard: the home-manager module's per-project backend
service must declare the env vars the backend needs to look up router-
owned state from a non-root user.

Background: PR #223 fixed ``agent-mcp-router.service`` by setting
``AGENT_MCP_ROUTER_DB=${config.xdg.dataHome}/agent-mcp/router.db``. The
same drift then bit the per-project backend template
(``agent-mcp@<project>.service``), which never set the env var at all.

Real reproduction on the deployed system (2026-06-24):

  $ curl -b cookies '.../api/<project>/all-data' \
      -H 'Accept: application/vnd.agent-mcp.v1+json'
  → 401 {"detail":{"error":"login_required", ...}}

  $ journalctl --user -u 'agent-mcp@<project>.service'
  agent_mcp.app.deps - WARNING - operator-session resolution failed
    for session '...'; treating as anonymous

Root cause: ``agent_mcp/app/deps.py`` ``_resolve_session_user``
(lines 98-120) lazily imports ``..router.identity`` and calls
``identity.get_session(session_id)``. That opens the router DB at
``get_router_db_path()`` (``agent_mcp/router/migrations_runner.py``
lines 32-42), which honours ``AGENT_MCP_ROUTER_DB`` if set and
otherwise returns the ``/var/lib/agent-mcp/router.db`` default. User-
mode units cannot read that path; the open raises ``PermissionError``,
the bare ``except Exception`` in ``_resolve_session_user`` logs the
"treating as anonymous" warning, and every operator-only endpoint
401s. Same drift pattern as PR #223 (router unit) and PR #224
(forwarding_hmac ExecStartPre).

Both the router unit AND the per-project backend unit must point at
the SAME router.db path — they are two processes reading one SQLite
file. This test anchors that invariant.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_HM_MODULE = _REPO_ROOT / "nix" / "home-manager-module.nix"


def _extract_backend_service_block(text: str) -> str:
    """Return the raw nix source of the ``"agent-mcp@" = { … };`` entry
    inside ``systemd.user.services``. The block ends at the closing
    ``};`` that is immediately followed (modulo whitespace/comment) by
    the next service definition ``"agent-mcp-router" = {``."""
    marker = '"agent-mcp@" = {'
    start = text.index(marker)
    # The next sibling key is "agent-mcp-router". Slice up to (but not
    # including) it. The block we return covers the entire Unit +
    # Service definition for the backend template.
    end = text.index('"agent-mcp-router" = {', start)
    return text[start:end]


def _extract_backend_environment_block(text: str) -> str:
    """Return the contents of the backend template's
    ``Environment = [ ... ]`` list. Raises ``ValueError`` if the
    backend block has no Environment list (the failure mode this test
    file was written to catch)."""
    block = _extract_backend_service_block(text)
    if "Environment = [" not in block:
        raise ValueError(
            'backend template ("agent-mcp@" = { ... }) has no '
            "Environment = [ ... ] list — the backend cannot tell "
            "agent_mcp.router.* modules where router.db lives."
        )
    env_idx = block.index("Environment = [")
    # The list extends until the matching `];` on its own line. We
    # search for the close-bracket-semicolon that terminates the list.
    # Anchor on the first occurrence of "];\n" after env_idx; the
    # backend block does not use ``++ lib.optionals`` extensions today.
    end = block.index("];", env_idx) + len("];")
    return block[env_idx:end]


def _extract_router_environment_block(text: str) -> str:
    """Mirror of the helper in test_home_manager_module_env.py — used
    here to assert the backend and router DB paths agree."""
    marker = '"agent-mcp-router" = {'
    start = text.index(marker)
    env_idx = text.index("Environment = [", start)
    end = text.index("RuntimeDirectory = ", env_idx)
    return text[env_idx:end]


def _router_db_value(env_block: str) -> str:
    match = re.search(r'"AGENT_MCP_ROUTER_DB=([^"]+)"', env_block)
    assert match is not None, (
        "AGENT_MCP_ROUTER_DB must be set via a quoted "
        '"AGENT_MCP_ROUTER_DB=<path>" entry in the Environment list.'
    )
    return match.group(1)


def test_backend_environment_sets_AGENT_MCP_ROUTER_DB() -> None:
    """The per-project backend template must set ``AGENT_MCP_ROUTER_DB``.

    Without this, ``agent_mcp.app.deps._resolve_session_user`` (the
    lazy ``from ..router import identity; identity.get_session(...)``
    path) hits the ``/var/lib/agent-mcp/router.db`` default,
    PermissionErrors, and every operator-only endpoint 401s with
    ``operator-session resolution failed; treating as anonymous`` in
    the journal."""
    text = _HM_MODULE.read_text()
    env_block = _extract_backend_environment_block(text)
    assert "AGENT_MCP_ROUTER_DB" in env_block, (
        'home-manager-module.nix: "agent-mcp@" backend template must '
        "set AGENT_MCP_ROUTER_DB; without it _resolve_session_user "
        "falls back to /var/lib/agent-mcp/router.db (unreadable by "
        "user-mode units) and every operator endpoint returns 401."
    )


def test_backend_router_db_matches_router_unit_path() -> None:
    """Backend + router MUST open the same router.db file. The two
    units pointing at different paths would mean ``identity.get_session``
    in the backend looks at an empty DB and never finds the operator
    session even when the router successfully created one."""
    text = _HM_MODULE.read_text()
    backend_env = _extract_backend_environment_block(text)
    router_env = _extract_router_environment_block(text)
    assert _router_db_value(backend_env) == _router_db_value(router_env), (
        "AGENT_MCP_ROUTER_DB on the backend template must equal the "
        "value on the router unit; they are two processes opening the "
        "same SQLite file."
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


@pytest.mark.parametrize(
    "var_name",
    [
        # Env vars the backend reads (directly or via lazy router-
        # module imports) that have system-path defaults. Each MUST be
        # set in the home-manager backend template so the user-mode
        # service has the values it needs without falling back to a
        # root-only path.
        #
        # Source: agent_mcp/router/migrations_runner.py:_DEFAULT_ROUTER_DB
        # via the lazy ``from ..router import identity`` in
        # agent_mcp/app/deps.py:98-120 (_resolve_session_user).
        "AGENT_MCP_ROUTER_DB",
    ],
)
def test_backend_environment_declares_required_var(var_name: str) -> None:
    """Each env var the backend reads at runtime must be declared in
    the home-manager backend service's Environment block."""
    text = _HM_MODULE.read_text()
    env_block = _extract_backend_environment_block(text)
    assert var_name in env_block, (
        f'home-manager-module.nix: "agent-mcp@" backend template '
        f"Environment must set {var_name} (the backend reads it at "
        "runtime; missing it makes the unit fall back to a path "
        "user-mode cannot satisfy)."
    )
