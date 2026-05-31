"use client"

import React, { useEffect, useState } from "react"
import { useServerStore } from "@/lib/stores/server-store"
import { ServerConnection } from "@/components/server/server-connection"

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

  const activeServer = servers.find(s => s.id === activeServerId)
  const isConnected =
    hydrated && activeServerId && activeServer?.status === "connected"

  if (!isConnected) {
    return <ServerConnection />
  }

  return <div className="h-full w-full">{children}</div>
}