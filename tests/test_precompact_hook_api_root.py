"""Precompact-hook URL-derivation contract.

The daemon-agent precompact hook (`nix/agent-mcp-daemon-agent-precompact-hook.sh.in`)
posts a tiny resume-pointer back to the project_context REST endpoint.
To do that it needs the REST API root, which it derives from
``AGENT_MCP_MCP_URL``.

Pre-PR-D (URL redesign), MCP URLs were:
    https://host/agent-mcp/<name>/mcp
so ``${mcp_url%/mcp}/api`` stripped the trailing ``/mcp`` and gave
    https://host/agent-mcp/<name>/api
which is NOT the REST root anyway — but for the project_context POST
that path coincidentally worked because the old REST surface lived
under ``/agent-mcp/<name>/...``.

Post-PR-D the URL is:
    https://host/agent-mcp/mcp/<name>
``${mcp_url%/mcp}`` is now a NO-OP (the suffix doesn't match), so the
old line produced ``https://host/agent-mcp/mcp/<name>/api`` — which
doesn't exist and 404s.

The correct derivation: strip the trailing ``/mcp/<name>`` and append
``/api/projects/<name>``. The hook then POSTs to
``/api/projects/<name>/project-context`` which is the canonical
REST-shape resource.

This test extracts the bash function and runs it in isolation against
representative URL shapes so the contract is locked.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK_TEMPLATE = (
    REPO_ROOT / "nix" / "agent-mcp-daemon-agent-precompact-hook.sh.in"
)


def _derive_api_root(mcp_url: str) -> str:
    """Run only the URL-derivation snippet from the hook against
    ``mcp_url`` and return the computed ``api_root``.

    We extract the relevant lines from the .sh.in template rather than
    duplicating the bash expression — this way a future tweak to the
    template is automatically reflected in the test.
    """
    bash = shutil.which("bash")
    if bash is None:  # pragma: no cover - bash is ubiquitous on CI/dev
        pytest.skip("bash not available")

    source = HOOK_TEMPLATE.read_text()

    # Pull out the api_root derivation block (lines containing api_root=).
    # Run it standalone with mcp_url pre-set; print the result.
    deriv = "\n".join(
        line for line in source.splitlines()
        if "api_root=" in line and not line.lstrip().startswith("#")
    )
    assert deriv, "could not extract api_root= derivation from hook template"

    script = f"""
set -euo pipefail
mcp_url={_shell_quote(mcp_url)}
{deriv}
printf '%s' "$api_root"
"""
    result = subprocess.run(
        [bash, "-c", script],
        capture_output=True, text=True, check=True,
    )
    return result.stdout


def _shell_quote(s: str) -> str:
    """Single-quote a string safely for bash."""
    return "'" + s.replace("'", "'\\''") + "'"


# ── New URL shape (post-PR-D, what production actually emits) ───────


def test_api_root_for_loopback_mcp_url() -> None:
    """Daemon wrapper builds http://127.0.0.1:1337/agent-mcp/mcp/<name>.
    The hook must derive the matching REST root."""
    api_root = _derive_api_root(
        "http://127.0.0.1:1337/agent-mcp/mcp/washing-brothers"
    )
    assert api_root == (
        "http://127.0.0.1:1337/agent-mcp/api/projects/washing-brothers"
    )


def test_api_root_for_tailnet_mcp_url() -> None:
    """If an operator points the daemon at the public tailnet URL the
    same derivation must work — `_validate_name` reserves `mcp` so the
    pattern is unambiguous."""
    api_root = _derive_api_root(
        "https://nixos-developer-system.tailfdae0.ts.net/agent-mcp/mcp/washing-brothers"
    )
    assert api_root == (
        "https://nixos-developer-system.tailfdae0.ts.net/agent-mcp/api/projects/washing-brothers"
    )


def test_api_root_handles_single_hyphen_project_name() -> None:
    """Project names allow single hyphens. The derivation must not
    eat them."""
    api_root = _derive_api_root(
        "http://127.0.0.1:1337/agent-mcp/mcp/my-project"
    )
    assert api_root == (
        "http://127.0.0.1:1337/agent-mcp/api/projects/my-project"
    )


def test_api_root_handles_non_default_port() -> None:
    api_root = _derive_api_root(
        "http://127.0.0.1:8080/agent-mcp/mcp/proj"
    )
    assert api_root == (
        "http://127.0.0.1:8080/agent-mcp/api/projects/proj"
    )
