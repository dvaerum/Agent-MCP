"use client"

import React, { useEffect } from "react"
import { RefreshCw, Server } from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import { useServerStore } from "@/lib/stores/server-store"
import { useDataStore } from "@/lib/stores/data-store"
import { useDialog } from "@/hooks/use-dialog"
import { VisGraph } from "./vis-graph-simple"
import { NodeDetailPanel } from "./node-detail-panel"
import { CORSDiagnostic } from "../debug/cors-diagnostic"

type SelectedNode = {
  id: string
  type: 'agent' | 'task' | 'context' | 'file' | 'admin'
  data: unknown
}

export function OverviewDashboard() {
  const { servers, activeServerId } = useServerStore()
  const activeServer = servers.find(s => s.id === activeServerId)
  const { data, loading, fetchAllData, isRefreshing } = useDataStore()
  
  // Node-detail panel state. Migrated to useDialog<SelectedNode>()
  // (Candidate F1, architecture review 2026-06-01) — replaces three
  // nullable useStates (id / type / data) plus a parallel boolean
  // (isPanelOpen) with a single piece of state. The three values
  // are read together as a tuple anyway, so packing them into one
  // record removes the "are they in sync?" question.
  const nodeDialog = useDialog<SelectedNode>()
  
  useEffect(() => {
    // Fetch data on mount
    if (activeServerId && activeServer?.status === 'connected') {
      fetchAllData()
    }
  }, [activeServerId, activeServer?.status, fetchAllData])
  
  const isConnected = !!activeServerId && activeServer?.status === 'connected'

  // Show connection prompt if no server is selected
  if (!isConnected) {
    return (
      <div className="h-full flex items-center justify-center p-4">
        <Card className="max-w-md">
          <CardContent className="flex flex-col items-center justify-center py-12 px-8 text-center">
            <Server className="h-12 w-12 text-muted-foreground mb-4" />
            <h3 className="text-lg font-medium text-foreground mb-2">Connect to an MCP Server</h3>
            <p className="text-muted-foreground text-sm">
              Select an MCP server from the project picker in the header to view the system graph and manage agents.
            </p>
            {activeServer && activeServer.status === 'error' && (
              <div className="text-sm text-destructive mt-4">
                Failed to connect to {activeServer.name} ({activeServer.baseUrl ?? `${activeServer.host}:${activeServer.port}`})
                <div className="mt-4">
                  <CORSDiagnostic />
                </div>
              </div>
            )}
          </CardContent>
        </Card>
      </div>
    )
  }

  const handleClosePanel = () => {
    nodeDialog.close()
  }

  return (
    /* CC-8/CC-16/CC-19/CC-26 audit 2026-06-02: plain Tailwind spacing,
       h1 sizing, drop animate-pulse, shorten H1 wrap. */
    <div className="w-full p-4 sm:p-6 space-y-4 sm:space-y-6 flex flex-col h-full">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4 shrink-0">
        <div>
          <h1 className="text-2xl sm:text-3xl font-semibold tracking-tight text-foreground">Collaboration Network</h1>
          <p className="text-muted-foreground text-sm sm:text-base mt-1">Real-time visualization of agent-task relationships</p>
        </div>
        <div className="flex flex-wrap items-center gap-2 sm:gap-3">
          <Badge variant="outline" className="text-xs bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 border-emerald-500/30 font-medium">
            <span aria-hidden className="w-2 h-2 bg-emerald-500 rounded-full mr-2" />
            {activeServer?.name}
          </Badge>
          {data?.timestamp && (
            <span className="text-xs text-muted-foreground tabular-nums">
              Last updated: {new Date(data.timestamp).toLocaleTimeString()}
            </span>
          )}
          <Button
            variant="outline"
            size="sm"
            onClick={() => fetchAllData(true)}
            disabled={loading || isRefreshing}
            className="text-xs"
          >
            <RefreshCw className={`h-3.5 w-3.5 mr-1.5 ${(loading || isRefreshing) ? 'animate-spin' : ''}`} />
            Refresh
          </Button>
        </div>
      </div>

      {/* Graph Container — CC-9 partial / overview-specific fix: was
          `style={{ height: 'calc(100vh - 280px)' }}` (magic px offset
          that broke when the header wrapped to multiple rows at narrow
          viewports). Now uses `flex-1 min-h-[400px]` so it expands to
          fill whatever space remains inside the page flex column. */}
      <div className="bg-card border border-border rounded-lg overflow-hidden flex-1 min-h-[400px]">
        <VisGraph
          fullscreen
          onNodeSelect={(nodeId, nodeType, nodeData) => {
            nodeDialog.open({ id: nodeId, type: nodeType, data: nodeData })
          }}
        />
      </div>

      
      {/* Node Detail Panel - Fixed positioned */}
      <NodeDetailPanel
        nodeId={nodeDialog.data?.id ?? null}
        nodeType={nodeDialog.data?.type ?? null}
        nodeData={(nodeDialog.data?.data ?? null) as any}
        isOpen={nodeDialog.isOpen}
        onClose={handleClosePanel}
      />
    </div>
  )
}