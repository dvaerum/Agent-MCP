# ADR-0015: SSO via OIDC + proxy-header trust

* Status: Accepted
* Date: 2026-06-18
* Plan: "prancy-napping-pie" Phase 3 Wave 3 (v5.0.70) — ephemeral plan
  file, never committed, no longer available; this ADR is the record
* Supersedes: nothing
* Builds on: ADR-0013 (operator login), ADR-0014 (REST admin API)

## Context

Phase 1 (ADR-0013) introduced router-side operator identity backed by a
local `users` table and the argon2 password store. Phase 2 added the
manager-agent role. Phase 3 layers groups, system perms (`sysadmin`),
and per-project roles on top of that. The final piece — and the goal
of Wave 3 — is letting external identity providers stand in for the
local username/password store so collaborative deployments don't have
to manage a parallel credential silo.

Two production patterns matter:

1. **External OIDC provider** (Keycloak, Authentik, Auth0, Google,
   etc.) — operators sign in at the IdP, the router accepts an
   `id_token` and binds it to a local session.
2. **Upstream proxy trust** (nginx + oauth2-proxy, traefik +
   forward-auth, tailscale-funnel + Tailnet identity, …) — the proxy
   authenticates the request and forwards the username via a header.
   The router accepts that header *only* when the request originates
   from a trusted source.

The two patterns aren't redundant — they cover different deployment
shapes. OIDC is the right choice for a deploy that needs full
authorization-code flow + token expiry + group provisioning; proxy
trust is the right choice for a deploy that has already standardised
on a forward-auth proxy and wants agent-mcp to "just trust the
header".

## Decision

agent-mcp ships **both** SSO front-ends in Phase 3 Wave 3, but with a
**single-provider-at-a-time** constraint: the OIDC config and the
proxy-header config are mutually exclusive (enforced at startup via
`SSOConfigError`, and at nix evaluation via an `assertions` entry).

Three modes:

| Mode | Activator | Auth source |
|---|---|---|
| `builtin` (default) | none | Local `users.password_hash` |
| `oidc` | `AGENT_MCP_SSO_OIDC_ISSUER` set | External OIDC IdP |
| `proxy_header` | `AGENT_MCP_SSO_PROXY_HEADER` set | Trusted upstream proxy |

### OIDC

* **Library**: Authlib's `OAuth2Session` (sync transport, called from
  a thread executor so the asyncio loop doesn't block on IdP I/O).
  Discovery via `/.well-known/openid-configuration`. PKCE is mandatory
  (`code_challenge_method=S256`). id_token decode + signature
  validation via Authlib's JWS surface against the IdP's JWKS.
* **Routes**: `GET /agent-mcp/sso/login` initiates the flow with a
  per-flow cookie binding the state + PKCE verifier to the browser;
  `GET /agent-mcp/sso/callback` validates the cookie matches the
  `state` query param, exchanges the code, decodes claims, finds-or-
  creates the local user, and mints the standard session cookie.
* **User matching algorithm**: match by `email` claim → existing
  `users.email`. On miss, create a new user with sanitised
  `preferred_username` (lowercase, dashes only), `email = email`,
  `password_hash = NULL` (the user has no password — the IdP owns the
  credential). The new user lands at `is_sysadmin = FALSE`; the
  sysadmin promotes later via the dashboard.
* **Group-claim mapping** (option A — **Mapped**, with wildcard JIT
  escape): config maps `{oidc_group: amcp_group}`. Unmapped claims are
  silently ignored. A special `"*"` key turns on the wildcard JIT
  escape — every unmatched group claim auto-creates a sanitized
  agent-mcp group and the user is added.
* **Single provider at a time**: option A from the locked grilling.
  Multi-provider OIDC would require per-provider redirect URIs +
  per-provider button labels + per-provider group-mapping namespaces;
  none of those are in scope for v5.0.70 and adding them would muddy
  the operator-visible config UI.

### Proxy-header trust

* **Activator**: `AGENT_MCP_SSO_PROXY_HEADER=<header-name>` (the
  header to consult; nix submodule defaults to `Remote-User`).
* **Trusted source enforcement**: the router only honours the
  trusted header when `request.remote` (the transport peer IP) is in
  the configured `trusted_ips` set. The default set is `127.0.0.1`
  + `::1` (the common deploy pattern: upstream proxy on the same
  host). `X-Forwarded-For` is **not** consulted for trust
  decisions — that header is operator-supplied, and the IP check IS
  the gatekeeper that prevents header spoofing.
* **JIT user creation**: same algorithm as OIDC's, with one extra
  knob: `AGENT_MCP_SSO_PROXY_DEFAULT_SYSADMIN` (default `false`).
  When set true, every JIT-created user via the proxy-header path
  gets `is_sysadmin = TRUE`. Only safe when the upstream proxy is
  the sole, well-trusted auth boundary.

### Dashboard

The System overview gains a new **SSO** tab next to Users / Groups
(matching the placement of the Wave 1b CRUD UIs). The tab fetches
`GET /agent-mcp/api/router/sso/config` and renders the active mode
plus the operator-visible knobs (the OIDC client secret is reported
only as a presence boolean — the value never crosses the wire).
Non-sysadmin operators see a "Sysadmin only" explanatory card
instead of the config table.

**Writes are deliberately not shipped in this PR**: the SSO config
travels via env vars (the home-manager / nix module owns the
canonical source). Letting the dashboard mutate them would either
require writing back to the host config (out of scope) or
maintaining a parallel config file the nix module wouldn't know
about (drift hazard). Tracked under the ADR-0015 follow-up.

### Schema

A new migration (`0003_sso_users.py`) relaxes `users.password_hash`
from `NOT NULL` to nullable so SSO-only JIT rows can exist without a
local password. The SQLite "create new + copy + swap" dance preserves
every existing row's hash verbatim.

## Consequences

### Positive

* Operators get OIDC sign-in with one nix-module option block; the
  PKCE flow is wired correctly and the id_token signature is
  validated against the IdP's JWKS — defaults are safe.
* Operators who already run a forward-auth proxy can drop agent-mcp
  in behind it with two env vars (`AGENT_MCP_SSO_PROXY_HEADER` +
  `AGENT_MCP_SSO_PROXY_TRUSTED_IPS`).
* Group-claim mapping lets a sysadmin pre-create groups in agent-mcp
  and bind IdP roles to them without writing custom sync code.
* The wildcard JIT escape gives a one-line "mirror every IdP group"
  option for deploys that don't need to curate the local namespace.
* The mutex prevents the "which mode wins?" footgun by construction.

### Negative / trade-offs

* **No SAML.** Out of scope. Operators on SAML-only IdPs are unserved
  by this PR; they can use the proxy-header path with a SAML-aware
  upstream proxy.
* **One OIDC issuer at a time.** Multi-provider deploys must pick the
  one IdP all operators federate through.
* **No SCIM provisioning.** Users are created lazily on first login,
  not pre-provisioned. Sysadmins who need a populated user list before
  first SSO logins land must seed via the CLI (Phase 1's
  `agent-mcp router create-operator`).
* **No 2FA at the router.** The IdP is expected to enforce it.
* **No config writes from the dashboard.** Sysadmins can read the
  current config but must edit the host config (and restart the
  router) to change it. Acceptable today because nix is the
  canonical config source; revisit if a non-nix deploy story
  emerges.

### Risks

* **Proxy-header trust footgun.** If an operator widens `trusted_ips`
  beyond localhost without putting a firewall in front of the router,
  an attacker reaching the router's TCP port directly can spoof the
  header. The defaults are localhost-only and the docstrings + the
  dashboard explicitly warn about this; the assertion catches the
  OIDC + proxy mutex but not a wide trusted-IP list.
* **OIDC discovery cache.** The current implementation re-fetches the
  discovery document on every authorize / callback round trip. Cheap
  in absolute terms (one HTTPS GET per login) but a per-process cache
  would be a nice optimisation later.
* **JIT-created user with NULL `password_hash`.** Existing tools that
  assume `password_hash` is always set need updating — the migration
  added the new nullable column but cannot statically prove no caller
  reads it without a null check. We audited the obvious sites; a
  future grep sweep is worth doing if odd login behaviour surfaces.

## Follow-ups

* **Dashboard SSO writes**: a NixOS-friendly config-write surface
  that integrates with the home-manager module (e.g. a small
  `agent-mcp-router-config.json` overlay that the module merges with
  declared values).
* **SCIM provisioning**: pre-seed users + groups from the IdP's SCIM
  endpoint so a fresh deploy comes up with the right namespace.
* **Multi-provider OIDC**: per-provider redirect URI + login-page
  selector if the operator-visible value materialises.
* **2FA / WebAuthn at the router**: only worth shipping if an
  operator has a use case that the IdP can't satisfy.

## Verification

* `tests/router/test_sso_oidc.py`, `…test_sso_proxy_header.py`,
  `…test_sso_mutex.py`, `…test_admin_sso_config_api.py` cover the
  route shapes, JIT user creation, group mapping (explicit +
  wildcard), trusted-source enforcement, mutex assertion, and the
  dashboard config endpoint's sysadmin gate.
* The `nix flake check` VM smoke (multi-tenant test) exercises the
  module's env-var wiring; SSO env vars are appended via
  `lib.optionals` so the unconfigured-SSO path stays byte-identical
  to v5.0.69.
