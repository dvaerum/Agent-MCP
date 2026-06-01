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
    <div className="w-full space-y-[var(--space-fluid-lg)]">
      {/* Header - following tasks dashboard pattern */}
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
        <div>
          <h1 className="text-fluid-2xl font-bold text-foreground">Multi-Agent Collaboration Network</h1>
          <p className="text-muted-foreground text-fluid-base mt-1">Real-time visualization of agent-task relationships</p>
        </div>
        <div className="flex flex-wrap items-center gap-2 sm:gap-3">
          <Badge variant="outline" className="text-xs bg-green-500/15 text-green-600 border-green-500/30 font-medium">
            <div className="w-2 h-2 bg-green-500 rounded-full mr-2 animate-pulse" />
            {activeServer?.name}
          </Badge>
          {data?.timestamp && (
            <span className="text-xs text-muted-foreground">
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

      {/* Graph Container - taking full available space like tasks table */}
      <div className="bg-card/30 border border-border/50 rounded-lg backdrop-blur-sm overflow-hidden" style={{ height: 'calc(100vh - 280px)' }}>
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