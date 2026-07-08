"""VULN-001 (security audit 2026-06-29): CORS wildcard with credentials.

The dashboard FastAPI app shipped with
``allow_origins=[..., '*']`` AND ``allow_credentials=True``, which is
the textbook browser-exploitable misconfiguration:

  * Any attacker-controlled origin satisfies the preflight check, so
    a logged-in operator visiting evil.example.com triggers a
    credentialed cross-origin request against agent-mcp running on
    their machine.
  * The CSRF-style attack reads the response (cookies/bearers are
    included), giving the attacker read+write access to whatever the
    operator can do.

This regression test pins three invariants:

  1. The configured CORSMiddleware allowlist does NOT contain ``'*'``
     (introspect ``app.user_middleware`` so future copy-paste
     re-introductions are caught at unit-test time, not at audit).

  2. A preflight from a non-allowlisted origin
     (``https://evil.example.com``) must not be granted credentials —
     specifically, the response must not echo
     ``Access-Control-Allow-Credentials: true`` paired with an
     attacker-permissive ``Access-Control-Allow-Origin``.

  3. A preflight from an allowlisted origin
     (``http://localhost:3847``) still works — the credentialed
     dashboard flow is unbroken by the fix.
"""

from __future__ import annotations

from starlette.middleware.cors import CORSMiddleware


def test_cors_middleware_does_not_allow_wildcard_origin(app) -> None:
    """``app.user_middleware`` must not list ``'*'`` in CORS allow_origins."""
    cors_middlewares = [
        m for m in app.user_middleware if m.cls is CORSMiddleware
    ]
    assert cors_middlewares, (
        "CORSMiddleware not found on app.user_middleware — the security "
        "test would silently pass on a misconfigured app."
    )

    for m in cors_middlewares:
        allow_origins = m.kwargs.get("allow_origins", [])
        assert "*" not in allow_origins, (
            "VULN-001: CORSMiddleware allow_origins contains '*'. "
            "Paired with allow_credentials=True (also configured), this "
            "is browser-exploitable as CSRF. Remove the wildcard and "
            "list only trusted origins."
        )


def test_preflight_from_evil_origin_does_not_grant_credentials(client) -> None:
    """A preflight from evil.example.com must not be granted credentials.

    The attack shape: an attacker origin gets a green preflight with
    ``Access-Control-Allow-Credentials: true``, then issues a real
    credentialed cross-site request and reads the response. This test
    asserts the preflight reply does not enable that flow.
    """
    response = client.options(
        "/api/status",
        headers={
            "Origin": "https://evil.example.com",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "content-type",
        },
    )

    # The attacker origin must not be echoed AND credentials granted.
    # Either: no Allow-Origin header at all (browser blocks), or
    # Allow-Origin set to something other than the attacker (browser
    # blocks). Critically, the combination
    #   Access-Control-Allow-Origin: https://evil.example.com
    #   Access-Control-Allow-Credentials: true
    # must never appear together.
    allow_origin = response.headers.get("access-control-allow-origin", "")
    allow_credentials = response.headers.get(
        "access-control-allow-credentials", ""
    ).lower()

    if allow_credentials == "true":
        assert allow_origin not in ("*", "https://evil.example.com"), (
            "VULN-001: preflight granted credentials to an arbitrary "
            f"origin. Allow-Origin={allow_origin!r}, "
            f"Allow-Credentials={allow_credentials!r}. "
            "This is the exploit shape — never ship."
        )

    # Belt-and-suspenders: wildcard origin must never appear, regardless
    # of the credentials header, because the CORS spec forbids it on
    # any response served to a credentialed request and our app stamps
    # credentials globally via allow_credentials=True.
    assert allow_origin != "*", (
        "VULN-001: preflight returned wildcard Allow-Origin. The app "
        "globally enables allow_credentials=True; the wildcard reply "
        "tells the browser to permit the credentialed request from any "
        "origin."
    )


def test_preflight_from_localhost_not_allowed_by_default(client) -> None:
    """SEC-1 (2026-07): the localhost dev origins are NOT in the
    production default allowlist.

    Pre-SEC-1 the default allowlist shipped
    ``http://localhost:3000/3001/3847`` with ``allow_credentials=True``
    — a latent CSRF surface on any reachable deployment. The default is
    now empty; a preflight from ``http://localhost:3847`` must NOT be
    echoed with credentials unless an operator opted it in via
    ``MCP_DASHBOARD_EXTRA_ORIGINS`` (covered in
    ``test_handle_options_origin_allowlist.py``).
    """
    response = client.options(
        "/api/status",
        headers={
            "Origin": "http://localhost:3847",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "content-type",
        },
    )

    allow_origin = response.headers.get("access-control-allow-origin", "")
    allow_credentials = response.headers.get(
        "access-control-allow-credentials", ""
    ).lower()

    # Never the exploit shape: an echoed localhost origin + credentials.
    if allow_credentials == "true":
        assert allow_origin != "http://localhost:3847", (
            "SEC-1: localhost:3847 was granted credentials by default. "
            "The production default allowlist must be empty; dev origins "
            "opt in via MCP_DASHBOARD_EXTRA_ORIGINS."
        )
