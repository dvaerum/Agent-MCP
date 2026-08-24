# ADR 0025: Forwarding-tier identity is excluded from confirmed-operator-tier by design

**Status**: Accepted, 2026-08-24. Codifies behaviour the code already
has — this ADR ships **no** behaviour change. Its purpose is to give an
existing invariant a name, a rationale that outlives the function
docstring holding it, and a regression test broad enough to catch the
near-miss that prompted it.
**Date**: 2026-08-24
**Builds on**: ADR-0015 (SSO — the `AGENT_MCP_SSO_PROXY_DEFAULT_SYSADMIN`
precedent this ADR's escape hatch copies), ADR-0017 (no content-based
secret redaction — the reason token disclosure is gated by an
*authorization* predicate rather than by scanning payloads).
**Depends on**: `agent_mcp/app/forwarding_header.py`'s module docstring,
specifically its "Why not just include a nonce" section — the scope of
that section's threat argument is the load-bearing fact below.

## Context

Two mechanisms meet at one predicate, and it is easy to read the first
as having settled the second.

**The first mechanism** is the signed forwarding header
(`agent_mcp/app/forwarding_header.py`). The always-on router terminates
the operator's cookie session, resolves that operator's *real*
per-project role from `project_membership`, and forwards the request to
the per-project backend over a Unix-domain socket carrying
`<operator_id>.<role>.<expiry>.<HMAC-hex>`. The role is inside the HMAC
input, so it cannot be tampered with in flight; an unknown role value is
a hard reject; a stale or far-future expiry is rejected by the 30-second
replay window. Finding B (Phase 5) added the signed role precisely to
close SEC-1, where the backend hard-coded `project_role="operator"` for
every verified header and handed a viewer-tier operator the full
operator capability bundle.

**The second mechanism** is the confirmed-operator-tier predicate
(`agent_mcp/core/operator_tier.py`, adapted onto the REST door by
`agent_mcp/app/routers/composition.py::is_confirmed_operator_tier`). It
answers a narrower question than "what may this caller do": *may this
caller receive plaintext agent bearer tokens?* It gates
`GET /api/tokens` and the bearer field of `GET /api/all-data`, and it
sits behind the capability gate as a defense-in-depth layer — a caller
who passes the capability check but whose operator tier is unverifiable
still gets secrets withheld.

The tempting inference is: *the forwarding role is unforgeable, so a
forwarding caller whose signed role is `operator` is just as
operator-tier as a cookie operator, and the predicate should say so.*
Since Phase 5 (Finding D) moved the signed role off a task-local
`ContextVar` and onto `RestPrincipal`, acting on that inference is now
literally a **one-line change** — thread `project_role` and `sysadmin`
into the forwarding branch's call, the way the `session` branch already
does. It looks like finishing an incomplete refactor. Phase 5 caught
exactly this near-miss in review and left a scope note; the note is one
paragraph in one function docstring, unlinked to the file whose threat
model it depends on, and the only test pinning the behaviour asserted
the **all-defaults** case (`project_role=None, sysadmin=False`) — which
the one-line change leaves passing.

The inference is wrong for a specific reason, and the reason is a scope
boundary in the forwarding header's own docstring. That docstring
declines a nonce + replay cache because "the router and backend are
co-located (same machine, Unix-domain socket), the request lifetime is
< 1 s, and a stolen header from a co-located process means the attacker
already owns the host." That argument bounds **replay risk**: it says a
captured header is not worth defending against separately, because
capturing one already implies host compromise. It says nothing at all
about whether the router *process* — a distinct process, with its own
codebase, its own attack surface (public HTTP, OIDC callbacks, SSO
proxy headers, session cookies), and its own bugs — should inherit the
same disclosure trust as a browser session that authenticated directly
against the backend it is reading secrets from. Those are different
questions, and only the first one has been answered.

## Decision

**The forwarding door never counts as confirmed-operator-tier for
secrets disclosure, regardless of the role it signs.**

`composition.is_confirmed_operator_tier` passes only `kind` to the
shared predicate for `kind == "forwarding"`. Because `"forwarding"` is
not an operator-bearer kind and the other two inputs default to their
least-privilege values, the predicate returns `False` for *every*
forwarding principal — including one carrying both
`project_role="operator"` and `sysadmin=True`. The signed role is still
carried on the principal and still consumed by
`RestPrincipal.route_role()`, so a forwarding caller gets their real
capability bundle for ordinary operations; the exclusion is at the
disclosure predicate, not by discarding the role.

This is a deliberate **process-boundary / defense-in-depth** choice, not
a gap:

* A valid identity assertion crossing a security-domain boundary earns
  **authorization**, not automatically the issuer's own **disclosure
  tier**. This is the same shape as AWS STS assumed-role scoping (the
  assumed role's permissions are the session's ceiling; holding the
  trust relationship does not confer the trusting account's own access)
  and Kubernetes service-account token scoping (a projected,
  audience-bound token authenticates the workload for its audience — it
  is not a stand-in for the API server's own credentials).
* A router-side bug or compromise that does **not** also compromise the
  backend should not automatically yield plaintext agent bearer tokens.
  Those tokens are lateral-movement fuel: each one authenticates as a
  live agent against the MCP surface. Withholding them means a router
  compromise costs the attacker the operations the compromised session
  could perform, not every agent credential in the project.
* The asymmetry with the cookie door is not an inconsistency. A cookie
  `session` principal's role was resolved **by this backend**, against
  router.db, in `deps._authorize_session_for_project`. A forwarding
  principal's role was resolved by another process and asserted to this
  one. Both assertions are trustworthy for authorization; only the
  first is a fact this backend established itself, which is the bar
  this predicate sets for handing out credentials.

The cost is explicit and accepted: a dashboard operator reaching the
backend through the router sees `[redacted]` where a direct
operator-bearer caller (agent CLI, admin script) sees the token. Agent
bearer tokens are shown once at mint time; the operational answer to "I
need this agent's token" is to mint a fresh credential for that
identity, not to read the existing one back out of a dashboard payload.

### Escape hatch — named here, deliberately NOT implemented

If a real deployment ever needs this widened, the sanctioned path is an
explicit, named, **off-by-default** config flag —
`AGENT_MCP_FORWARDING_CONFIRMS_OPERATOR_TIER`, default `false` — read
alongside the other trust settings and threaded into the adapter as an
input, leaving the shared predicate untouched.

That shape matches the precedent `AGENT_MCP_SSO_PROXY_DEFAULT_SYSADMIN`
already set (ADR-0015) for an equally consequential trust decision: a
widening an operator may knowingly opt into, visible in their
configuration, greppable, and documentable — rather than a silent
default. What is **not** sanctioned is a one-line edit to the predicate
call, because that removes the decision from the deployment's
configuration and puts it back in the code where nobody reviewing a
future refactor will recognise it as a policy change.

**This ADR does not implement the flag.** It names it so that a real
future need does not have to re-derive the decision from scratch, and so
that "add the flag" is visibly the cheaper path than "edit the
predicate".

## Consequences

### Positive

* The invariant has a name and a citable home. The reasoning no longer
  depends on a reader happening to open the right function docstring
  *and* connect it to `forwarding_header.py`'s scope boundary.
* `tests/test_arch_forwarding_never_confirmed_tier.py` pins all six
  `project_role` × `sysadmin` combinations, so the one-line change fails
  loudly at test time instead of passing review as a completed
  refactor. Verified by applying that exact edit as a throwaway and
  watching 5 of the new assertions go red — while the pre-existing
  `test_wave12_pra_operator_tier.py` suite stayed fully green, which is
  precisely the coverage gap this ADR's test closes.
* A future widening now has a designed shape (a config flag) rather than
  an invitation to edit the predicate.

### Negative / trade-offs

* Dashboard operators genuinely cannot read agent bearer tokens through
  the forwarding path. This is the accepted cost above, not an
  unintended consequence — but it *is* a real usability gap, and if it
  becomes a repeated operator complaint, that is the signal to
  implement the flag rather than to erode the predicate.
* Two doors that both represent "an operator" get different disclosure
  answers, which reads as inconsistent until the process-boundary
  argument is understood. That is exactly why this document exists; the
  docstrings on both sides now point at it.
* Pinning behaviour with a test makes an intentional future change
  slightly more expensive (the test must be updated deliberately). That
  friction is the point.

## Alternatives considered

* **Thread `project_role`/`sysadmin` through the forwarding branch** —
  the one-line change. Rejected: it silently widens who receives
  plaintext agent bearers, on the argument that "the role is signed", an
  argument that establishes replay resistance rather than disclosure
  parity. If it is ever wanted, the flag above makes it an operator's
  explicit decision rather than a refactor's side effect.
* **Implement the flag now, defaulted off** — rejected as speculative
  generality. No deployment has asked for it; an unused config surface
  is a maintenance and mis-set-in-production liability, and naming the
  design in this ADR captures everything a future implementer needs
  without shipping dead code.
* **Add a nonce / one-shot replay cache to the forwarding header** —
  orthogonal, and already declined on its own merits in
  `forwarding_header.py`. It would strengthen replay resistance, which
  is not the property in question here; a fully replay-proof header
  still would not make the router process's assertion equivalent to a
  direct backend authentication.
* **Leave it as a docstring note** — the status quo this ADR replaces.
  It survived Phase 5 only because a reviewer happened to catch the
  near-miss. The Finding-C conclusion in
  `docs/proposals/security-authz-architecture-hardening.md` applies:
  classes closed structurally stop recurring, classes closed by
  discipline keep recurring.

## Links

* `agent_mcp/app/routers/composition.py::is_confirmed_operator_tier` —
  the adapter; its "DELIBERATE SCOPE (Finding D, Phase 5)" paragraph
  points back here.
* `agent_mcp/app/forwarding_header.py` — the signed header's format,
  replay window, and the "Why not just include a nonce" section whose
  scope this ADR delimits; its docstring points back here.
* `agent_mcp/core/operator_tier.py` — the shared predicate, and why the
  REST and MCP surfaces feed one implementation instead of two.
* `agent_mcp/app/rest_principal.py` — `RestPrincipal`, and
  `route_role()`, which still returns the forwarding caller's REAL
  signed role for authorization.
* `tests/test_arch_forwarding_never_confirmed_tier.py` — the regression
  pin for this decision.
* `tests/test_wave12_pra_operator_tier.py` — the Wave 12 PR A tests,
  including the pre-existing all-defaults forwarding assertion this one
  broadens (left unchanged).
* [ADR-0015](0015-sso-oidc-and-proxy-header.md) — the
  `AGENT_MCP_SSO_PROXY_DEFAULT_SYSADMIN` precedent for an
  off-by-default trust-widening flag.
* [ADR-0017](0017-no-content-secret-redaction.md) — why secret
  withholding is an authorization decision, not content scanning.
