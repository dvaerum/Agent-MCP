# ADR-0013: Operator login — dashboard requires authentication

## Status

Accepted (2026-06-18). Supersedes the "dashboard admin-by-design"
assumption baked into ADR-0009 (dashboard owns the ops surface; whoever
can reach the URL is implicitly admin).

## Context

Pre-Phase-1 agent-mcp authenticated two distinct populations with one
secret. The router's `admin_token` was simultaneously:

- The dashboard operator's bearer — anyone who could reach the
  `/agent-mcp/` URL was implicitly admin, per the "dashboard admin
  surface" decision in ADR-0009. Securing the URL was the deployer's
  job (nginx basic-auth, tailscale ACL, etc.).
- Every admin-tier agent's MCP bearer — spawned agents whose name
  starts with `admin` inherited the admin token at spawn time (see
  `admin_tools.py:396-401`).

This conflated two orthogonal concerns: *who is the human operating
the project* and *what can an agent do*. Two consequences:

1. Operators on shared networks (corporate VPN, tailnet without ACLs)
   were one URL leak away from total compromise — the dashboard never
   asked for credentials, so a stolen URL was a stolen admin token.
2. Multi-operator workflows were impossible. Two humans sharing a
   project had to share the same token; rotating it locked everyone
   out simultaneously.

## Decision

Operators must log in to the dashboard. The router gains:

- A `/var/lib/agent-mcp/router.db` SQLite store for `users`,
  `sessions`, and `project_membership`.
- A `POST /agent-mcp/login` form that creates a server-side session
  and sets an opaque `agent_mcp_session` cookie (HttpOnly, Secure,
  SameSite=Lax, Path=/agent-mcp/).
- A `require_operator_session` aiohttp middleware that gates every
  dashboard mutation/read on a valid session cookie + project
  membership (for project-scoped paths).
- A `require_operator_session` FastAPI dependency on the per-project
  backend that accepts the cookie OR a legacy `Authorization: Bearer
  <admin_token>` header OR a body-token field — the legacy paths stay
  for Phase 1 backwards-compat, then narrow over Phase 2/3.

The `admin_token` keeps authenticating *agents* via the
`Authorization: Bearer` header on `/mcp` endpoints — that path is
unchanged. Only the dashboard surface migrates to cookies.

Three bootstrap paths cover the deploy matrix:

1. **Env vars** (`AGENT_MCP_BOOTSTRAP_USERNAME` /
   `AGENT_MCP_BOOTSTRAP_PASSWORD`) — for NixOS+sops deployments where
   the secret is sourced from a sops-encrypted file at activation
   time.
2. **Setup wizard** — first-boot redirect to `/agent-mcp/setup` when
   the `users` table is empty; the operator chooses username +
   password from the browser.
3. **CLI** — `agent-mcp router create-operator --username <u>` for
   ops fallbacks and second-operator provisioning.

The first operator created against a non-empty project registry
inherits `project_membership` rows for every existing project — this
is the pre-Phase-1-deployment migration story, so single-tenant
deploys upgrade without losing access.

## Consequences

- The "anyone with the URL is admin" assumption from ADR-0009 is
  retired on the dashboard surface. The router HTML / React
  dashboard now bounces unauthenticated callers to `/agent-mcp/login`.
- Agent-side MCP traffic (`/agent-mcp/mcp/<project>`) is unchanged.
  Spawned agents continue to authenticate with the admin token; no
  agent-config migration is required for Phase 1.
- Operators can now safely expose the dashboard URL on shared
  networks. Tailnet ACLs / VPN posture remain a defence-in-depth
  layer but no longer the *only* layer.
- Multi-operator workflows are unlocked: a second operator added via
  `create-operator` gets explicit `project_membership` grants and
  shares the dashboard with the first.
- Phase 2 introduces the manager-agent role (a tier between operator
  and worker); Phase 3 layers groups + SSO via OIDC.
- CSRF protection beyond the `SameSite=Lax` cookie attribute is
  deferred to Phase 2 — `Lax` is enough to block cross-origin form
  posts in browsers that respect the attribute, and the dashboard's
  mutations are all `application/json` (not form-encoded), which
  triggers the CORS preflight gate for cross-origin callers.

## Alternatives considered

- **Keep the admin-by-design dashboard, document tailnet ACL as
  required.** Rejected: relies on every deployer correctly
  configuring perimeter access; one missed ACL = total compromise.
  Also blocks multi-operator workflows.
- **JWT instead of server-side sessions.** Rejected for a
  single-host deploy: JWT's stateless property only matters when you
  scale to multiple ingresses; for one host, server-side sessions
  give us immediate revocation (delete the row) and a single source
  of truth in `router.db`.
- **OIDC SSO at Phase 1.** Deferred to Phase 3 — a non-trivial
  Authlib integration that varies per deployment (Authelia vs Keycloak
  vs Google Workspace) and isn't blocking the multi-operator unlock.

## References

- Plan: `prancy-napping-pie` Phase 1 (operator login + framework
  migration).
- Supersedes: ADR-0009 (dashboard owns the ops surface) — the "owns
  the ops surface" half stands; the "implicit admin" half is
  retired.
- Related: ADR-0008 (single-tenant URL parity) — single-tenant mode
  still works under the operator-login regime; the bootstrap is the
  same.
