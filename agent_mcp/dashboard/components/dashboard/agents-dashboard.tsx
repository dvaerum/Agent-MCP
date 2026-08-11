"use client"

import { useState, useEffect, useCallback } from "react"
import {
  Users, Clock, AlertCircle, CheckCircle2, Network,
  Search, PowerOff, Power,
} from "lucide-react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { apiClient, Agent, Task, agentPresence } from "@/lib/api"
import { toastError, toastSuccess } from "@/components/ui/toast"
import { useServerStore } from "@/lib/stores/server-store"
import { useDataStore } from "@/lib/stores/data-store"
import { useDialog } from "@/hooks/use-dialog"
import { useFilters } from "@/hooks/use-filters"
import { TaskDetailsDialog } from "./task-details-dialog"
import { SendDirectiveModal } from "@/components/dashboard/shared/send-directive-modal"
import { AgentMobileCard } from "@/components/dashboard/agents-mobile-list"
import { DataTablePage } from "@/components/dashboard/shared/data-table-page"
import type { StatsCardProps } from "@/components/dashboard/shared/stats-card"
import {
  AGENTS_TABLE_CLASS,
  useAgentColumns,
} from "@/components/dashboard/agents/agent-columns"
import { RegisterAgentModal } from "@/components/dashboard/agents/register-agent-modal"
import { AgentDetailDialog } from "@/components/dashboard/agents/agent-detail-dialog"
import {
  EditAgentDialog,
  type AgentEditUpdates,
} from "@/components/dashboard/agents/edit-agent-dialog"
import { TerminateAgentDialog } from "@/components/dashboard/agents/terminate-agent-dialog"
import { PurgeAgentDialog } from "@/components/dashboard/agents/purge-agent-dialog"

export function AgentsDashboard() {
  const { servers, activeServerId } = useServerStore()
  const activeServer = servers.find(s => s.id === activeServerId)
  const { data, loading, error, fetchAllData, refreshData, getActiveAgents } = useDataStore()
  // Filter state — owned by useFilters<AgentFilters> (PR 4 of the
  // 2026-06-09 architecture review). Replaces the two sibling
  // useStates (searchTerm / statusFilter) shared with messages-/
  // tasks-dashboard.tsx as the same hand-rolled pattern.
  // Agents-dashboard doesn't expose a "Clear filters" button today, so
  // `clearAll` / `isActive` are unused — the hook still owns the
  // state shape + per-field updater (`setFilter`). No `onReset`
  // callback: agents-dashboard has no pagination cursor; filter
  // changes just re-run the client-side filter pass.
  const { filters, setFilter } = useFilters<{
    searchTerm: string
    statusFilter: string
  }>({
    initial: { searchTerm: '', statusFilter: 'all' },
  })
  const { searchTerm, statusFilter } = filters
  // Five row-action dialogs use the live-lookup useDialog<T> hook
  // (Candidate D, architecture review 2026-06-02). Each dialog stores
  // a key (agent_id / task_id) and asks the matching selector for the
  // current row on every render — background refresh, edits, and
  // terminations all flow into the open dialog automatically.
  //
  // For purge/terminate the "row" is just the agent_id string itself
  // (those dialogs only need the id, not the row); the selector is
  // therefore an identity function that surfaces the stored key as
  // data so the dialog body stays uniform with the others.
  const agentSelector = useCallback(
    (id: string | null) =>
      id ? data?.agents?.find((a) => a.agent_id === id) ?? null : null,
    [data?.agents],
  )
  const taskByIdSelector = useCallback(
    (id: string | null) =>
      id ? data?.tasks?.find((t) => t.task_id === id) ?? null : null,
    [data?.tasks],
  )
  const identitySelector = useCallback(
    (id: string | null) => id,
    [],
  )
  const taskDialog = useDialog<Task>(taskByIdSelector)
  const purgeDialog = useDialog<string>(identitySelector)       // holds purge-target agent_id
  const terminateDialog = useDialog<string>(identitySelector)   // holds terminate-target agent_id
  const editDialog = useDialog<Agent>(agentSelector)
  const detailDialog = useDialog<Agent>(agentSelector)

  // Deleted-while-open: if the agent or task vanishes from the source
  // (terminate from another tab, etc.), the live selector returns
  // null. Auto-close so the user isn't staring at an empty modal.
  // purge/terminate dialogs are skipped — their "row" is the id itself
  // and is never null while open.
  //
  // exhaustive-deps disabled for this block: useDialog returns a fresh
  // object each render, so we depend on its stable fields
  // (.isOpen/.data/.close) rather than the whole object. Listing the
  // object would re-run every render with no behavioural gain.
  /* eslint-disable react-hooks/exhaustive-deps */
  useEffect(() => {
    if (taskDialog.isOpen && taskDialog.data === null) taskDialog.close()
  }, [taskDialog.isOpen, taskDialog.data, taskDialog.close])
  useEffect(() => {
    if (editDialog.isOpen && editDialog.data === null) editDialog.close()
  }, [editDialog.isOpen, editDialog.data, editDialog.close])
  useEffect(() => {
    if (detailDialog.isOpen && detailDialog.data === null) detailDialog.close()
  }, [detailDialog.isOpen, detailDialog.data, detailDialog.close])
  /* eslint-enable react-hooks/exhaustive-deps */

  // Source list: include all agents (terminated rows need to surface so
  // admins can hit Restore/Purge on them). getActiveAgents() is kept
  // as the fallback for the "no terminated agents in this project"
  // case but extended with any terminated rows from data.agents.
  const allAgents = data?.agents || []
  const activeAgents = getActiveAgents()
  const terminatedAgents = allAgents.filter(
    (a) => a.status === 'terminated'
  )
  // Concat + dedupe by agent_id (active first, then terminated).
  const seen = new Set<string>()
  const agents = [...activeAgents, ...terminatedAgents].filter((a) => {
    if (seen.has(a.agent_id)) return false
    seen.add(a.agent_id)
    return true
  })
  const isConnected = !!activeServerId && activeServer?.status === 'connected'

  // Fetch data on mount and when server changes
  useEffect(() => {
    if (activeServerId && activeServer?.status === 'connected') {
      fetchAllData()
    }
  }, [activeServerId, activeServer?.status, fetchAllData])

  // Auto-cleanup loop removed (regression: silently terminated valid
  // worker agents every 2 minutes while the tab was open). Agent
  // termination is now strictly explicit user action via the Terminate
  // button. See tests/test_dashboard_no_auto_cleanup.py.

  const handleTaskClick = useCallback((task: Task) => {
    taskDialog.open(task.task_id)
  }, [taskDialog])

  const filteredAgents = agents.filter(agent => {
    const matchesSearch = agent.agent_id.toLowerCase().includes(searchTerm.toLowerCase()) ||
                         (agent.current_task && agent.current_task.toLowerCase().includes(searchTerm.toLowerCase()))
    // Wave 7 PR 2: filter on derived presence ('online' / 'offline'
    // / 'pending' / 'terminated') instead of the spawn-lifecycle
    // status column. The dropdown values below match
    // `AgentPresence`.
    const matchesStatus = statusFilter === 'all' || agentPresence(agent) === statusFilter
    return matchesSearch && matchesStatus
  })

  // Wave 7 PR 2 — stats reflect derived presence, matching the
  // badge + filter. "Online" = live MCP stream; "Pending" =
  // registered but never connected; "Offline" = was connected
  // previously, not now. "Total" still counts all visible rows
  // (active + terminated together) so the header lines up.
  const presenceOf = (a: Agent) => agentPresence(a)
  const stats = {
    total: agents.length,
    online: agents.filter(a => presenceOf(a) === 'online').length,
    pending: agents.filter(a => presenceOf(a) === 'pending').length,
    offline: agents.filter(a => presenceOf(a) === 'offline').length,
    // Terminated rows are counted in `total` but have no dedicated card;
    // surfacing the count on the Total card keeps the four numbers
    // reconcilable (total = online + pending + offline + terminated).
    terminated: agents.filter(a => presenceOf(a) === 'terminated').length,
    totalInSystem: allAgents.length,
  }

  // Wave 7 PR 3 (coordinator transition): ``handleCreateAgent`` is
  // gone. The dashboard registers agents through ``RegisterAgentModal``
  // which calls ``apiClient.registerAgent`` directly and renders the
  // minted token + .mcp.json snippet inline; there is no shared
  // mutation handler any more.
  //
  // Every mutation handler below funnels failures into the shared
  // ``toastError`` (architecture review Class 1) — the extracted
  // dialogs no longer carry their own ``setError`` banners. The
  // list-load error is owned by <DataTablePage>.

  const handleTerminateAgent = async (agentId: string) => {
    try {
      await apiClient.terminateAgent(agentId)
      await refreshData()
    } catch (error) {
      toastError(error, `Failed to terminate ${agentId}`)
    }
  }

  const handleRestoreAgent = useCallback(async (agentId: string) => {
    try {
      await apiClient.restoreAgent(agentId)
      await refreshData()
    } catch (error) {
      toastError(error, `Failed to restore ${agentId}`)
    }
  }, [refreshData])

  const handlePurgeAgent = useCallback((agentId: string) => {
    purgeDialog.open(agentId)
  }, [purgeDialog])

  // Edit save — the dialog owns the form + the diff; the page owns the
  // mutation. Re-throws so the dialog stays open with the operator's
  // edits intact (same split as Memories' EditMemoryModal).
  const handleSaveAgent = useCallback(
    async (agentId: string, updates: AgentEditUpdates) => {
      try {
        await apiClient.editAgent(agentId, updates)
        await refreshData()
      } catch (error) {
        toastError(error, `Failed to save ${agentId}`)
        throw error
      }
    },
    [refreshData],
  )

  // Disconnect / Reconnect — pause or resume monitoring without
  // terminating. The live-update SSE channel refetches on its own, but we
  // refreshData() here too so the row flips immediately on click.
  const handleDisconnectAgent = useCallback(async (agentId: string) => {
    try {
      const res = await apiClient.disconnectAgent(agentId)
      await refreshData()
      toastSuccess(`Agent "${agentId}" disconnected — monitoring paused.`)
      return res
    } catch (error) {
      toastError(error, `Failed to disconnect ${agentId}`)
    }
  }, [refreshData])

  const handleReconnectAgent = useCallback(async (agentId: string) => {
    try {
      await apiClient.reconnectAgent(agentId)
      await refreshData()
      toastSuccess(`Agent "${agentId}" reconnected — monitoring resumed.`)
    } catch (error) {
      toastError(error, `Failed to reconnect ${agentId}`)
    }
  }, [refreshData])

  const handleDisconnectAll = async () => {
    try {
      await apiClient.disconnectAllAgents()
      await refreshData()
      toastSuccess('All agents disconnected — global monitoring paused.')
    } catch (error) {
      toastError(error, 'Failed to disconnect all agents')
    }
  }

  const handleReconnectAll = async () => {
    try {
      await apiClient.reconnectAllAgents()
      await refreshData()
      toastSuccess('All agents reconnected — global monitoring resumed.')
    } catch (error) {
      toastError(error, 'Failed to reconnect all agents')
    }
  }

  // Row-click handler. Opens a confirmation dialog before firing the
  // actual terminate; the in-effect idle-cleanup path uses
  // handleTerminateAgent directly so it stays bypass-confirm.
  const handleTerminateConfirm = useCallback((agentId: string) => {
    terminateDialog.open(agentId)
  }, [terminateDialog])

  const handleEditAgent = useCallback((agent: Agent) => {
    editDialog.open(agent.agent_id)
  }, [editDialog])

  const handleSelectAgent = useCallback((agent: Agent) => {
    detailDialog.open(agent.agent_id)
  }, [detailDialog])

  // Send-directive (ad-hoc poke) modal state. Agent-centric action:
  // reachable for ANY agent from its row / detail dialog, independent of
  // whether it has a schedule. `directiveAgentId` is the locked target.
  const [directiveAgentId, setDirectiveAgentId] = useState<string | null>(null)
  const [directiveOpen, setDirectiveOpen] = useState(false)
  const handleSendDirective = useCallback((agentId: string) => {
    setDirectiveAgentId(agentId)
    setDirectiveOpen(true)
  }, [])

  // One column spec drives BOTH the desktop table and (via
  // renderMobileCard) the mobile card list — <ResponsiveDataTable>
  // renders the twin guard.
  const columns = useAgentColumns({
    onTerminate: handleTerminateConfirm,
    onRestore: handleRestoreAgent,
    onPurge: handlePurgeAgent,
    openView: handleSelectAgent,
    onEdit: handleEditAgent,
    onTaskClick: handleTaskClick,
    onSendDirective: handleSendDirective,
    onDisconnect: handleDisconnectAgent,
    onReconnect: handleReconnectAgent,
  })

  // Stats strip — Wave 7 PR 2: presence-driven counters. "Online"
  // replaces the legacy "Running"; "Pending" now means "registered
  // but no MCP session yet" (paste the snippet to bring it online);
  // "Offline" replaces "Failed" (was previously connected).
  const statsCards: StatsCardProps[] = [
    {
      icon: Users,
      label: 'Total',
      value: stats.total,
      change:
        stats.terminated > 0
          ? `${stats.terminated} terminated`
          : stats.total > 0 ? `${stats.online} online` : undefined,
      trend: 'neutral',
    },
    {
      icon: CheckCircle2,
      label: 'Online',
      value: stats.online,
      change: stats.total > 0 ? `${Math.round((stats.online / stats.total) * 100)}%` : '0%',
      trend: 'up',
    },
    {
      icon: Clock,
      label: 'Pending',
      value: stats.pending,
      change: stats.pending > 0 ? 'Awaiting paste' : 'None',
      trend: 'neutral',
    },
    {
      icon: AlertCircle,
      label: 'Offline',
      value: stats.offline,
      change: stats.offline > 0 ? 'Idle/disconnected' : 'None',
      trend: 'neutral',
    },
  ]

  const filterBar = (
    <>
      <div className="relative flex-1 sm:max-w-sm">
        <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
        <Input
          placeholder="Search agents..."
          value={searchTerm}
          onChange={(e) => setFilter("searchTerm", e.target.value)}
          className="pl-10 bg-background border-border text-foreground placeholder:text-muted-foreground focus:border-primary/50 focus:ring-primary/20 transition-all"
        />
      </div>
      <Select value={statusFilter} onValueChange={(v) => setFilter("statusFilter", v)}>
        <SelectTrigger className="w-full sm:w-32 bg-background border-border text-foreground">
          <SelectValue />
        </SelectTrigger>
        {/* Wave 7 PR 2 — filter on derived presence. Matches
            `AgentPresence` in `lib/api.ts`. */}
        <SelectContent className="bg-background border-border">
          <SelectItem value="all">All Status</SelectItem>
          <SelectItem value="online">Online</SelectItem>
          <SelectItem value="pending">Pending</SelectItem>
          <SelectItem value="offline">Offline</SelectItem>
          <SelectItem value="terminated">Terminated</SelectItem>
        </SelectContent>
      </Select>
    </>
  )

  // Fleet master switch — "we're done for now" / "we're back".
  // Disconnect all flips the GLOBAL event-loop toggle OFF (every
  // agent's wait_for_events returns stop_listening) + closes every
  // live stream; Reconnect all flips it back ON. Both reversible;
  // per-agent disconnects persist through a global reconnect.
  const headerActions = (
    <>
      <Button
        variant="outline"
        size="sm"
        onClick={handleDisconnectAll}
        className="text-xs text-muted-foreground hover:text-amber-600"
        title="Disconnect ALL agents — pause fleet-wide monitoring now (reversible)"
        data-testid="disconnect-all"
      >
        <PowerOff className="h-3.5 w-3.5 mr-1.5" />
        Disconnect all
      </Button>
      <Button
        variant="outline"
        size="sm"
        onClick={handleReconnectAll}
        className="text-xs text-muted-foreground hover:text-primary"
        title="Reconnect ALL agents — resume fleet-wide monitoring"
        data-testid="reconnect-all"
      >
        <Power className="h-3.5 w-3.5 mr-1.5" />
        Reconnect all
      </Button>
      {/* Wave 7 coordinator transition: register-only modal is
          the sole agent-creation surface. The legacy
          spawn-via-tmux ``CreateAgentModal`` was deleted in PR 3. */}
      <RegisterAgentModal />
    </>
  )

  return (
    <DataTablePage<Agent>
        guard={
          !isConnected
            ? {
                icon: Network,
                title: 'No Server Connection',
                description: 'Connect to an MCP server to manage agents',
              }
            : null
        }
        loading={loading}
        error={error}
        header={{
          title: 'Agent Fleet',
          subtitle: 'Monitor and manage autonomous agents',
          serverName: activeServer?.name,
          lastUpdated: data?.timestamp,
          onRefresh: refreshData,
          refreshing: loading,
          actions: headerActions,
        }}
        stats={statsCards}
        filterBar={filterBar}
        columns={columns}
        rows={filteredAgents}
        getRowId={(a) => a.agent_id}
        onRowClick={handleSelectAgent}
        // `table-fixed` + the min-width floor that goes with it; both
        // are derived from the column widths, so they live next to
        // them in agent-columns.tsx rather than being restated here.
        tableClassName={AGENTS_TABLE_CLASS}
        renderMobileCard={(agent) => (
          <AgentMobileCard
            agent={agent}
            openView={handleSelectAgent}
            onEdit={handleEditAgent}
            onTerminate={handleTerminateConfirm}
            onRestore={handleRestoreAgent}
            onPurge={handlePurgeAgent}
            onSendDirective={handleSendDirective}
            onDisconnect={handleDisconnectAgent}
            onReconnect={handleReconnectAgent}
          />
        )}
        empty={{
          icon: Users,
          title: 'No agents found',
          description:
            agents.length === 0
              ? "Add your first agent to get started."
              : "No agents match your current filters.",
          action:
            agents.length === 0 ? (
              <div className="flex flex-col sm:flex-row gap-2">
                {/* Wave 7 PR 3: register-only modal is the sole
                    creation surface; the legacy spawn modal is gone. */}
                <RegisterAgentModal />
              </div>
            ) : undefined,
        }}
      >
        {/* Agent Detail Modal — replaces the old sidebar drawer */}
        <AgentDetailDialog
          agent={detailDialog.data}
          open={detailDialog.isOpen}
          onOpenChange={(open) => {
            if (!open) {
              detailDialog.close()
            }
          }}
          onTaskClick={(task) => {
            handleTaskClick(task)
          }}
          onEdit={() => {
            const agent = detailDialog.data
            if (!agent) return
            detailDialog.close()
            handleEditAgent(agent)
          }}
          onTerminate={() => {
            const agent = detailDialog.data
            if (!agent) return
            detailDialog.close()
            handleTerminateConfirm(agent.agent_id)
          }}
          onPurge={() => {
            const agent = detailDialog.data
            if (!agent) return
            detailDialog.close()
            handlePurgeAgent(agent.agent_id)
          }}
          onSendDirective={() => {
            const agent = detailDialog.data
            if (!agent) return
            detailDialog.close()
            handleSendDirective(agent.agent_id)
          }}
        />

        {/* Edit Agent Dialog */}
        <EditAgentDialog
          agent={editDialog.data}
          open={editDialog.isOpen}
          onOpenChange={(open) => {
            if (!open) editDialog.close()
          }}
          onSave={handleSaveAgent}
        />

        {/* Terminate confirmation dialog (soft-delete) */}
        <TerminateAgentDialog
          agentId={terminateDialog.data}
          open={terminateDialog.isOpen}
          onOpenChange={(open) => {
            if (!open) terminateDialog.close()
          }}
          onConfirmed={async (agentId) => {
            await handleTerminateAgent(agentId)
          }}
        />

        {/* Task Details Dialog */}
        <TaskDetailsDialog
          task={taskDialog.data}
          open={taskDialog.isOpen}
          onOpenChange={(open) => {
            if (!open) taskDialog.close()
          }}
        />

        {/* Purge confirmation dialog (cascade tombstone + DELETE) */}
        <PurgeAgentDialog
          agentId={purgeDialog.data}
          open={purgeDialog.isOpen}
          onOpenChange={(open) => {
            if (!open) purgeDialog.close()
          }}
          onConfirmed={() => {
            void refreshData()
          }}
        />

        {/* Send-directive (ad-hoc poke) modal — shared with the Schedules
            page. Locked to the row/detail agent so any agent is pokeable
            straight from the Agents page, no schedule required. */}
        <SendDirectiveModal
          open={directiveOpen}
          onOpenChange={setDirectiveOpen}
          lockedAgentId={directiveAgentId}
        />
      </DataTablePage>
  )
}
