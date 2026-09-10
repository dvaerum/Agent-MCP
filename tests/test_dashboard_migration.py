"""Static-grep guards for the dashboard's session-cookie auth surface.

Asserts that:

  * ``agent_mcp/dashboard/lib/api/*.ts`` no longer splices
    ``token: tokens.admin_token`` into mutation payloads — the session
    cookie is what authenticates.
  * ``ApiClient`` redirects to ``/agent-mcp/login`` on a 401, preserving
    the current path in ``?next=``.

Phase F (prancy-napping-pie): this file used to also grep-check the
Python backend (``agent_mcp/app/routers/*.py``, ``agent_mcp/router/
app.py``) for the same body-token-read pattern, plus a TODO-marker
sweep over the whole ``agent_mcp/**/*.py`` tree — all three deleted
here, not trimmed: their subject (the Python router/app auth-handler
layer) is gone, superseded by ``conexus-router``/``conexus-backend``'s
own Rust auth gates (`rest_gate.rs`/`session_gate.rs`), which carry
their own test coverage. What survives is pure dashboard-TS grep
coverage, unaffected by that deletion.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
# W6-followup F1 split the old lib/api.ts God-module into per-resource
# modules under lib/api/. The mutation payloads (token-strip guard) and
# the request core (401 redirect) now live across several of them, so
# read the whole directory as one blob.
API_TS_DIR = REPO_ROOT / "agent_mcp" / "dashboard" / "lib" / "api"


def _read_api_client() -> str:
    """Concatenate every per-resource api module (core + bundles)."""
    return "\n".join(
        p.read_text(encoding="utf-8") for p in sorted(API_TS_DIR.glob("*.ts"))
    )


# ── dashboard: token field stripped from mutation payloads ────────


_ADMIN_TOKEN_IN_BODY = re.compile(r"token:\s*tokens\.admin_token")


def test_dashboard_api_client_strips_token_field_from_payloads() -> None:
    """``apiClient.createAgent`` / ``editAgent`` / ``terminateAgent`` /
    ``restoreAgent`` / ``purgeAgent`` / ``createTask`` / ``updateTask`` /
    ``deleteTask`` no longer include ``token: tokens.admin_token`` in
    mutation bodies — the cookie carries auth now.
    """
    text = _read_api_client()
    hits = []
    for i, line in enumerate(text.splitlines(), start=1):
        if line.strip().startswith("//"):
            continue
        if _ADMIN_TOKEN_IN_BODY.search(line):
            hits.append((i, line.rstrip()))
    assert hits == [], (
        "Dashboard mutation payloads still include token: tokens.admin_token:\n  "
        + "\n  ".join(f"{n}: {ln}" for n, ln in hits)
    )


# ── ApiClient 401 redirect handler exists ─────────────────────────


def test_dashboard_api_client_has_401_redirect_handler() -> None:
    """ApiClient must redirect to /agent-mcp/login on a 401, preserving
    the current path in ``?next=`` so post-login the operator lands
    back where they started.
    """
    text = _read_api_client()
    # Loose match: must reference both ``401`` and ``/agent-mcp/login``
    # somewhere in the file plus ``next=`` for the preserved path.
    assert "401" in text, "ApiClient must inspect the 401 status code"
    assert "/agent-mcp/login" in text, (
        "ApiClient must redirect to /agent-mcp/login on 401"
    )
    assert "next=" in text or "next =" in text, (
        "ApiClient 401 redirect must preserve the current path via ?next="
    )
