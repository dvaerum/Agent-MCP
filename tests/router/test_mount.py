"""ADR-0020: per-request external mount / prefix derivation.

The router must serve the same routes under `/agent-mcp` (tailnet) AND at
the host root (Traefik at mm.best.aau.dk), deriving the client-facing
prefix + origin per request. `canonical_path` is security-load-bearing:
the auth gate keys off it, so a root-aliased path is gated identically to
its `/agent-mcp` twin.
"""

from __future__ import annotations

from unittest import mock

from aiohttp.test_utils import make_mocked_request

from agent_mcp.router import mount


def _req(path: str, *, remote: str | None = None, headers=None):
    """Mocked request with a chosen peer IP. remote=None → UDS/loopback
    (trusted); a dotted-quad → untrusted direct client."""
    transport = mock.Mock()
    peername = (remote, 40000) if remote else None
    transport.get_extra_info = lambda key, default=None: (
        peername if key == "peername" else default
    )
    return make_mocked_request(
        "GET", path, headers=headers or {}, transport=transport,
    )


# ── canonical_path (security-load-bearing) ──────────────────────────


def test_canonical_path_tailnet_unchanged():
    assert mount.canonical_path(_req("/agent-mcp/api/router/health")) == \
        "/agent-mcp/api/router/health"
    assert mount.canonical_path(_req("/agent-mcp/")) == "/agent-mcp/"
    assert mount.canonical_path(_req("/agent-mcp")) == "/agent-mcp"


def test_canonical_path_root_normalised_to_mount():
    # A root-aliased request MUST canonicalise to its /agent-mcp twin so
    # the auth gate treats it identically (else root bypasses auth).
    assert mount.canonical_path(_req("/api/router/projects")) == \
        "/agent-mcp/api/router/projects"
    assert mount.canonical_path(_req("/app/proj/")) == "/agent-mcp/app/proj/"
    assert mount.canonical_path(_req("/")) == "/agent-mcp/"


# ── external_prefix ─────────────────────────────────────────────────


def test_external_prefix_inferred_from_arrival():
    # Tailnet: arrived under /agent-mcp → client sees /agent-mcp.
    assert mount.external_prefix(_req("/agent-mcp/app/x/")) == "/agent-mcp"
    # Traefik root: arrived at root → client sees no prefix.
    assert mount.external_prefix(_req("/app/x/")) == ""


def test_external_prefix_trusted_forwarded_header_wins():
    # A trusted proxy that strips + declares the prefix is honoured.
    r = _req("/app/x/", remote=None,  # loopback → trusted
             headers={"X-Forwarded-Prefix": "/agent-mcp"})
    assert mount.external_prefix(r) == "/agent-mcp"
    # Explicit empty prefix (Traefik at root announcing it) → root.
    r2 = _req("/agent-mcp/app/x/", remote=None,
              headers={"X-Forwarded-Prefix": "/"})
    assert mount.external_prefix(r2) == ""


def test_external_prefix_untrusted_forwarded_header_ignored():
    # A DIRECT (untrusted) client cannot forge the prefix — falls back to
    # path inference.
    r = _req("/app/x/", remote="8.8.8.8",
             headers={"X-Forwarded-Prefix": "/agent-mcp"})
    assert mount.external_prefix(r) == ""


# ── external_origin / external_url ──────────────────────────────────


def test_external_origin_trusts_forwarded_only_from_trusted_peer():
    trusted = _req("/app/", remote=None, headers={
        "X-Forwarded-Proto": "https", "X-Forwarded-Host": "mm.best.aau.dk",
    })
    assert mount.external_origin(trusted) == "https://mm.best.aau.dk"
    untrusted = _req("/app/", remote="8.8.8.8", headers={
        "X-Forwarded-Proto": "https", "X-Forwarded-Host": "evil.example",
    })
    assert "evil.example" not in mount.external_origin(untrusted)


def test_external_url_both_front_doors():
    # Tailnet front door → prefixed absolute URL.
    tailnet = _req("/agent-mcp/mcp/proj", remote=None, headers={
        "X-Forwarded-Proto": "https",
        "X-Forwarded-Host": "host.ts.net",
    })
    assert mount.external_url(tailnet, "/mcp/proj") == \
        "https://host.ts.net/agent-mcp/mcp/proj"
    # Traefik root front door → root absolute URL, same process.
    root = _req("/mcp/proj", remote=None, headers={
        "X-Forwarded-Proto": "https",
        "X-Forwarded-Host": "mm.best.aau.dk",
    })
    assert mount.external_url(root, "/mcp/proj") == \
        "https://mm.best.aau.dk/mcp/proj"
