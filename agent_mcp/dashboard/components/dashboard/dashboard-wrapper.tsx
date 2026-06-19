"use client"

import React, { useEffect, useState } from "react"
import { useServerStore } from "@/lib/stores/server-store"
import { ServerConnection } from "@/components/server/server-connection"
import { projectContext } from "@/lib/project-context"

interface DashboardWrapperProps {
  children: React.ReactNode
}

export function DashboardWrapper({ children }: DashboardWrapperProps) {
  const { activeServerId, servers } = useServerStore()
  // zustand-persist hydrates after first paint. Reading activeServerId /
  // servers at render time gives the default empty state during SSG and
  // the persisted state on the client — that mismatch surfaces as React
  // error #418. Gate the connected check on a post-mount `hydrated`
  // flag so first client paint matches SSG output, then re-render
  // against the persisted state.
  const [hydrated, setHydrated] = useState(false)
  useEffect(() => {
    if (useServerStore.persist.hasHydrated()) {
      setHydrated(true)
      return
    }
    const unsub = useServerStore.persist.onFinishHydration(() => setHydrated(true))
    return unsub
  }, [])

  // Router-served deployments mount the dashboard at
  // `/agent-mcp/app/<name>/`. The URL already names the project, the
  // always-on Python router already proxies `/agent-mcp/api/<name>` to
  // the per-project backend, and `project-context.ts` already pointed
  // `apiClient` at that root at module load. The connect-to-MCP gating
  // screen exists for standalone deployments (Electron / external tool)
  // where the operator picks a host:port — it has no purpose here, and
  // before this fix it left users staring at a dead "Connect to MCP
  // Server" screen because `setActiveServer`'s health-check could lag
  // or fail without producing a `status === "connected"` server-store
  // entry. Skip the gate when we know which project we're serving;
  // child dashboards render against the already-configured baseUrl.
  if (projectContext.isRouterServed && projectContext.projectName !== null) {
    return <div className="h-full w-full">{children}</div>
  }

  const activeServer = servers.find(s => s.id === activeServerId)
  const isConnected =
    hydrated && activeServerId && activeServer?.status === "connected"

  if (!isConnected) {
    return <ServerConnection />
  }

  return <div className="h-full w-full">{children}</div>
}