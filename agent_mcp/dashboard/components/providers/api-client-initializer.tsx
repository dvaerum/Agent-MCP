"use client"

import { useEffect } from 'react'
import { useServerStore } from '@/lib/stores/server-store'
import { apiClient } from '@/lib/api'

// Matches /agent-mcp/__dashboard/<name>/... — the deployment URL
// shape used when this dashboard is mounted behind a path-prefixed
// reverse proxy. Auto-seed + cold-start retry only kick in when the
// URL matches. Standalone deployments (no path prefix) fall through
// to the original hydrate-from-store behavior.
const DASHBOARD_PATH_RE = /\/agent-mcp\/__dashboard\/([^/]+)/

export function ApiClientInitializer() {
  const { activeServerId, servers } = useServerStore()

  // Auto-seed a synthetic server entry from window.location.pathname
  // when the URL identifies a deployment-mounted project. Upstream
  // gates the main pane on a connected server-store entry; without
  // this seed, a freshly-mounted dashboard with an empty persisted
  // store shows "Connect to MCP Server" and the sidebar does nothing.
  //
  // Must wait for zustand-persist hydration — the first render uses
  // the store's initial empty state and the persisted state arrives
  // a tick later. Seeding before hydration creates a duplicate entry
  // every reload.
  //
  // (host, port) are placeholders — the activeServerId effect below
  // would otherwise call setServer(host, port) and produce a broken
  // `http://proxy:0/api` URL. We pre-empt that by calling
  // apiClient.setBaseUrl with the URL-derived API root
  // (`/agent-mcp/__api/<name>`) BEFORE the seed runs, so even the
  // cold-start retry fetches via the router's proxy. The subsequent
  // setServer call still happens (the synthetic entry satisfies the
  // store gate) but the explicit baseUrl overrides what it produces.
  useEffect(() => {
    const trySeed = () => {
      if (typeof window === 'undefined') return
      const m = DASHBOARD_PATH_RE.exec(window.location.pathname)
      if (!m) return
      const name = m[1]
      apiClient.setBaseUrl('/agent-mcp/__api/' + name)
      const store = useServerStore.getState()
      let existing = store.servers.find(s => s.name === name)
      if (!existing) {
        store.addServer({ name, host: 'proxy', port: 0 })
        existing = useServerStore.getState().servers.find(s => s.name === name)
      }
      if (existing && useServerStore.getState().activeServerId !== existing.id) {
        useServerStore.getState().setActiveServer(existing.id)
      }
    }
    if (useServerStore.persist.hasHydrated()) {
      trySeed()
      return
    }
    const unsub = useServerStore.persist.onFinishHydration(trySeed)
    return unsub
  }, [])

  useEffect(() => {
    console.debug('ApiClientInitializer: Hydrating API client...')
    if (activeServerId) {
      const activeServer = servers.find(s => s.id === activeServerId)
      if (activeServer) {
        console.debug(`ApiClientInitializer: Setting API server to ${activeServer.host}:${activeServer.port}`)
        apiClient.setServer(activeServer.host, activeServer.port)
        // Path-prefix deployments: override the host:port baseUrl
        // with the URL-derived API root. setServer above is still
        // useful because some external code reads the server entry,
        // but the actual fetches must go through the router proxy.
        if (typeof window !== 'undefined') {
          const m = DASHBOARD_PATH_RE.exec(window.location.pathname)
          if (m) {
            apiClient.setBaseUrl('/agent-mcp/__api/' + m[1])
          }
        }
      }
    }
  }, [activeServerId, servers])

  // Cold-start retry. A freshly-stopped backend takes ~10-15s to
  // create its socket (Python import time + lifespan startup). The
  // dashboard's first health check can fire well before that;
  // setActiveServer marks the entry status: 'error' and the user
  // lands on "Connect to MCP Server" even though the URL already
  // identifies the project. Retry up to 30× at 1.5s = 45s, which is
  // the worst-case backend startup the router itself tolerates.
  useEffect(() => {
    if (typeof window === 'undefined') return
    const m = window.location.pathname.match(DASHBOARD_PATH_RE)
    if (!m) return
    const name = m[1]
    if (activeServerId) return
    const target = servers.find(s => s.name === name)
    if (!target || target.status === 'connecting') return
    let attempts = 0
    const tick = setInterval(() => {
      attempts += 1
      if (useServerStore.getState().activeServerId) {
        clearInterval(tick)
        return
      }
      const t = useServerStore.getState().servers.find(s => s.name === name)
      if (!t) {
        clearInterval(tick)
        return
      }
      useServerStore.getState().setActiveServer(t.id)
      if (attempts >= 30) {
        clearInterval(tick)
      }
    }, 1500)
    return () => clearInterval(tick)
  }, [activeServerId, servers])

  return null
}
