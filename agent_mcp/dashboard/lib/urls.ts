/**
 * Single source of URL truth for the dashboard.
 *
 * Every dashboard URL — overview reads, project lifecycle mutations,
 * wiring snippets, MCP transport, static assets — flows through one of
 * the helpers below. Inline ``/agent-mcp/...`` strings are forbidden;
 * the next URL rename touches this file and the consumers that use
 * the helpers, not every component with a string template.
 *
 * Public surface (locked by ADR 0014):
 *
 *   /agent-mcp/                              service descriptor
 *                                            (browsers: 302 → /app/)
 *   /agent-mcp/app/                          React overview (cross-project)
 *   /agent-mcp/app/<name>/                   per-project dashboard pages
 *   /agent-mcp/app/<name>/<sec>              section deep-link
 *   /agent-mcp/api/<name>/<rest>             per-project REST proxy
 *                                            (strict Accept gate)
 *   /agent-mcp/api/router/health             public service descriptor
 *   /agent-mcp/api/router/projects           list / create
 *   /agent-mcp/api/router/projects/<n>       PATCH / DELETE
 *   /agent-mcp/api/router/projects/<n>/stop  stop backend
 *   /agent-mcp/api/router/projects/<n>/client-config
 *                                            JSON .mcp.json descriptor
 *   /agent-mcp/api/router/projects/<n>/installer
 *                                            text/x-shellscript installer
 *   /agent-mcp/api/router/projects/<n>/aliases?alias=<a>
 *                                            alias usage lookup
 *   /agent-mcp/api/router/projects/<n>/aliases/<a>
 *                                            DELETE — expire alias now
 *   /agent-mcp/api/router/projects/<n>/agents
 *                                            POST — admin create-agent
 *   /agent-mcp/api/router/overview           cross-project envelope
 *   /agent-mcp/assets/<rest>                 Next.js static bundle
 *   /agent-mcp/mcp/<name>                    MCP transport
 */

// ── Mount prefix (ADR-0020) ─────────────────────────────────────────
// The router serves the dashboard at different external mounts: under
// `/agent-mcp/…` on the tailnet, and at the host ROOT behind a Traefik
// reverse proxy (mm.best.aau.dk). The prefix is owned by the proxy, so
// the dashboard DERIVES it at runtime from window.location — everything
// below (API base, nav links, login, SSE, path regexes) cascades from it.
//
// Derivation: the prefix is whatever precedes the first reserved segment
// (app | api | assets | mcp | login — ADR-0014's reserved segments). The
// dashboard SPA only ever runs under `…/app/…`, so this always resolves:
//   /agent-mcp/app/foo/  → "/agent-mcp"   (tailnet — byte-identical)
//   /app/foo/            → ""             (Traefik root)
// SSR/prerender (next build, no window) defaults to "/agent-mcp"; the
// static export re-runs this in the browser at import, so the client
// value is always correct per-origin.
export function deriveMount(pathname?: string): string {
  const p =
    pathname ??
    (typeof window === "undefined" ? null : window.location.pathname)
  // No window (SSR/prerender) → the historical default; the static
  // export re-evaluates this in the browser at import, so the client
  // value is always correct per-origin.
  if (p === null || p === undefined) return "/agent-mcp"
  const m = p.match(/^(.*?)\/(?:app|api|assets|mcp|login)(?:\/|$)/)
  return m?.[1] ?? ""
}

// ── Top-level path segments ─────────────────────────────────────────
const ROOT = deriveMount()
const APP = `${ROOT}/app`
const API = `${ROOT}/api`
const ROUTER_API = `${API}/router`
const ROUTER_PROJECTS = `${ROUTER_API}/projects`

/** Operator login page. Pass ``next`` to preserve the current path
 *  across the bounce — the wizard reads it back as the post-login
 *  redirect target. */
export function loginUrl(next?: string): string {
  if (next === undefined) return `${ROOT}/login`
  return `${ROOT}/login?next=${encodeURIComponent(next)}`
}

/** Operator logout endpoint (R12-F1). ``POST``-only server route that
 *  drops the session row and clears the cookie — see
 *  ``agent_mcp/router/login.py`` ``logout_handler``. Mount-derived like
 *  ``loginUrl()`` (ADR-0020: root vs tailnet front doors both alias
 *  this route to the same handler via ``_add_root_aliases``). */
export function logoutUrl(): string {
  return `${ROOT}/logout`
}

/** React overview entry (cross-project cards). */
export function overviewAppUrl(): string {
  return `${APP}/`
}

/** Per-project dashboard root (e.g. /agent-mcp/app/washing-brothers/). */
export function appUrl(projectName: string, section?: string): string {
  const base = `${APP}/${encodeURIComponent(projectName)}/`
  if (section === undefined) return base
  // Section is a sub-path under the project root; the URL-routing
  // hook reads it back via location.pathname. Strip any leading slash
  // the caller provided so we don't emit a doubled separator.
  return `${base}${section.replace(/^\/+/, "")}`
}

/** REST root for a project — what ApiClient.setBaseUrl receives. */
export function apiUrl(projectName: string, rest?: string): string {
  const base = `${API}/${encodeURIComponent(projectName)}`
  if (rest === undefined) return base
  return `${base}/${rest.replace(/^\/+/, "")}`
}

/** MCP transport URL for a project. Callers that build MCP-client
 *  config strings go through this helper so the URL shape is
 *  centralised. */
export function mcpUrl(projectName: string, origin: string = ""): string {
  return `${origin}${ROOT}/mcp/${encodeURIComponent(projectName)}`
}

/** Operator dashboard live-update SSE channel for a project. Distinct
 *  from ``mcpUrl`` (the agent-scoped MCP transport): this is the
 *  cookie-authenticated ``GET /agent-mcp/api/<name>/events`` endpoint the
 *  dashboard's notification client subscribes to, proxied through the
 *  REST ``/api`` root so the operator session cookie carries the auth. */
export function eventsUrl(projectName: string, origin: string = ""): string {
  return `${origin}${API}/${encodeURIComponent(projectName)}/events`
}

// ── Router admin surface (ADR 0014) ────────────────────────────────

/** Cross-project overview envelope (consumed by the React overview's
 *  store). */
export function overviewUrl(): string {
  return `${ROUTER_API}/overview`
}

/** Collection URL — ``GET`` lists projects, ``POST`` creates one. */
export function routerProjectsUrl(): string {
  return ROUTER_PROJECTS
}

/** Per-project resource URL — ``PATCH`` to rename, ``DELETE`` to
 *  unregister. The optional ``query`` is concatenated unescaped (the
 *  caller is responsible for encoding); used for
 *  ``?delete_workspace=true``. */
export function routerProjectUrl(name: string, query?: string): string {
  const base = `${ROUTER_PROJECTS}/${encodeURIComponent(name)}`
  if (query === undefined) return base
  return `${base}?${query.replace(/^[?&]+/, "")}`
}

/** ``GET`` returns the project's ``.mcp.json`` body with the vendor
 *  media type ``application/vnd.agent-mcp.client-config+json``. */
export function projectClientConfigUrl(name: string): string {
  return `${ROUTER_PROJECTS}/${encodeURIComponent(name)}/client-config`
}

/** ``GET`` returns the project's installer shell script with
 *  ``Content-Type: text/x-shellscript``. */
export function projectInstallerUrl(name: string): string {
  return `${ROUTER_PROJECTS}/${encodeURIComponent(name)}/installer`
}

/** ``GET ...?alias=<a>`` returns the usage record for the alias
 *  ``<a>`` against the project ``<name>``. */
export function projectAliasesUrl(name: string, alias?: string): string {
  const base = `${ROUTER_PROJECTS}/${encodeURIComponent(name)}/aliases`
  if (alias === undefined) return base
  return `${base}?alias=${encodeURIComponent(alias)}`
}

/** ``DELETE`` expires the alias immediately, skipping the reaper. */
export function projectAliasUrl(name: string, alias: string): string {
  return (
    `${ROUTER_PROJECTS}/${encodeURIComponent(name)}` +
    `/aliases/${encodeURIComponent(alias)}`
  )
}

// ── Router admin: users / groups / memberships (Phase 3 Wave 1b) ───

/** Users collection (``GET`` list, ``POST`` create). */
export function routerUsersUrl(): string {
  return `${ROUTER_API}/users`
}

/** Per-user resource (``PATCH`` edit, ``DELETE`` remove). */
export function routerUserUrl(userId: string): string {
  return `${ROUTER_API}/users/${encodeURIComponent(userId)}`
}

/** Groups collection (``GET`` list, ``POST`` create). */
export function routerGroupsUrl(): string {
  return `${ROUTER_API}/groups`
}

/** Per-group resource (``PATCH`` edit, ``DELETE`` remove). */
export function routerGroupUrl(groupId: string): string {
  return `${ROUTER_API}/groups/${encodeURIComponent(groupId)}`
}

/** Group members collection (``GET`` list, ``POST`` add). */
export function routerGroupMembersUrl(groupId: string): string {
  return `${ROUTER_API}/groups/${encodeURIComponent(groupId)}/members`
}

/** Single group member by surrogate member_id (``DELETE``). */
export function routerGroupMemberUrl(
  groupId: string,
  memberId: string,
): string {
  return (
    `${ROUTER_API}/groups/${encodeURIComponent(groupId)}` +
    `/members/${encodeURIComponent(memberId)}`
  )
}

/** Per-group capability grants. ``GET`` returns the cap list;
 *  ``PUT`` atomically replaces it (Wave 9 PR 5). Sysadmin-only. */
export function routerGroupCapabilitiesUrl(groupId: string): string {
  return `${ROUTER_API}/groups/${encodeURIComponent(groupId)}/capabilities`
}

/** Per-project memberships collection (``GET`` list, ``POST`` add). */
export function projectMembershipsUrl(name: string): string {
  return `${ROUTER_PROJECTS}/${encodeURIComponent(name)}/memberships`
}

/** Per-project membership by surrogate ``u:<id>``/``g:<id>``
 *  (``PATCH`` change role, ``DELETE`` remove). */
export function projectMembershipUrl(
  name: string,
  membershipId: string,
): string {
  return (
    `${ROUTER_PROJECTS}/${encodeURIComponent(name)}` +
    `/memberships/${encodeURIComponent(membershipId)}`
  )
}

// ── Router admin: SSO config (Phase 3 Wave 3) ──────────────────────

/** SSO config introspection (``GET``). Sysadmin-only. */
export function routerSsoConfigUrl(): string {
  return `${ROUTER_API}/sso/config`
}

// ── URL pattern matchers (used by project-context.ts) ───────────────

/** Regex matching <mount>/app/<name>/<rest?> — extracts the project name
 *  as match[1]. ADR-0020: built from the derived mount so it matches at
 *  both /agent-mcp/app/<name> (tailnet) and /app/<name> (Traefik root).
 *  This is what makes project-context.ts identify the project (and take
 *  the router-served render path) under either front door. */
export const APP_PROJECT_PATH_RE = new RegExp(`${ROOT}/app/([^/]+)`)

/** Regex matching the bare <mount>/app/ overview path (no project
 *  segment). The cross-project React overview lives here. */
export const APP_OVERVIEW_PATH_RE = new RegExp(`${ROOT}/app/?$`)

// v5.0.0: ``LEGACY_DASHBOARD_PATH_RE`` (the regex for the old
// /agent-mcp/__dashboard/<name> path shape) was removed alongside the
// router's 308 redirects for that surface. No importers existed at
// the time of removal.
