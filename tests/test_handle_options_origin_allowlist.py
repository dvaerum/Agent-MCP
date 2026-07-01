"""Audit-A INFO-002 + INFO-003 (2026-06-30 follow-up): direct unit
tests for the per-route CORS fallback and the
``MCP_DASHBOARD_EXTRA_ORIGINS`` env-var hook.

Context
-------
VULN-001 removed the CORS wildcard and centralised the allowlist in
:data:`agent_mcp.app._dispatch_helpers.ALLOWED_ORIGINS`. Two
follow-ups are covered here:

* **INFO-002** — the :func:`handle_options` fallback in
  ``_dispatch_helpers.py`` runs when Starlette's ``CORSMiddleware``
  does NOT short-circuit the preflight (i.e. non-allowed origins,
  or routes that include ``OPTIONS`` in their ``methods=`` list).
  ``tests/test_cors_no_wildcard_with_credentials.py`` exercises the
  middleware path but never lands in the fallback. This file
  constructs a stub :class:`Request` and drives the fallback
  directly, so the allowlist enforcement has coverage independent
  of the middleware wiring.

* **INFO-003** — operators serving the dashboard behind a reverse
  proxy (tailnet, custom domain) need a way to extend the CORS
  allowlist without patching the source. The
  ``MCP_DASHBOARD_EXTRA_ORIGINS`` env-var accepts a comma-separated
  list of full origins; ``'*'`` and scheme-less entries are rejected
  at load time. Env-var tests use ``importlib.reload`` +
  ``monkeypatch.setenv`` so the process-wide module state is
  restored between tests.
"""

from __future__ import annotations

import asyncio
import importlib
from typing import Dict, Optional

import pytest


# ---------------------------------------------------------------------------
# INFO-002 — direct tests for the handle_options Origin allowlist
# ---------------------------------------------------------------------------


class _StubRequest:
    """Minimal :class:`starlette.requests.Request` stand-in.

    ``handle_options`` only reads ``request.headers.get('origin', '')``,
    so a bare object with a ``headers`` mapping is sufficient — no
    scope / receive machinery needed. Using a stub instead of the real
    ``Request`` keeps the test focused on the allowlist check.
    """

    def __init__(self, headers: Optional[Dict[str, str]] = None) -> None:
        self.headers = headers or {}


def test_handle_options_echoes_allowed_origin() -> None:
    """A request from an allowlisted origin gets the credentialed CORS
    reply: ``Access-Control-Allow-Origin: <origin>``,
    ``Access-Control-Allow-Credentials: true``, plus ``Vary: Origin``
    so any CDN caches the reply per-origin instead of poisoning it."""
    from agent_mcp.app._dispatch_helpers import handle_options

    request = _StubRequest(headers={"origin": "http://localhost:3847"})
    response = asyncio.run(handle_options(request))

    assert response.headers.get("access-control-allow-origin") == (
        "http://localhost:3847"
    )
    assert response.headers.get("access-control-allow-credentials") == "true"
    assert response.headers.get("vary") == "Origin"


def test_handle_options_rejects_non_allowlisted_origin() -> None:
    """A request from a non-allowlisted origin gets NO CORS headers.

    The empty-body 200 is deliberate — browsers reading the preflight
    reply see no ``Access-Control-Allow-Origin`` and refuse to fire
    the real credentialed request, which is the desired outcome."""
    from agent_mcp.app._dispatch_helpers import handle_options

    request = _StubRequest(headers={"origin": "https://evil.com"})
    response = asyncio.run(handle_options(request))

    assert "access-control-allow-origin" not in response.headers
    assert "access-control-allow-credentials" not in response.headers


def test_handle_options_rejects_missing_origin() -> None:
    """A request with no ``Origin`` header gets NO CORS headers.

    Same-origin fetches don't send Origin on GET/POST in some browser
    versions; the fallback must not stamp CORS headers on those
    either, because there's no origin to echo back."""
    from agent_mcp.app._dispatch_helpers import handle_options

    request = _StubRequest(headers={})
    response = asyncio.run(handle_options(request))

    assert "access-control-allow-origin" not in response.headers
    assert "access-control-allow-credentials" not in response.headers


def test_handle_options_rejects_suffix_bypass_attempt() -> None:
    """An origin whose prefix matches an allowlisted entry but whose
    suffix is attacker-controlled must be rejected.

    Concrete shape: ``http://localhost:3847.evil.com``. A naive
    ``startswith('http://localhost:3847')`` check would admit this;
    the exact-match ``origin in ALLOWED_ORIGINS`` check rejects it.
    Pinning the exact-match contract keeps a future refactor from
    accidentally weakening the guard."""
    from agent_mcp.app._dispatch_helpers import handle_options

    request = _StubRequest(
        headers={"origin": "http://localhost:3847.evil.com"}
    )
    response = asyncio.run(handle_options(request))

    assert "access-control-allow-origin" not in response.headers
    assert "access-control-allow-credentials" not in response.headers


# ---------------------------------------------------------------------------
# INFO-003 — MCP_DASHBOARD_EXTRA_ORIGINS env-var hook
# ---------------------------------------------------------------------------
#
# The env-var is read at import time (module-scope call to
# ``_load_extra_origins()``). Tests must reload the module after
# monkey-patching the env so the new value takes effect. The
# monkeypatch fixture undoes the ``setenv`` on teardown, and we
# reload once more without the env-var to restore the default
# ``ALLOWED_ORIGINS`` for downstream tests in the same worker.


@pytest.fixture
def reload_dispatch_helpers(monkeypatch: pytest.MonkeyPatch):
    """Yield a callable that reloads ``_dispatch_helpers`` after any
    env-var changes the caller made. On teardown, clear the env-var
    and reload once more so subsequent tests see the pristine defaults.
    """
    from agent_mcp.app import _dispatch_helpers as dh

    def _reload():
        importlib.reload(dh)
        return dh

    try:
        yield _reload
    finally:
        monkeypatch.delenv("MCP_DASHBOARD_EXTRA_ORIGINS", raising=False)
        importlib.reload(dh)


def test_extra_origins_env_var_adds_to_allowlist(
    monkeypatch: pytest.MonkeyPatch,
    reload_dispatch_helpers,
) -> None:
    """Setting ``MCP_DASHBOARD_EXTRA_ORIGINS`` adds the parsed
    origins to :data:`ALLOWED_ORIGINS` alongside the localhost
    defaults — no default eviction, additive only."""
    monkeypatch.setenv(
        "MCP_DASHBOARD_EXTRA_ORIGINS",
        "https://dashboard.example.com,https://ops.internal",
    )
    dh = reload_dispatch_helpers()

    assert "https://dashboard.example.com" in dh.ALLOWED_ORIGINS
    assert "https://ops.internal" in dh.ALLOWED_ORIGINS
    # Defaults still present — env-var extends, never replaces.
    assert "http://localhost:3847" in dh.ALLOWED_ORIGINS
    assert "http://127.0.0.1:3847" in dh.ALLOWED_ORIGINS
    assert "http://localhost:3000" in dh.ALLOWED_ORIGINS
    assert "http://localhost:3001" in dh.ALLOWED_ORIGINS


def test_extra_origins_env_var_trims_whitespace(
    monkeypatch: pytest.MonkeyPatch,
    reload_dispatch_helpers,
) -> None:
    """Whitespace around CSV entries is trimmed — the shell-friendly
    format is tolerant to line-continuation and readability spaces.
    Empty entries (from trailing commas, double commas) are dropped."""
    monkeypatch.setenv(
        "MCP_DASHBOARD_EXTRA_ORIGINS",
        " https://a.example.com , https://b.example.com ,,",
    )
    dh = reload_dispatch_helpers()

    assert "https://a.example.com" in dh.ALLOWED_ORIGINS
    assert "https://b.example.com" in dh.ALLOWED_ORIGINS
    # No empty-string entry snuck in from the trailing commas.
    assert "" not in dh.ALLOWED_ORIGINS


def test_extra_origins_env_var_rejects_wildcard(
    monkeypatch: pytest.MonkeyPatch,
    reload_dispatch_helpers,
) -> None:
    """Setting ``MCP_DASHBOARD_EXTRA_ORIGINS='*'`` raises ``ValueError``
    at import time — the "panic wildcard" foot-gun VULN-001 was built
    to prevent."""
    monkeypatch.setenv("MCP_DASHBOARD_EXTRA_ORIGINS", "*")

    with pytest.raises(ValueError, match="does not accept '\\*'"):
        reload_dispatch_helpers()


def test_extra_origins_env_var_rejects_missing_scheme(
    monkeypatch: pytest.MonkeyPatch,
    reload_dispatch_helpers,
) -> None:
    """Setting an entry with no ``http://``/``https://`` prefix raises
    ``ValueError`` — no ambiguity between ``http://evil.com`` and
    ``https://evil.com``, no misread of a bare host as a domain
    wildcard."""
    monkeypatch.setenv("MCP_DASHBOARD_EXTRA_ORIGINS", "evil.com")

    with pytest.raises(ValueError, match="must be a full origin"):
        reload_dispatch_helpers()


def test_extra_origins_env_var_rejects_mixed_valid_and_invalid(
    monkeypatch: pytest.MonkeyPatch,
    reload_dispatch_helpers,
) -> None:
    """One bad entry in a CSV list fails the entire load — no partial
    success. Better a hard error at start-up than half-configured CORS
    at runtime."""
    monkeypatch.setenv(
        "MCP_DASHBOARD_EXTRA_ORIGINS",
        "https://good.example.com,evil.com",
    )

    with pytest.raises(ValueError, match="must be a full origin"):
        reload_dispatch_helpers()


def test_extra_origins_env_var_unset_gives_defaults_only(
    monkeypatch: pytest.MonkeyPatch,
    reload_dispatch_helpers,
) -> None:
    """With the env-var absent, ``ALLOWED_ORIGINS`` is exactly the
    localhost default set — no accidental widening."""
    monkeypatch.delenv("MCP_DASHBOARD_EXTRA_ORIGINS", raising=False)
    dh = reload_dispatch_helpers()

    assert dh.ALLOWED_ORIGINS == dh._DEFAULT_ALLOWED_ORIGINS
