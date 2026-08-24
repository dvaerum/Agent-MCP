"""N3 Tier 1 subtractions (security-arch-hardening-consolidated.md
Phase 1): of N3's five duplicated request-classification pairs, this
does only the ONE that is a pure copy with no local knowledge and no
trust-boundary implications. N3's ``sso.is_trusted_proxy_source`` vs
``rate_limit`` disagreement was investigated and deliberately NOT
"fixed" here -- see the plan's round log / this PR's description:
``rate_limit``'s default trusted-proxy set includes loopback
unconditionally, while ``sso.is_trusted_proxy_source`` was
deliberately built with NO implicit trust (only the operator-
configured ``AGENT_MCP_SSO_PROXY_TRUSTED_IPS`` allowlist) --
delegating to the "canonical" helper would have been a real security
regression (any loopback-originating request could forge the trusted
header), caught by the existing
``test_trusted_header_from_untrusted_source_rejected`` regression test.
That's a genuine architectural question (should SSO's proxy-header
trust model gain a UDS-fronted carve-out, and if so, on whose terms?),
not a mechanical dedup -- flagged back to the operator rather than
force-fit.

RESOLVED since, on the operator's terms: the two now share ONE
``rate_limit.is_trusted_peer``, but the allowlist stays a per-caller
PARAMETER (so sso still gets no implicit loopback trust), and the UDS
carve-out is a kernel-verified ``SO_PEERCRED`` UID check rather than an
"empty ``request.remote`` implies trust" heuristic. See
``tests/router/test_so_peercred_peer_trust.py``.
"""

from __future__ import annotations


def test_mutation_methods_is_the_same_object_everywhere() -> None:
    """app/deps.py._MUTATION_METHODS was a verbatim copy of
    auth_middleware._MUTATION_METHODS, kept in sync only by a comment.
    Must be an import, not a redeclaration."""
    from agent_mcp.app.deps import _MUTATION_METHODS as backend_constant
    from agent_mcp.router.auth_middleware import (
        _MUTATION_METHODS as router_constant,
    )

    assert backend_constant is router_constant
