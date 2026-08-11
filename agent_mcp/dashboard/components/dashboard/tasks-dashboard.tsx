"use client"

import { useState, useEffect, useCallback, useMemo } from "react"
import {
  CheckSquare, Clock, AlertCircle,
  Search, Plus, X, CheckCircle2, Target, Zap,
} from "lucide-react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { apiClient, Task, type TaskFilters } from "@/lib/api"
import { useServerStore } from "@/lib/stores/server-store"
import { useSseHealthy } from "@/lib/stores/data-store"
import { useDialog } from "@/hooks/use-dialog"
import { useFilters } from "@/hooks/use-filters"
import { usePagedQuery } from "@/hooks/use-paged-query"
import { AgentSelect } from "@/components/dashboard/shared/agent-select"
import { TaskMobileCard } from "@/components/dashboard/tasks-mobile-list"
import { DataTablePage } from "@/components/dashboard/shared/data-table-page"
import type { StatsCardProps } from "@/components/dashboard/shared/stats-card"
import { CreateTaskModal } from "@/components/dashboard/tasks/create-task-modal"
import { ViewTaskDialog } from "@/components/dashboard/tasks/view-task-dialog"
import { EditTaskDialog } from "@/components/dashboard/tasks/edit-task-dialog"
import { DeleteTaskDialog } from "@/components/dashboard/tasks/delete-task-dialog"
import { TasksPagination } from "@/components/dashboard/tasks/tasks-pagination"
import { useTasksColumns } from "@/components/dashboard/tasks/use-tasks-columns"

// PF-1 clamp (Wave 3): GET /tasks returns the WHOLE task set with no
// server-side pagination, so the client bounds the rendered list. Same
// page size messages-dashboard uses for its server-paged list, applied
// here as an in-memory window over `filteredTasks`.
const PAGE_SIZE = 100

// Background-refresh interval for the tasks listing. The 60s interval
// keeps the "background refresh while tab open" semantic from
// pre-migration ``useTasksData`` — the hook itself is reactive only,
// not interval-driven, so we still wire the timer here.
//
// No ``cacheMs`` (was 30s pre task-filters): the GET /tasks fetch is
// now parameterized by the server-side filter snapshot and re-runs via
// the hook's ``deps`` when a filter changes. A global TTL on the last
// response would make ``usePagedQuery`` short-circuit that reactive
// re-fetch (see its cache-hit guard) and silently serve the *previous*
// filter's rows — so the filtered fetch must not be cached. The 60s
// background refresh still forces a fetch (``refresh()`` bypasses any
// cache), and the reactive effect only fires on server / filter
// changes, so there's nothing left for a TTL to dedupe here.
const REFRESH_INTERVAL = 60000 // 1 minute for background refresh

// Tasks-dashboard data hook — delegates the loading/error/lastFetch/
// refresh state machine to ``usePagedQuery<Task>`` (PR 5 of the
// 2026-06-09 architecture review). The underlying request still
// goes through ``apiClient.getTasks()`` (a GET ``/tasks`` that
// returns ``Task[]`` directly — no envelope, no pagination, no token
// in body), so we use the hook's ``fetchFn`` escape hatch to wrap
// it. The escape hatch threads the AbortSignal into the response so
// a slow stale fetch can't overwrite a fresh fast one.
const useTasksData = (serverFilters: TaskFilters) => {
  const { activeServerId, servers } = useServerStore()
  const activeServer = servers.find(s => s.id === activeServerId)
  const isConnected = !!activeServerId && activeServer?.status === 'connected'

  // PF-3: the live SSE stream's health. While healthy, the slow
  // background poll below is skipped — the mcp:resources-updated refetch
  // covers new task activity; the interval is only a fallback for when
  // SSE is down.
  const sseHealthy = useSseHealthy()

  // Disconnected: surface an empty result without firing the fetch
  // (the wrapper short-circuits via fetchFn returning empty). The
  // hook's state machine still runs — that's fine, it just stays at
  // {data: [], total: 0, loading: false, error: null}.
  //
  // ``serverFilters`` (status / assignment / creator) drives the
  // server-side filtered GET /tasks — the single source of truth
  // shared with the backend + the MCP view_tasks tool. Its serialized
  // form is threaded into ``deps`` below so a filter change re-runs
  // the fetch.
  const fetchFn = useCallback(
    async (_signal: AbortSignal): Promise<{ data: Task[]; total: number }> => {
      if (!isConnected) {
        return { data: [], total: 0 }
      }
      try {
        const tasksData = await apiClient.getTasks(serverFilters)
        return { data: tasksData, total: tasksData.length }
      } catch (err) {
        // Pre-migration ``useTasksData`` silenced 'NO_SERVER_CONNECTED'
        // errors so a transient disconnect didn't paint a red banner
        // (the DashboardWrapper handles connection state). Preserve
        // that quirk by returning empty instead of throwing.
        if (err instanceof Error && err.message === 'NO_SERVER_CONNECTED') {
          return { data: [], total: 0 }
        }
        throw err
      }
    },
    [isConnected, serverFilters],
  )

  // Stable serialization of the filter snapshot for the reactive
  // re-fetch dependency — a fresh object identity every render would
  // thrash the effect; the value change is what must re-fetch.
  const serverFiltersKey = JSON.stringify(serverFilters)

  const {
    data: tasks,
    loading,
    error: queryError,
    refresh,
    lastFetch,
  } = usePagedQuery<Task>({
    fetchFn,
    deps: [activeServerId, serverFiltersKey],
  })

  // The pre-migration shape exposed ``error`` as ``string | null``.
  // The hook returns a real ``Error | null``; map it for backward
  // compat with the consumer below.
  const error: string | null = queryError ? queryError.message : null

  // Background refresh — separate from the hook (which is reactive
  // only). Bypass the cache by calling ``refresh()`` directly; the
  // hook treats refresh() as a force-fetch. (PF-3) skipped entirely
  // while SSE is healthy — the live refetch below covers that case.
  useEffect(() => {
    if (sseHealthy) return
    const interval = setInterval(() => {
      refresh()
    }, REFRESH_INTERVAL)
    return () => clearInterval(interval)
  }, [refresh, sseHealthy])

  // Live refetch on backend mutation. The operator SSE client
  // (lib/mcp-notifications.ts) dispatches a debounced
  // ``mcp:resources-updated`` window event on every
  // ``notifications/resources/updated``. The tasks list fetches its OWN
  // GET /tasks endpoint (not the data-store all-data envelope), so
  // scheduleDashboardRefresh doesn't cover it — hook the event to the
  // force-fetch refresh (backend + client already debounce).
  useEffect(() => {
    if (typeof window === "undefined") return
    const handler = () => {
      refresh()
    }
    window.addEventListener("mcp:resources-updated", handler)
    return () => window.removeEventListener("mcp:resources-updated", handler)
  }, [refresh])

  // ``lastFetch`` is ``number | null`` from the hook; the consumer
  // below assumes ``number`` (it checks ``> 0``). Coalesce.
  return useMemo(() => ({
    tasks,
    loading,
    error,
    refresh,
    lastFetch: lastFetch ?? 0,
    isConnected,
  }), [tasks, loading, error, refresh, lastFetch, isConnected])
}

export function TasksDashboard() {
  const { servers, activeServerId } = useServerStore()
  const activeServer = servers.find(s => s.id === activeServerId)
  // Filter state — owned by useFilters (PR 4 of the 2026-06-09
  // architecture review). Two classes of filter live here:
  //
  //   * Client-side (title/description text search + priority): the
  //     rows are already in memory, so these narrow the list locally
  //     via the memoised `filteredTasks` pass below. Priority is NOT
  //     part of the GET /tasks contract, so it stays client-side.
  //
  //   * Server-side (status / assignment / creator): these drive the
  //     GET /tasks query params — the single source of truth shared
  //     with the backend + the MCP view_tasks tool. They are lifted
  //     into `serverFilters` (a TaskFilters) and threaded into the
  //     fetch, so the server does the filtering (and the "incomplete"
  //     status alias, which can't be expressed by matching a single
  //     stored status, resolves server-side).
  //
  // `clearAll` backs the "Clear" affordance; `isActive` toggles it.
  const { filters, setFilter, clearAll, isActive } = useFilters<{
    searchTerm: string
    priorityFilter: string
    statusFilter: string
    assignment: string
    createdBy: string
  }>({
    initial: {
      searchTerm: '',
      priorityFilter: 'all',
      statusFilter: 'all',
      assignment: 'all',
      createdBy: '',
    },
  })
  const { searchTerm, priorityFilter, statusFilter, assignment, createdBy } = filters

  // Lift the server-side dimensions into the TaskFilters shape the
  // getTasks() query-string builder consumes. Assignment uses the
  // dedicated `assigned` / `unassigned` booleans — never a magic
  // assigned_to="unassigned" value — so an agent named "unassigned"
  // can't collide with the claimable-pool filter.
  const serverFilters = useMemo<TaskFilters>(() => {
    const f: TaskFilters = {}
    if (statusFilter !== 'all') f.status = statusFilter
    if (assignment === 'assigned') f.assigned = true
    else if (assignment === 'unassigned') f.unassigned = true
    const cb = createdBy.trim()
    if (cb) f.created_by = cb
    return f
  }, [statusFilter, assignment, createdBy])

  const { tasks, loading, error, refresh, lastFetch, isConnected } = useTasksData(serverFilters)

  // Create modal — parent-controlled open (the header + empty-state
  // "Create Task" buttons drive it), rendered once as a child below.
  const [createOpen, setCreateOpen] = useState(false)

  // Row-action dialog state. Each holds the **task_id** of the task
  // being viewed / edited / deleted via the live-lookup useDialog<T>
  // hook (Candidate D, architecture review 2026-06-02). The dialog
  // body reads `dialog.data` which is recomputed on every render by
  // the selector below — so background refresh, edits saved from the
  // sibling Edit dialog, and tombstoning the row all flow through
  // immediately.
  const taskSelector = useCallback(
    (id: string | null) => (id ? tasks.find(t => t.task_id === id) ?? null : null),
    [tasks],
  )
  const viewDialog = useDialog<Task>(taskSelector)
  const editDialog = useDialog<Task>(taskSelector)
  const deleteDialog = useDialog<Task>(taskSelector)

  // Deleted-while-open: if the row vanishes from the source under us
  // (background refresh sees a delete from another tab, or a sibling
  // dialog deletes the same task), the live selector returns null.
  // Auto-close the dialog so the user isn't stuck staring at an
  // empty modal; the row's gone, no point keeping the modal up.
  //
  // exhaustive-deps disabled for this block: useDialog returns a fresh
  // object each render, so we depend on its stable fields
  // (.isOpen/.data/.close) rather than the whole object. Listing the
  // object would re-run every render with no behavioural gain.
  /* eslint-disable react-hooks/exhaustive-deps */
  useEffect(() => {
    if (viewDialog.isOpen && viewDialog.data === null) viewDialog.close()
  }, [viewDialog.isOpen, viewDialog.data, viewDialog.close])
  useEffect(() => {
    if (editDialog.isOpen && editDialog.data === null) editDialog.close()
  }, [editDialog.isOpen, editDialog.data, editDialog.close])
  useEffect(() => {
    if (deleteDialog.isOpen && deleteDialog.data === null) deleteDialog.close()
  }, [deleteDialog.isOpen, deleteDialog.data, deleteDialog.close])
  /* eslint-enable react-hooks/exhaustive-deps */

  // Client-side narrowing only. Status + assignment + creator are
  // already applied server-side (see `serverFilters`), so `tasks` is
  // the pre-filtered set; here we only apply the in-memory text search
  // and the priority filter (priority isn't part of the GET /tasks
  // contract).
  const filteredTasks = useMemo(() => {
    return tasks.filter(task => {
      const matchesSearch = task.title.toLowerCase().includes(searchTerm.toLowerCase()) ||
                           (task.description && task.description.toLowerCase().includes(searchTerm.toLowerCase()))
      const matchesPriority = priorityFilter === 'all' || task.priority === priorityFilter
      return matchesSearch && matchesPriority
    })
  }, [tasks, searchTerm, priorityFilter])

  // PF-1 clamp — bound the rendered list to one PAGE_SIZE window. The
  // whole task set is already in memory (`filteredTasks`); this only
  // caps how many rows reach the DOM at once.
  const [currentOffset, setCurrentOffset] = useState(0)

  // Reset to the first page whenever the filtered set changes (a new
  // search/priority narrowing, or a server refetch shrinks the list) so
  // the offset can't strand the user past the end on an empty page.
  useEffect(() => {
    setCurrentOffset(0)
  }, [searchTerm, priorityFilter, statusFilter, assignment, createdBy])

  const totalFiltered = filteredTasks.length
  // Clamp the offset defensively in case the list shrank between renders
  // before the reset effect runs.
  const safeOffset =
    currentOffset >= totalFiltered
      ? Math.max(0, Math.floor(Math.max(0, totalFiltered - 1) / PAGE_SIZE) * PAGE_SIZE)
      : currentOffset
  const pagedTasks = useMemo(
    () => filteredTasks.slice(safeOffset, safeOffset + PAGE_SIZE),
    [filteredTasks, safeOffset],
  )

  const onFirstPage = safeOffset === 0
  const onLastPage = safeOffset + PAGE_SIZE >= totalFiltered
  const rangeStart = totalFiltered === 0 ? 0 : safeOffset + 1
  const rangeEnd = Math.min(safeOffset + PAGE_SIZE, totalFiltered)
  const goNewest = () => setCurrentOffset(0)
  const goNewer = () => setCurrentOffset(Math.max(0, safeOffset - PAGE_SIZE))
  const goOlder = () => setCurrentOffset(safeOffset + PAGE_SIZE)
  const goOldest = () =>
    setCurrentOffset(
      Math.floor(Math.max(0, totalFiltered - 1) / PAGE_SIZE) * PAGE_SIZE,
    )

  // Memoize stats calculation
  const stats = useMemo(() => {
    const total = tasks.length
    const in_progress = tasks.filter(t => t.status === 'in_progress').length
    const pending = tasks.filter(t => t.status === 'pending').length
    const completed = tasks.filter(t => t.status === 'completed').length
    const failed = tasks.filter(t => t.status === 'failed').length
    // Any status without a dedicated card (unassigned, cancelled, …) is in
    // `total` but not in the four cards; surface the remainder on the Total
    // card so the numbers reconcile
    // (total = in_progress + pending + completed + failed + other).
    const other = Math.max(0, total - in_progress - pending - completed - failed)
    return { total, in_progress, pending, completed, failed, other }
  }, [tasks])

  // Row-action handlers. Each opens the matching Dialog. openView
  // is used by BOTH the eye icon and the row-body click. We forward
  // the stable hook .open methods directly.
  const openView = viewDialog.open
  const openEdit = editDialog.open
  const openDelete = deleteDialog.open
  const handleEditSaved = useCallback(() => { refresh() }, [refresh])
  const handleDeleted = useCallback(() => { refresh() }, [refresh])
  const handleCreated = useCallback(() => { refresh() }, [refresh])

  // Stats strip — rendered by the scaffold's <StatsCard> row.
  const statsCards: StatsCardProps[] = [
    {
      icon: Target,
      label: 'Total',
      value: stats.total,
      change:
        stats.other > 0
          ? `${stats.other} other`
          : stats.total > 0 ? `${stats.in_progress} active` : undefined,
      trend: 'neutral',
    },
    {
      icon: Zap,
      label: 'Active',
      value: stats.in_progress,
      change: stats.total > 0 ? `${Math.round((stats.in_progress / stats.total) * 100)}%` : '0%',
      trend: 'up',
    },
    {
      icon: Clock,
      label: 'Pending',
      value: stats.pending,
      change: stats.pending > 0 ? 'Queued' : 'None',
      trend: 'neutral',
    },
    {
      icon: CheckCircle2,
      label: 'Completed',
      value: stats.completed,
      change: stats.total > 0 ? `${Math.round((stats.completed / stats.total) * 100)}% done` : '0%',
      trend: 'up',
    },
    {
      icon: AlertCircle,
      label: 'Failed',
      value: stats.failed,
      change: stats.failed > 0 ? 'Need review' : 'All good',
      trend: stats.failed > 0 ? 'down' : 'neutral',
    },
  ]

  // Column spec — one source for the desktop table (via
  // <ResponsiveDataTable>) and the mobile twin. Extracted to
  // `useTasksColumns` (Wave 5, mirrors `useMessagesColumns`).
  const columns = useTasksColumns({ openView, openEdit, openDelete })

  /*
    Controls — un-boxed filter row matching Messages/Memories (no Card
    wrapper). Search + Priority narrow the in-memory rows client-side;
    Status + Assignment + Created-by drive the server-side GET /tasks
    query (single source of truth).

    The inner flex wrapper keeps this page's `sm:flex-wrap` — six
    controls don't fit one desktop row, and the scaffold's filter-bar
    slot is a plain non-wrapping row (right for the 1-2 control pages).
  */
  const filterBar = (
    <div className="flex flex-col sm:flex-row sm:flex-wrap items-stretch sm:items-center gap-2 sm:gap-3 w-full">
      <div className="relative flex-1 sm:max-w-xs">
        {/* CC-1/CC-13 audit 2026-06-02: Search + Select migrated to
            shadcn semantic tokens. Now relies on the Input/Select
            primitives' built-in focus-visible:ring-ring/50 styles. */}
        <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
        <Input
          aria-label="Search tasks"
          placeholder="Search tasks..."
          value={searchTerm}
          onChange={(e) => setFilter("searchTerm", e.target.value)}
          className="pl-10"
        />
      </div>
      {/* Status (server-side). "Incomplete (open)" sends
          status=incomplete — the backend alias for every non-terminal
          task (pending + in_progress). */}
      <Select value={statusFilter} onValueChange={(v) => setFilter("statusFilter", v)}>
        <SelectTrigger className="w-full sm:w-44">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="all">Any status</SelectItem>
          <SelectItem value="incomplete">Incomplete (open)</SelectItem>
          <SelectItem value="pending">Pending</SelectItem>
          <SelectItem value="in_progress">In Progress</SelectItem>
          <SelectItem value="completed">Completed</SelectItem>
          <SelectItem value="cancelled">Cancelled</SelectItem>
          <SelectItem value="failed">Failed</SelectItem>
        </SelectContent>
      </Select>
      {/* Assignment (server-side). Maps to the dedicated
          assigned=true / unassigned=true booleans — never a magic
          assigned_to="unassigned" value. */}
      <Select value={assignment} onValueChange={(v) => setFilter("assignment", v)}>
        <SelectTrigger className="w-full sm:w-40">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="all">Any assignment</SelectItem>
          <SelectItem value="assigned">Assigned</SelectItem>
          <SelectItem value="unassigned">Unassigned</SelectItem>
        </SelectContent>
      </Select>
      {/* Created by (server-side) → created_by. Live-agents picker
          (shared <AgentSelect>); noneLabel="— Any —" means no filter. */}
      <div className="w-full sm:w-44">
        <AgentSelect
          value={createdBy || null}
          onChange={(v) => setFilter("createdBy", v ?? "")}
          noneLabel="— Any —"
          placeholder="created by"
        />
      </div>
      {/* Priority (client-side — not part of the GET /tasks contract). */}
      <Select value={priorityFilter} onValueChange={(v) => setFilter("priorityFilter", v)}>
        <SelectTrigger className="w-full sm:w-32">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="all">Any priority</SelectItem>
          <SelectItem value="high">High</SelectItem>
          <SelectItem value="medium">Medium</SelectItem>
          <SelectItem value="low">Low</SelectItem>
        </SelectContent>
      </Select>
      {isActive && (
        <Button variant="ghost" size="sm" onClick={clearAll}>
          <X className="h-4 w-4 mr-1" />
          Clear
        </Button>
      )}
    </div>
  )

  return (
    <DataTablePage<Task>
      guard={
        !isConnected
          ? {
              icon: CheckSquare,
              title: 'No Server Connection',
              description: 'Connect to an MCP server to manage tasks',
            }
          : null
      }
      loading={loading}
      error={error}
      header={{
        title: 'Task Operations',
        subtitle: 'Orchestrate and monitor autonomous tasks',
        serverName: activeServer?.name,
        lastUpdated: lastFetch > 0 ? lastFetch : undefined,
        onRefresh: refresh,
        refreshing: loading,
        actions: (
          <Button
            size="sm"
            className="bg-primary hover:bg-primary/90 text-primary-foreground transition-colors duration-150"
            onClick={() => setCreateOpen(true)}
          >
            <Plus className="h-4 w-4 mr-1.5" />
            Create Task
          </Button>
        ),
      }}
      stats={statsCards}
      filterBar={filterBar}
      columns={columns}
      rows={pagedTasks}
      getRowId={(task) => task.task_id}
      onRowClick={(task) => openView(task.task_id)}
      renderMobileCard={(task) => (
        <TaskMobileCard
          task={task}
          openView={openView}
          openEdit={openEdit}
          openDelete={openDelete}
        />
      )}
      skeletonRows={6}
      empty={{
        icon: CheckSquare,
        title: 'No tasks found',
        description:
          tasks.length === 0
            ? "Create your first task to get started."
            : "No tasks match your current filters.",
        action:
          tasks.length === 0
            ? (
              <Button size="sm" onClick={() => setCreateOpen(true)}>
                <Plus className="h-4 w-4 mr-1.5" />
                Create Task
              </Button>
            )
            : undefined,
      }}
    >
      {/* PF-1 clamp footer — only when the list spills past one page.
          Renders directly beneath the table (DataTablePage drops its
          children there). */}
      {totalFiltered > PAGE_SIZE && (
        <div className="mt-4">
          <TasksPagination
            rangeStart={rangeStart}
            rangeEnd={rangeEnd}
            total={totalFiltered}
            onFirstPage={onFirstPage}
            onLastPage={onLastPage}
            onNewest={goNewest}
            onNewer={goNewer}
            onOlder={goOlder}
            onOldest={goOldest}
          />
        </div>
      )}

      {/* Create modal — shared <FormDialog> + useAsyncSubmit shell
          (Wave 5). Opened from the header + empty-state buttons. */}
      <CreateTaskModal
        open={createOpen}
        onOpenChange={setCreateOpen}
        onCreated={handleCreated}
      />

      {/* Row-action dialogs (View / Edit / Delete) — Dialog modals,
          NOT the sidebar Sheet. */}
      <ViewTaskDialog
        task={viewDialog.data}
        onOpenChange={(open) => { if (!open) viewDialog.close() }}
        onEdit={() => {
          const task = viewDialog.data
          if (!task) return
          viewDialog.close()
          openEdit(task.task_id)
        }}
        onDelete={() => {
          const task = viewDialog.data
          if (!task) return
          viewDialog.close()
          openDelete(task.task_id)
        }}
      />
      <EditTaskDialog
        task={editDialog.data}
        onOpenChange={(open) => { if (!open) editDialog.close() }}
        onSaved={handleEditSaved}
      />
      <DeleteTaskDialog
        task={deleteDialog.data}
        onOpenChange={(open) => { if (!open) deleteDialog.close() }}
        onDeleted={handleDeleted}
      />
    </DataTablePage>
  )
}
