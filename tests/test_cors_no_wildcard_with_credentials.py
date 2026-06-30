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


def test_preflight_from_allowed_origin_still_works(client) -> None:
    """Preflight from a configured localhost origin must keep working.

    Regression guard: the fix removes ``'*'`` from the allowlist, but
    must NOT regress the dashboard's own credentialed flow served
    from http://localhost:3847.
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

    # The dashboard expects an echoed, specific Allow-Origin (NOT '*'
    # — wildcard would itself disable credentials per the spec) and
    # Allow-Credentials: true so the session cookie rides along.
    assert allow_origin == "http://localhost:3847", (
        f"Expected Allow-Origin: http://localhost:3847, got {allow_origin!r}. "
        "The dashboard's credentialed flow depends on the origin being "
        "echoed back; a wildcard would also break the flow because "
        "browsers refuse to send credentials when ACAO is '*'."
    )
    assert allow_credentials == "true", (
        "Expected Allow-Credentials: true for the allowlisted origin. "
        "Without it, the dashboard's session cookie is dropped on the "
        "preflight floor and subsequent requests are unauthenticated."
    )
