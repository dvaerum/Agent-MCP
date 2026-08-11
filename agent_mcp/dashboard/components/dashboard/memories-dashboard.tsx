"use client"

import React, { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Brain,
  Search,
  Plus,
  Pencil,
  Trash2,
  Eye,
  AlertCircle,
  CheckCircle2,
  Clock,
  Database,
  Network
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Badge } from '@/components/ui/badge'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { useAllData, useAllDataStatus } from '@/lib/queries/all-data'
import { useServerStore } from '@/lib/stores/server-store'
import { useDialog } from '@/hooks/use-dialog'
import { useFilters } from '@/hooks/use-filters'
import { apiClient, type Memory, type RawContextEntry } from '@/lib/api'
import { toastError, toastSuccess } from '@/components/ui/toast'
import { decodeMemoryValue, memoryValuePreview } from '@/lib/memory-value'
import { CreateMemoryModal } from './modals/create-memory-modal'
import { ViewMemoryModal } from './modals/view-memory-modal'
import { EditMemoryModal } from './modals/edit-memory-modal'
import { ConfirmActionModal } from './modals/confirm-action-modal'
import { MemoryMobileCard } from '@/components/dashboard/memories-mobile-list'
import {
  DataTablePage,
} from '@/components/dashboard/shared/data-table-page'
import type { StatsCardProps } from '@/components/dashboard/shared/stats-card'
import type { Column } from '@/components/dashboard/shared/responsive-data-table'

// ---------------------------------------------------------------------
// Cell helpers (were inline in the pre-foundation <MemoryRow>).
// ---------------------------------------------------------------------

const formatKey = (key: string) =>
  key.length > 25 ? key.substring(0, 25) + '...' : key

const valueTooltip = (value: unknown): string =>
  typeof value === 'string' ? value : JSON.stringify(value)

const formatDeleteValue = (value: unknown) => {
  if (typeof value === 'string') {
    return value.length > 50 ? value.substring(0, 50) + '...' : value
  }
  const jsonStr = JSON.stringify(value, null, 2)
  return jsonStr.length > 50 ? jsonStr.substring(0, 50) + '...' : jsonStr
}

// Preview block shown inside the confirm modal's `details` slot —
// reproduces the pre-foundation DeleteMemoryModal body (KEY /
// DESCRIPTION / VALUE PREVIEW / metadata). It is also what makes a
// memory delete tier-1 material: the value is on screen at the moment
// of confirmation, so the row is recreatable by copy/paste.
function MemoryDeletePreview({ memory }: { memory: Memory }) {
  return (
    <div className="space-y-3">
      <div className="text-sm font-medium text-foreground">Memory to be deleted:</div>
      <div className="bg-muted/30 border border-border rounded-lg p-3 space-y-3">
        <div>
          <div className="text-xs font-medium text-muted-foreground mb-1">KEY</div>
          <code className="text-sm font-mono text-foreground bg-background border border-border rounded px-2 py-1 block">
            {memory.context_key}
          </code>
        </div>
        {memory.description && (
          <div>
            <div className="text-xs font-medium text-muted-foreground mb-1">DESCRIPTION</div>
            <div className="text-sm text-foreground">{memory.description}</div>
          </div>
        )}
        <div>
          <div className="text-xs font-medium text-muted-foreground mb-1">VALUE PREVIEW</div>
          <div className="text-sm text-muted-foreground bg-background border border-border rounded px-2 py-1 font-mono max-h-16 overflow-hidden">
            {formatDeleteValue(memory.value)}
          </div>
        </div>
        <div className="flex items-center gap-4 text-xs text-muted-foreground pt-2 border-t border-border">
          <span>Updated: {new Date(memory.updated_at).toLocaleDateString()}</span>
          <span>By: {memory.updated_by}</span>
          {memory._metadata && <span>Size: {memory._metadata.size_kb} KB</span>}
        </div>
      </div>
    </div>
  )
}

export function MemoriesDashboard() {
  const { servers, activeServerId } = useServerStore()
  const activeServer = servers.find(s => s.id === activeServerId)
  // Wave 6 keystone increment 1: memories read the `context` slice of
  // the shared `/all-data` TanStack Query (memory rows are context
  // rows). `refresh` is the awaitable force-refetch the mutation
  // handlers await after create/edit/delete.
  const data = useAllData()
  const { loading, error, refresh: refreshData } = useAllDataStatus()
  // Filter state — owned by the shared useFilters hook (matches Agents/
  // Tasks). Search is a filter; sort is a separate control (useFilters
  // covers filter fields, not sort order).
  const { filters, setFilter } = useFilters<{ searchTerm: string }>({
    initial: { searchTerm: '' },
  })
  const { searchTerm } = filters
  const [sortBy, setSortBy] = useState<string>('updated_at')

  const isConnected = !!activeServerId && activeServer?.status === 'connected'

  // Convert context data to memories format
  const memories: Memory[] = React.useMemo(() => {
    if (!data?.context) {
      return []
    }

    return (data.context as RawContextEntry[]).map(ctx => ({
      context_key: ctx.context_key,
      value: ctx.value,
      description: ctx.description,
      updated_at: ctx.updated_at,
      updated_by: ctx.updated_by,
      created_at: ctx.created_at,
      created_by: ctx.created_by,
      _metadata: {
        size_bytes: JSON.stringify(ctx.value).length,
        size_kb: Math.round(JSON.stringify(ctx.value).length / 1024 * 100) / 100,
        json_valid: true,
        days_old: ctx.updated_at ? Math.floor((Date.now() - new Date(ctx.updated_at).getTime()) / (1000 * 60 * 60 * 24)) : undefined,
        is_stale: ctx.updated_at ? (Date.now() - new Date(ctx.updated_at).getTime()) > (30 * 24 * 60 * 60 * 1000) : false,
        is_large: JSON.stringify(ctx.value).length > 10240
      }
    }))
  }, [data?.context])

  // Live-lookup selector for the View/Edit/Delete dialogs. Re-computes
  // when `memories` changes (i.e. when the underlying context slice
  // refreshes from the store) so the open dialog re-renders against
  // the current row.
  const memorySelector = useCallback(
    (key: string | null) =>
      key ? memories.find((m) => m.context_key === key) ?? null : null,
    [memories],
  )
  const viewDialog = useDialog<Memory>(memorySelector)
  const editDialog = useDialog<Memory>(memorySelector)
  const deleteDialog = useDialog<Memory>(memorySelector)

  // Deleted-while-open: if the row is purged from the store, the
  // selector returns null. Auto-close so the user isn't stuck on an
  // empty modal.
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

  // Wave 6: no mount fetch effect — the `/all-data` TanStack Query
  // fetches automatically once a connected server is selected.

  // Filter and sort memories
  const filteredMemories = React.useMemo(() => {
    const filtered = memories.filter(memory =>
      memory.context_key.toLowerCase().includes(searchTerm.toLowerCase()) ||
      (memory.description && memory.description.toLowerCase().includes(searchTerm.toLowerCase())) ||
      JSON.stringify(memory.value).toLowerCase().includes(searchTerm.toLowerCase())
    )

    // Sort memories
    filtered.sort((a, b) => {
      switch (sortBy) {
        case 'key':
          return a.context_key.localeCompare(b.context_key)
        case 'size':
          return (b._metadata?.size_bytes || 0) - (a._metadata?.size_bytes || 0)
        case 'updated_at':
        default:
          return new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime()
      }
    })

    return filtered
  }, [memories, searchTerm, sortBy])

  // Calculate stats
  const stats = React.useMemo(() => {
    const total = memories.length
    const stale = memories.filter(m => m._metadata?.is_stale).length
    const large = memories.filter(m => m._metadata?.is_large).length
    const errors = memories.filter(m => !m._metadata?.json_valid).length

    return {
      total,
      stale,
      large,
      errors
    }
  }, [memories])

  const handleView = useCallback((memory: Memory) => {
    viewDialog.open(memory.context_key)
  }, [viewDialog])

  const handleEdit = useCallback((memory: Memory) => {
    editDialog.open(memory.context_key)
  }, [editDialog])

  const handleDelete = useCallback((memory: Memory) => {
    deleteDialog.open(memory.context_key)
  }, [deleteDialog])

  // Wave 2 (cleanup-wave-2): all three mutation handlers authenticate
  // via the operator session cookie (the apiClient.request helper
  // attaches it with ``credentials: "include"``). No admin bearer is
  // threaded through the call site anymore.
  //
  // Success/error are surfaced via the shared toast (matches Agents/
  // Tasks). The delete confirmation is the shared tier-1
  // <ConfirmActionModal>; it shows an inline error on failure — we
  // re-throw so it stays open, and also toast for consistency.
  const handleDeleteMemory = async (memory: Memory) => {
    try {
      await apiClient.deleteMemory(memory.context_key)
      await refreshData()
      toastSuccess(`Memory "${memory.context_key}" deleted.`)
    } catch (error) {
      toastError(error, 'Failed to delete memory')
      throw error
    }
  }

  const handleCreateMemory = async (data: {
    context_key: string
    context_value: unknown
    description?: string
  }) => {
    try {
      await apiClient.createMemory({
        context_key: data.context_key,
        context_value: data.context_value,
        description: data.description,
      })
      await refreshData()
      toastSuccess(`Memory "${data.context_key}" created.`)
    } catch (error) {
      toastError(error, 'Failed to create memory')
      throw error
    }
  }

  const handleUpdateMemory = async (data: {
    context_key: string
    context_value: unknown
    description?: string
  }) => {
    try {
      await apiClient.updateMemory(data.context_key, {
        context_value: data.context_value,
        description: data.description,
      })
      await refreshData()
      toastSuccess(`Memory "${data.context_key}" updated.`)
    } catch (error) {
      toastError(error, 'Failed to update memory')
      throw error
    }
  }

  // Stats strip — rendered by the scaffold's <StatsCard> row.
  const statsCards: StatsCardProps[] = [
    {
      icon: Database,
      label: 'Total',
      value: stats.total,
      change: stats.total > 0 ? `${memories.length} entries` : undefined,
      trend: 'neutral',
    },
    {
      icon: CheckCircle2,
      label: 'Healthy',
      value: stats.total - stats.stale - stats.errors,
      change:
        stats.total > 0
          ? `${Math.round(((stats.total - stats.stale - stats.errors) / stats.total) * 100)}%`
          : '0%',
      trend: 'up',
    },
    {
      icon: Clock,
      label: 'Stale',
      value: stats.stale,
      change: stats.stale > 0 ? 'Need review' : 'All fresh',
      trend: stats.stale > 0 ? 'down' : 'neutral',
    },
    {
      icon: AlertCircle,
      label: 'Issues',
      value: stats.errors + stats.large,
      change: stats.errors + stats.large > 0 ? 'Need attention' : 'All good',
      trend: stats.errors + stats.large > 0 ? 'down' : 'neutral',
    },
  ]

  // Column spec — one source for the desktop table (via
  // <ResponsiveDataTable>) and, through `renderMobileCard`, the mobile
  // card. Cells reproduce the pre-foundation <MemoryRow> exactly.
  const columns: Column<Memory>[] = useMemo(() => [
    {
      id: 'key',
      header: 'Memory Key',
      cellClassName: 'py-2 px-2 sm:px-4',
      cell: (memory) => (
        <div className="flex items-center gap-2">
          <Brain className="h-3 w-3 text-primary flex-shrink-0" />
          <div className="min-w-0 flex-1">
            <div className="font-medium text-xs sm:text-sm text-foreground truncate" title={memory.context_key}>
              {formatKey(memory.context_key)}
            </div>
            {memory.description && (
              <div className="text-xs text-muted-foreground truncate hidden sm:block" title={memory.description}>
                {memory.description.length > 20 ? memory.description.substring(0, 20) + '...' : memory.description}
              </div>
            )}
          </div>
        </div>
      ),
    },
    {
      id: 'value',
      header: 'Value',
      hideBelow: 'md',
      cellClassName: 'py-2 px-2',
      cell: (memory) => {
        const preview = memoryValuePreview(decodeMemoryValue(memory.value))
        return (
          <div className="flex items-center gap-2 max-w-[220px]" title={valueTooltip(memory.value)}>
            <Badge
              variant="outline"
              className="text-xs font-semibold border-0 px-3 py-1.5 rounded-md bg-muted/50 text-muted-foreground ring-1 ring-border flex-shrink-0 whitespace-nowrap"
            >
              {preview.label}
            </Badge>
            <span className="text-xs text-muted-foreground truncate">
              {preview.snippet}
            </span>
          </div>
        )
      },
    },
    {
      id: 'status',
      header: 'Status',
      hideBelow: 'lg',
      cellClassName: 'py-2 px-2',
      cell: (memory) => {
        const metadata = memory._metadata
        return (
          <div className="flex items-center gap-1 flex-wrap">
            {metadata?.size_kb && metadata.size_kb > 1 && (
              <Badge
                variant="outline"
                className="text-xs font-semibold border-0 px-3 py-1.5 rounded-md bg-muted/50 text-muted-foreground ring-1 ring-border"
              >
                {metadata.size_kb}KB
              </Badge>
            )}
            {metadata?.is_stale && (
              <Badge
                variant="outline"
                className="text-xs font-semibold border-0 px-3 py-1.5 rounded-md bg-orange-500/15 text-orange-500 dark:text-orange-300 ring-1 ring-orange-500/20"
              >
                Stale
              </Badge>
            )}
            {metadata?.is_large && (
              <Badge
                variant="outline"
                className="text-xs font-semibold border-0 px-3 py-1.5 rounded-md bg-red-500/15 text-red-500 dark:text-red-300 ring-1 ring-red-500/20"
              >
                Large
              </Badge>
            )}
          </div>
        )
      },
    },
    {
      id: 'updated',
      header: 'Updated',
      hideBelow: 'sm',
      cellClassName: 'py-2 px-2',
      cell: (memory) => (
        <div className="text-xs text-muted-foreground">
          <div className="truncate">{memory.updated_by}</div>
          <div>{memory.updated_at ? new Date(memory.updated_at).toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) : 'Unknown'}</div>
        </div>
      ),
    },
    {
      id: 'actions',
      header: 'Actions',
      headClassName: 'w-24',
      cellClassName: 'py-2 px-1',
      // Every onClick stopPropagation so the row-body onClick (View)
      // doesn't fire on top of the action. Hover-reveal via the row's
      // `group` class (owned by <ResponsiveDataTable>).
      cell: (memory) => (
        <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
          <Button
            variant="ghost"
            size="sm"
            onClick={(e) => { e.stopPropagation(); handleView(memory) }}
            className="h-7 w-7 p-0 text-muted-foreground hover:text-foreground hover:bg-muted"
            title="View details"
          >
            <Eye className="h-3.5 w-3.5" />
          </Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={(e) => { e.stopPropagation(); handleEdit(memory) }}
            className="h-7 w-7 p-0 text-primary hover:text-primary hover:bg-primary/10"
            title="Edit memory"
          >
            <Pencil className="h-3.5 w-3.5" />
          </Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={(e) => { e.stopPropagation(); handleDelete(memory) }}
            className="h-7 w-7 p-0 text-destructive hover:text-destructive hover:bg-destructive/10"
            title="Delete memory"
          >
            <Trash2 className="h-3.5 w-3.5" />
          </Button>
        </div>
      ),
    },
  ], [handleView, handleEdit, handleDelete])

  const filterBar = (
    <>
      <div className="relative flex-1 sm:max-w-sm">
        <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
        <Input
          aria-label="Search memories"
          placeholder="Search memories..."
          value={searchTerm}
          onChange={(e) => setFilter("searchTerm", e.target.value)}
          className="pl-10 bg-background border-border text-foreground placeholder:text-muted-foreground focus:border-primary/50 focus:ring-primary/20 transition-all"
        />
      </div>
      <Select value={sortBy} onValueChange={setSortBy}>
        <SelectTrigger className="w-full sm:w-40 bg-background border-border text-foreground">
          <SelectValue />
        </SelectTrigger>
        <SelectContent className="bg-background border-border">
          <SelectItem value="updated_at">Latest First</SelectItem>
          <SelectItem value="key">Alphabetical</SelectItem>
          <SelectItem value="size">Size (Large First)</SelectItem>
        </SelectContent>
      </Select>
    </>
  )

  return (
    <DataTablePage<Memory>
      guard={
        !isConnected
          ? {
              icon: Network,
              title: 'No Server Connection',
              description: 'Connect to an MCP server to manage memories',
            }
          : null
      }
      loading={loading}
      error={error}
      header={{
        title: 'Memory Bank',
        subtitle: 'Manage system context and memories',
        serverName: activeServer?.name,
        lastUpdated: data?.timestamp,
        onRefresh: refreshData,
        refreshing: loading,
        actions: <CreateMemoryModal onCreateMemory={handleCreateMemory} />,
      }}
      stats={statsCards}
      filterBar={filterBar}
      columns={columns}
      rows={filteredMemories}
      getRowId={(m) => m.context_key}
      onRowClick={handleView}
      renderMobileCard={(memory) => (
        <MemoryMobileCard
          memory={memory}
          onView={handleView}
          onEdit={handleEdit}
          onDelete={handleDelete}
        />
      )}
      empty={{
        icon: Brain,
        title: 'No memories found',
        description:
          memories.length === 0
            ? 'Create your first memory to get started.'
            : 'No memories match your current filters.',
        action:
          memories.length === 0 ? (
            <CreateMemoryModal
              onCreateMemory={handleCreateMemory}
              trigger={
                <Button>
                  <Plus className="h-4 w-4 mr-2" />
                  Create first memory
                </Button>
              }
            />
          ) : undefined,
      }}
    >
      {/* View Memory Modal */}
      <ViewMemoryModal
        memory={viewDialog.data}
        open={viewDialog.isOpen}
        onOpenChange={(open) => { if (!open) viewDialog.close() }}
        onEdit={() => {
          const memory = viewDialog.data
          if (!memory) return
          viewDialog.close()
          handleEdit(memory)
        }}
        onDelete={() => {
          const memory = viewDialog.data
          if (!memory) return
          viewDialog.close()
          handleDelete(memory)
        }}
      />

      {/* Edit Memory Modal (shared standalone component) */}
      {editDialog.isOpen && editDialog.data && (
        <EditMemoryModal
          memory={editDialog.data}
          open={editDialog.isOpen}
          onOpenChange={(open) => { if (!open) editDialog.close() }}
          onUpdateMemory={handleUpdateMemory}
        />
      )}

      {/* Delete confirmation — TIER 1 (a deliberate downgrade from the
          type-DELETE `<DeleteConfirmModal>` this used to render).

          A memory delete is a single row with a bounded cascade (one RAG
          source), its full value is on screen in the `details` slot
          below — so it is recreatable by copy/paste — and the keys where
          that is NOT true (`server_*`, `database_version`,
          `system_config`, `mcp_server_url`) are already gated
          server-side behind `force_delete` in
          `project_context_tools.py`. It is also the highest-frequency
          delete in the product.

          Charging type-to-confirm for routine housekeeping is what
          trains an operator to type DELETE without reading, and that
          reflex is spent on the Users / Groups dialogs, which cascade
          across every project membership and capability grant. Making
          THIS cheap is what keeps those expensive. See
          `confirm-action-modal.tsx` for the tier table + citations. */}
      <ConfirmActionModal
        open={deleteDialog.isOpen}
        onOpenChange={(open) => { if (!open) deleteDialog.close() }}
        title="Delete memory"
        description={
          deleteDialog.data
            ? `Delete “${deleteDialog.data.context_key}”? This cannot be undone.`
            : undefined
        }
        confirmLabel="Delete Memory"
        details={deleteDialog.data && <MemoryDeletePreview memory={deleteDialog.data} />}
        onConfirm={async () => {
          const memory = deleteDialog.data
          if (memory) await handleDeleteMemory(memory)
        }}
      />
    </DataTablePage>
  )
}
