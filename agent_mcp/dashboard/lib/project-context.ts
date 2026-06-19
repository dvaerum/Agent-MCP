"use client"

/**
 * Module-level singleton + React Context for the dashboard's
 * "what project am I" derivation.
 *
 * Replaces the 3-useEffect bootstrap dance in
 * `components/providers/api-client-initializer.tsx` (Candidate C from
 * architecture review 2026-06-01). The derivation is purely synchronous
 * — it only depends on `window.location.pathname` — so a module-level
 * singleton (computed at import time) is strictly simpler than chaining
 * useEffects through React's render cycle.
 *
 * URL pattern (PR-B). Path-prefixed deployments mount the dashboard at
 * `/agent-mcp/app/<name>/...` (the always-on Python router URL-routes
 * each `<name>` to a per-project systemd-template backend on a Unix
 * socket). When pathname matches, we derive:
 *   - projectName: the `<name>` segment (e.g. `washing-brothers`)
 *   - baseUrl:     the router-proxied API root for fetches
 *                  (`/agent-mcp/api/<name>`)
 *   - apiPrefix:   the same value, exposed separately for callers
 *                  building URLs that aren't simple `${baseUrl}/...`
 *                  concatenations
 *
 * URL building goes through `lib/urls.ts` — the single source of URL
 * truth — so the next URL rename touches one file plus consumers.
 *
 * SSR fallback. Next.js prerenders this module at build time, where
 * `window` is undefined. We fall through to safe defaults
 * (projectName: null, baseUrl: '/api', apiPrefix: '') matching
 * upstream's standalone single-tenant deployment shape. On the client
 * the module re-evaluates with the real pathname.
 *
 * Side effects at module load. When the path-prefix matches, this
 * module:
 *   1. Calls `apiClient.setBaseUrl(baseUrl)` so the very first fetch
 *      (which can land before any React effect runs) already goes
 *      through the router's proxy.
 *   2. Seeds a synthetic entry into `useServerStore` and sets it as
 *      active. The store's `persist` middleware hydrates lazily, so
 *      the seed is gated via `onFinishHydration` to avoid duplicating
 *      the entry on reload. This collapses the auto-seed +
 *      activeServerId-sync effects from the old initializer into one
 *      synchronous-at-import side effect.
 *
 * Cold-start retry. Moved out of this module entirely — it now lives
 * inside `ApiClient.request()` as transparent exponential-backoff on
 * 5xx. Callers see success or a hard failure; the boundary-level
 * setInterval poll is gone.
 *
 * Marked "use client" because `createContext` is a client-only React
 * API. Next.js permits server components (e.g. app/layout.tsx) to
 * import and render `<ProjectContext.Provider>` from a "use client"
 * module — this is the standard Server/Client boundary pattern.
 */

import { createContext } from 'react'
import { apiClient } from './api'
import { useServerStore } from './stores/server-store'
import {
  APP_OVERVIEW_PATH_RE,
  APP_PROJECT_PATH_RE,
  apiUrl,
} from './urls'

function derive(): {
  projectName: string | null
  isOverview: boolean
  // True when the dashboard is served behind the always-on Python
  // router at `/agent-mcp/app/<name>/...` (multi-tenant deployment).
  // Distinct from `projectName !== null` only in that consumers reading
  // this flag don't have to re-derive the standalone-mode case
  // themselves. Used by DashboardWrapper to skip the "Connect to MCP
  // Server" gating screen (the URL already names the project; the
  // router already proxies to its backend; the gating UI is for
  // standalone deployments only) and by ServerConnection to hide the
  // localhost port scanner (cross-origin, useless in this mode).
  isRouterServed: boolean
  baseUrl: string
  apiPrefix: string
} {
  const pathname =
    typeof window !== 'undefined' ? window.location.pathname : ''
  if (APP_OVERVIEW_PATH_RE.test(pathname)) {
    // Cross-project overview route — no per-project API root.
    return {
      projectName: null,
      isOverview: true,
      isRouterServed: true,
      baseUrl: '',
      apiPrefix: '',
    }
  }
  const match = pathname.match(APP_PROJECT_PATH_RE)
  if (match) {
    const apiRoot = apiUrl(match[1])
    return {
      projectName: match[1],
      isOverview: false,
      isRouterServed: true,
      baseUrl: apiRoot,
      apiPrefix: apiRoot,
    }
  }
  // SSR / standalone (no path prefix). Upstream's single-tenant
  // deployment fetches from /api on the same origin.
  return {
    projectName: null,
    isOverview: false,
    isRouterServed: false,
    baseUrl: '/api',
    apiPrefix: '',
  }
}

export const projectContext = derive()

export const ProjectContext = createContext(projectContext)

// Module-load side effects. Only fire on the client AND only when the
// URL identifies a path-prefix-mounted project. Standalone deploys
// keep their existing connect-via-modal flow. Overview mode (no
// project segment) skips both side effects — the overview component
// hits the router's `/__overview` endpoint directly and doesn't need
// the per-project API client/server-store entry.
if (
  typeof window !== 'undefined' &&
  projectContext.projectName !== null &&
  !projectContext.isOverview
) {
  // 1. Point the ApiClient at the router-proxied API root before any
  //    fetch can land. This is the only thing the cold-start retry
  //    needs to know — the ApiClient.request() retry loop handles
  //    waiting out the lazily-spawned backend transparently.
  apiClient.setBaseUrl(projectContext.baseUrl)

  // 2. Seed the zustand server-store so legacy `useServerStore`
  //    consumers (sidebar, overview cards, vis-graph) see a connected
  //    active server and render the dashboard instead of the
  //    "Connect to MCP Server" placeholder.
  //
  //    The seed must wait for `persist` to hydrate, or it duplicates
  //    the entry on every reload (first render seeds empty state,
  //    persisted state arrives a tick later carrying the previous
  //    seed).
  const seed = (): void => {
    const name = projectContext.projectName as string
    const baseUrl = projectContext.baseUrl
    const store = useServerStore.getState()
    let existing = store.servers.find(s => s.name === name)
    if (!existing) {
      // baseUrl is the load-bearing field for path-prefix entries;
      // host/port are kept only to satisfy the legacy MCPServer shape
      // and are hidden in the UI when baseUrl is present.
      store.addServer({ name, host: 'proxy', port: 0, baseUrl })
      existing = useServerStore.getState().servers.find(s => s.name === name)
    } else if (!existing.baseUrl) {
      // Backfill baseUrl on entries persisted before this field
      // existed — otherwise setActiveServer would still overwrite the
      // API client with the stale `proxy:0` host/port.
      store.updateServer(existing.id, { baseUrl })
      existing = useServerStore.getState().servers.find(s => s.name === name)
    }
    if (
      existing &&
      useServerStore.getState().activeServerId !== existing.id
    ) {
      useServerStore.getState().setActiveServer(existing.id)
    }
  }
  if (useServerStore.persist.hasHydrated()) {
    seed()
  } else {
    useServerStore.persist.onFinishHydration(seed)
  }
}
