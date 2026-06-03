/**
 * Single source of URL truth for the dashboard (PR-B).
 *
 * Before PR-B every dashboard file knew the router URL shape directly:
 * 14 files hard-coded `/agent-mcp/__dashboard/...` / `/agent-mcp/__api/...`
 * paths inline (see audit §5). PR-B's URL rename forced a touch in
 * every one of them. This module exists so the NEXT rename touches one
 * file plus the consumers that use the helpers, not every component
 * with a string template.
 *
 * Public surface (Shape 3, locked by /grill-me):
 *
 *   /agent-mcp/                  — service descriptor (browsers: 302 → /app/)
 *   /agent-mcp/app/              — React overview (cross-project cards)
 *   /agent-mcp/app/<name>/       — per-project dashboard pages
 *   /agent-mcp/app/<name>/<sec>  — section deep-link (e.g. /tasks, /agents)
 *   /agent-mcp/api/<name>/<rest> — REST surface (strict Accept gate, PR-A)
 *   /agent-mcp/assets/<rest>     — Next.js static bundle (sentinel-substituted)
 *   /agent-mcp/<name>/mcp        — MCP transport (PR-D moves to /mcp/<name>)
 *
 * Direct router endpoints (NOT yet renamed in PR-B — PR-C folds them
 * into POST /api/projects):
 *
 *   /agent-mcp/__projects, /agent-mcp/__overview, /agent-mcp/__create,
 *   /agent-mcp/__rename, /agent-mcp/__unregister, /agent-mcp/__alias-usage,
 *   /agent-mcp/__remove-alias, /agent-mcp/__client-config/<n>.mcp.json,
 *   /agent-mcp/__client-installer/<n>.sh
 *
 * These are typed in the helpers below as `internalRouterUrl(op)` so
 * the call sites don't grow another flavour of hardcoded path.
 */

// ── Top-level path segments ─────────────────────────────────────────
// Capitalised constants make accidental drift visible. Update one place
// when the prefix changes; the next URL-rename PR rides on this file.
const ROOT = "/agent-mcp"
const APP = `${ROOT}/app`
const API = `${ROOT}/api`
const ASSETS = `${ROOT}/assets`

/** Service descriptor URL — fetch for endpoint discovery. */
export function descriptorUrl(): string {
  return `${ROOT}/`
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

/** Static asset URL prefix — the value Next.js's assetPrefix bakes
 *  into the bundle (and what the sentinel substitution emits at serve
 *  time). The optional `path` argument is concatenated as-is for
 *  callers that build asset URLs directly (rare). */
export function assetsUrl(path?: string): string {
  if (path === undefined) return ASSETS
  return `${ASSETS}/${path.replace(/^\/+/, "")}`
}

/** MCP transport URL for a project. PR-B keeps the per-project shape
 *  /agent-mcp/<name>/mcp; PR-D will rewrite this to /agent-mcp/mcp/<name>.
 *  Callers that build MCP-client config strings MUST go through this
 *  helper so the PR-D move is a one-line change. */
export function mcpUrl(projectName: string, origin: string = ""): string {
  return `${origin}${ROOT}/${encodeURIComponent(projectName)}/mcp`
}

/** Direct router-internal endpoints (not yet under /api/). PR-C will
 *  fold the project-lifecycle ones into REST resources; the others
 *  (__client-config, __client-installer) stay as router-only utilities
 *  because they're operator wiring tools, not API surface. */
export function internalRouterUrl(op: string, query?: string): string {
  // `op` is the segment after /agent-mcp/ (e.g. `__projects`,
  // `__client-config/foo.mcp.json`).
  const cleaned = op.replace(/^\/+/, "")
  const base = `${ROOT}/${cleaned}`
  if (query === undefined) return base
  const sep = base.includes("?") ? "&" : "?"
  return `${base}${sep}${query.replace(/^[?&]+/, "")}`
}

// ── URL pattern matchers (used by project-context.ts) ───────────────

/** Regex matching /agent-mcp/app/<name>/<rest?> — extracts the project
 *  name as match[1]. The trailing slash is optional in the URL but the
 *  client-router code normalises it. */
export const APP_PROJECT_PATH_RE = /\/agent-mcp\/app\/([^/]+)/

/** Regex matching the bare /agent-mcp/app/ overview path (no project
 *  segment). The cross-project React overview lives here. */
export const APP_OVERVIEW_PATH_RE = /\/agent-mcp\/app\/?$/

// ── Legacy / deprecated path matchers ───────────────────────────────
// Kept around for the 30-day 308-redirect grace period. Components that
// run on the OLD URL get bounced to the new path by the router; any
// regex parsing in client code should look for BOTH shapes so the
// transient state is handled gracefully.
export const LEGACY_DASHBOARD_PATH_RE = /\/agent-mcp\/__dashboard\/([^/]+)/
