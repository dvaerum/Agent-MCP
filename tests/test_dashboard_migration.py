"""Static-grep guards for the Phase 1 PR D dashboard auth migration.

Asserts that:

  * Mutation handlers in the per-resource router modules under
    ``agent_mcp/app/routers/`` no longer read ``body['token']`` /
    ``data.get('token')`` to authenticate (auth moved to the
    ``require_operator_session`` dependency). Reads in the dep
    itself are allow-listed.
  * The dashboard's ``agent_mcp/dashboard/lib/api.ts`` no longer
    splices ``token: tokens.admin_token`` into mutation payloads —
    the session cookie is what authenticates.
  * No leftover ``TODO(prancy-napping-pie PR D)`` markers remain in
    the source tree (PR D's sweep is complete).

Wave 8 PR 2 rewire: the single ``agent_mcp/app/routes.py`` file was
deleted; the same invariant must now hold across every per-resource
``agent_mcp/app/routers/*.py`` module the handlers moved to. The test
scans the whole subpackage instead of a single file.

These tests are intentionally grep-style + structural. The wire
contract is exercised by ``tests/test_dashboard_session_auth.py``.
"""

from __future__ import annotations

import re
from pathlib import Path



REPO_ROOT = Path(__file__).resolve().parent.parent
ROUTERS_DIR = REPO_ROOT / "agent_mcp" / "app" / "routers"
ROUTER_APP_FILE = REPO_ROOT / "agent_mcp" / "router" / "app.py"
API_TS_FILE = REPO_ROOT / "agent_mcp" / "dashboard" / "lib" / "api.ts"


# ── helpers ────────────────────────────────────────────────────────


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# Patterns that indicate a handler is reading a token field out of an
# in-memory body / form dict to authenticate. Matches:
#   data.get('token')          data.get("token")
#   data['token']              data["token"]
#   body.get('token')          form.get('token')
#   body['token']              form['token']
_TOKEN_BODY_PATTERNS = [
    re.compile(r"\bdata\.get\(\s*['\"]token['\"]"),
    re.compile(r"\bdata\[\s*['\"]token['\"]\s*\]"),
    re.compile(r"\bbody\.get\(\s*['\"]token['\"]"),
    re.compile(r"\bbody\[\s*['\"]token['\"]\s*\]"),
    re.compile(r"\bform\.get\(\s*['\"]token['\"]"),
    re.compile(r"\bform\[\s*['\"]token['\"]\s*\]"),
]


def _scan(text: str, patterns: list[re.Pattern]) -> list[tuple[int, str]]:
    """Return (line_number, matched_line) for every pattern hit."""
    hits: list[tuple[int, str]] = []
    for i, line in enumerate(text.splitlines(), start=1):
        # Skip comments + obvious docstring lines: the regex catches
        # them, but they're not real code paths.
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        for pat in patterns:
            if pat.search(line):
                hits.append((i, line.rstrip()))
                break
    return hits


# ── backend: token must not be read inside mutation handlers ──────


def test_routes_py_does_not_read_token_from_body_in_mutation_handlers() -> None:
    """Per-resource router handlers under ``agent_mcp/app/routers/``
    must not authenticate via ``data.get('token')`` / ``body['token']``.

    PR D moves auth into the ``require_operator_session`` FastAPI
    dependency. Each per-handler ``admin_token = data.get('token')``
    + ``verify_token(admin_token, ...)`` ladder is replaced.

    The dep itself + the legacy ``verify_token`` helper are allowed —
    they're imports of the auth surface, not handler-local reads.

    Wave 8 PR 2 rewire: the legacy single-file ``agent_mcp/app/routes.py``
    is gone; the same invariant is now enforced across every per-resource
    router module the handlers moved to.
    """
    all_hits: list[tuple[Path, int, str]] = []
    for path in sorted(ROUTERS_DIR.glob("*.py")):
        text = _read(path)
        for n, ln in _scan(text, _TOKEN_BODY_PATTERNS):
            all_hits.append((path, n, ln))
    assert all_hits == [], (
        "Found legacy body-token reads in agent_mcp/app/routers/:\n  "
        + "\n  ".join(
            f"{p.relative_to(REPO_ROOT)}:{n}: {ln}" for p, n, ln in all_hits
        )
    )


def test_router_app_does_not_read_token_from_body_in_mutation_handlers() -> None:
    """``agent_mcp/router/app.py`` router-level handlers must not
    authenticate via body['token'] / form['token'] either.

    The router uses session-cookie auth via the
    ``require_operator_session_middleware``. Form-based handlers
    (e.g. __create) no longer need to extract a token from the form.
    """
    text = _read(ROUTER_APP_FILE)
    hits = _scan(text, _TOKEN_BODY_PATTERNS)
    assert hits == [], (
        "Found legacy body-token reads in agent_mcp/router/app.py:\n  "
        + "\n  ".join(f"{n}: {ln}" for n, ln in hits)
    )


# ── dashboard: token field stripped from mutation payloads ────────


_ADMIN_TOKEN_IN_BODY = re.compile(r"token:\s*tokens\.admin_token")


def test_dashboard_api_client_strips_token_field_from_payloads() -> None:
    """``apiClient.createAgent`` / ``editAgent`` / ``terminateAgent`` /
    ``restoreAgent`` / ``purgeAgent`` / ``createTask`` / ``updateTask`` /
    ``deleteTask`` no longer include ``token: tokens.admin_token`` in
    mutation bodies — the cookie carries auth now.
    """
    text = _read(API_TS_FILE)
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


# ── TODO sweep — PR D markers all addressed ───────────────────────


def test_no_lingering_pr_d_todo_markers() -> None:
    """``grep -rn TODO(prancy-napping-pie PR D) agent_mcp/`` → empty.

    PR D promised to address every such marker. If new code adds one
    after PR D ships, it should use a different tracking marker.
    """
    needle = "TODO(prancy-napping-pie PR D)"
    hits: list[tuple[Path, int, str]] = []
    for path in REPO_ROOT.glob("agent_mcp/**/*.py"):
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if needle in line:
                hits.append((path, i, line.rstrip()))
    assert hits == [], (
        "Found lingering PR D TODO markers:\n  "
        + "\n  ".join(f"{p}:{n}: {ln}" for p, n, ln in hits)
    )


# ── ApiClient 401 redirect handler exists ─────────────────────────


def test_dashboard_api_client_has_401_redirect_handler() -> None:
    """ApiClient must redirect to /agent-mcp/login on a 401, preserving
    the current path in ``?next=`` so post-login the operator lands
    back where they started.
    """
    text = _read(API_TS_FILE)
    # Loose match: must reference both ``401`` and ``/agent-mcp/login``
    # somewhere in the file plus ``next=`` for the preserved path.
    assert "401" in text, "ApiClient must inspect the 401 status code"
    assert "/agent-mcp/login" in text, (
        "ApiClient must redirect to /agent-mcp/login on 401"
    )
    assert "next=" in text or "next =" in text, (
        "ApiClient 401 redirect must preserve the current path via ?next="
    )
