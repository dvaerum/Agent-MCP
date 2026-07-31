# ADR 0014: REST admin API; retire the `__` URL namespace

**Status**: Accepted (v5.0.60)
**Date**: 2026-06-18
**Supersedes**: the implicit "PR-C will finish this" migration plan
  documented in `agent_mcp/dashboard/lib/urls.ts:1-40`.
**Superseded in part by**: ADR-0020 (2026-07-31) — the leading
  `/agent-mcp` mount prefix in the URL table below is no longer owned by
  the app; it becomes a reverse-proxy concern (`X-Forwarded-Prefix`).
  Read the routes below as relative to the proxy's mount (e.g.
  `…/api/router/health`). Everything else in this ADR — retiring `__`,
  the REST resources, the reserved `api`/`app`/`assets`/`mcp`/`router`
  segments, Accept-header versioning — stands unchanged.

## Context

The dashboard router has shipped ~11 operator-facing endpoints under
`/agent-mcp/__*` since the original deploy
(`__projects`, `__create`, `__rename`, `__unregister`, `__stop`,
`__overview`, `__client-config/<n>.mcp.json`,
`__client-installer/<n>.sh`, `__alias-usage`, `__remove-alias`,
`__create-agent`). The `__` sigil was a reserved-namespace trick to
keep router-admin paths from colliding with project names (the slug
regex bans underscores).

PR-C added a partial REST shape at `/api/projects/...` (POST create,
DELETE delete, POST rename, POST stop) but didn't migrate the
dashboard or remove the legacy `__*` handlers. The dashboard kept
hitting the legacy URLs; the partial REST surface was largely dead
code. Two consequent problems:

  - **Two URL surfaces to reason about.** Auth gating, single-tenant
    behaviour, and reserved-name validation had to keep both shapes
    in sync.
  - **The legacy shape leaked the implementation.**
    Form-encoded bodies + 303 redirects to an HTML index page (since
    Phase 6 the index page itself is gone — the redirects now land on
    a JSON service descriptor) made non-browser clients awkward to
    write and the dashboard's modal code unable to consume the
    response envelope cleanly.

## Decision

Retire the `__` namespace. Every operator endpoint moves to a REST
resource under `/agent-mcp/api/router/...`. The single reserved
top-level segment `router` (joining `api`, `app`, `assets`, `mcp` —
all defended at slug-validate time) carves out the admin namespace
so the surface can't collide with a project named `projects` /
`overview` / `health`.

| Legacy | New | Method |
|---|---|---|
| `GET /agent-mcp/__projects` | `GET /agent-mcp/api/router/projects` | `GET` |
| `POST /agent-mcp/__create` (form-encoded) | `POST /agent-mcp/api/router/projects` (JSON) | `POST` |
| `POST /agent-mcp/__rename` | `PATCH /agent-mcp/api/router/projects/<name>` (body: `{name, grace_days?}`) | `PATCH` |
| `POST /agent-mcp/__unregister` | `DELETE /agent-mcp/api/router/projects/<name>` | `DELETE` |
| `POST /agent-mcp/__stop` | `POST /agent-mcp/api/router/projects/<name>/stop` | `POST` |
| `GET /agent-mcp/__overview` | `GET /agent-mcp/api/router/overview` | `GET` |
| `GET /agent-mcp/__client-config/<n>.mcp.json` | `GET /agent-mcp/api/router/projects/<name>/client-config` | `GET` |
| `GET /agent-mcp/__client-installer/<n>.sh` | `GET /agent-mcp/api/router/projects/<name>/installer` | `GET` |
| `GET /agent-mcp/__alias-usage` | `GET /agent-mcp/api/router/projects/<name>/aliases?alias=<a>` | `GET` |
| `POST /agent-mcp/__remove-alias` | `DELETE /agent-mcp/api/router/projects/<name>/aliases/<alias>` | `DELETE` |
| `POST /agent-mcp/__create-agent` | `POST /agent-mcp/api/router/projects/<name>/agents` | `POST` |
| *(new)* | `GET /agent-mcp/api/router/health` | `GET` (public) |

Notes:

  - `client-config` keeps the `.mcp.json` payload but the URL drops
    the file extension. The vendor media type
    `application/vnd.agent-mcp.client-config+json` advertises the
    shape via `Content-Type`.
  - `installer` similarly drops `.sh`; served as `text/x-shellscript`
    so `curl | bash` is unambiguous.
  - All mutation endpoints take JSON bodies (no form-encoded /
    multipart/form-data).
  - Versioning stays Accept-header (`application/vnd.agent-mcp.v1+json`)
    — no `/v1/` segment in the path.

### Auth

All new routes flow through Phase 1 PR D's
`require_operator_session_middleware` automatically because they live
under `/api/...`. The dashboard sends no `token` field in any request
body — the session cookie is sent automatically.

The one exception is `GET /agent-mcp/api/router/health`, listed in
`_UNAUTH_PREFIXES`. It's the unauthenticated liveness probe / public
service descriptor.

`_NON_PROJECT_API_SEGMENTS` becomes `{"router"}` (was `{"projects"}`)
— the single reserved top-level segment captures the whole admin
surface, so per-project membership checks skip cleanly for any
`/api/router/...` URL.

### Scope of the migration

The retirement is **atomic**:

  1. New routes ship together with dashboard migration AND legacy
     route deletion AND nix-test updates AND this ADR.
  2. The legacy `__*` URLs return 404 (not 410, not redirected). A
     dedicated test module (`tests/router/test_router_admin_api.py`)
     guards the retirement with one positive + one negative test per
     endpoint.
  3. No dual-routing window — the dashboard is the only first-party
     consumer and it migrates in the same diff.

## Consequences

**Positive**

  - One URL surface to reason about. Inline `/agent-mcp/...` strings
    in dashboard code are forbidden; every URL flows through
    `agent_mcp/dashboard/lib/urls.ts`.
  - REST shape lets non-dashboard clients (CI scripts, monitoring,
    future automation) reason about the surface declaratively.
  - The Accept-header gate applies uniformly. The discoverable
    service descriptor at `GET /agent-mcp/api/router/health`
    advertises the version + mode without bypassing auth.
  - Project-name reservation grows from 4 segments to 5 (added
    `router`) but the surface area for collision shrinks: every admin
    route nests under the one reserved segment, not 11 different
    `__*` paths.

**Negative**

  - URL churn for any out-of-tree consumer that hard-coded a `__*`
    path. The legacy URLs 404 in v5.0.60; there is no grace window.
    Recovery is a one-line URL swap per call site.
  - The router-admin `create-agent` endpoint
    (`POST /api/router/projects/<name>/agents`) sits next to the
    per-project endpoint (`POST /api/<project>/agents`) and the two
    URLs differ only in the `router/projects/` segment. The
    docstring on `admin_api.create_agent_handler` explains the
    layering distinction (admin wrapper that proxies via
    `_mcp_call_admin` vs direct MCP-session create).
