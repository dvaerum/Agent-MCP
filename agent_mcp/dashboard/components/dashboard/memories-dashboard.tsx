"use client"

import React, { useCallback, useEffect, useState } from 'react'
import {
  Brain,
  Search,
  Plus,
  Pencil,
  Trash2,
  Eye,
  RefreshCw,
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
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow
} from '@/components/ui/table'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { cn } from '@/lib/utils'
import { useDataStore } from '@/lib/stores/data-store'
import { useServerStore } from '@/lib/stores/server-store'
import { useDialog } from '@/hooks/use-dialog'
import { useFilters } from '@/hooks/use-filters'
import { apiClient, type Memory } from '@/lib/api'
import { toastError, toastSuccess } from '@/components/ui/toast'
import { decodeMemoryValue, memoryValuePreview } from '@/lib/memory-value'
import { CreateMemoryModal } from './modals/create-memory-modal'
import { ViewMemoryModal } from './modals/view-memory-modal'
import { EditMemoryModal } from './modals/edit-memory-modal'
import { DeleteMemoryModal } from './modals/delete-memory-modal'
import { Skeleton } from "@/components/ui/skeleton"
import { EmptyState } from "@/components/dashboard/shared/empty-state"
import { MemoriesMobileList } from "@/components/dashboard/memories-mobile-list"

// Stats card component — matches the Agents/Tasks StatsCard (CC-4/CC-8/
// CC-16 audit 2026-06-02: plain Tailwind sizing + rounded-lg + semantic
// tokens + tabular-nums; no fluid CSS-vars, no rounded-xl/backdrop-blur).
const StatsCard = ({ icon: Icon, label, value, change, trend }: {
  icon: React.ComponentType<{ className?: string }>
  label: string
  value: number
  change?: string
  trend?: 'up' | 'down' | 'neutral'
}) => (
  <div className="bg-card border border-border rounded-lg p-3 sm:p-5 hover:bg-muted/30 transition-colors duration-150 group">
    <div className="flex items-center justify-between">
      <div>
        <div className="flex items-center gap-2 mb-2">
          <Icon className="h-4 w-4 text-muted-foreground transition-colors" />
          <span className="text-xs font-semibold text-muted-foreground uppercase tracking-wider">{label}</span>
        </div>
        <div className="text-2xl sm:text-3xl font-semibold text-foreground tabular-nums mb-1">{value}</div>
        {change && (
          <div className={cn(
            "text-xs font-medium tabular-nums",
            trend === 'up' && "text-emerald-500",
            trend === 'down' && "text-destructive",
            trend === 'neutral' && "text-muted-foreground"
          )}>
            {change}
          </div>
        )}
      </div>
    </div>
  </div>
)

// Memory row component
const MemoryRow = ({ memory, onView, onEdit, onDelete }: {
  memory: Memory
  onView: (memory: Memory) => void
  onEdit: (memory: Memory) => void
  onDelete: (memory: Memory) => void
}) => {
  const metadata = memory._metadata

  // ADR-0017 (Wave 12 PR B): no content-based secret redaction. memory
  // is shared project knowledge, rendered AS-IS — the key-name masking
  // that used to hide "secret-looking" rows (and, wrongly, the
  // operator's own legitimate notes) is gone. Real secrets belong in
  // the operator-only project_settings store, never in memory.
  //
  // Wave 13: the value cell shows a compact type badge + one-line
  // snippet of the human-readable content; the full rich view is the
  // modal (View action).
  const preview = memoryValuePreview(decodeMemoryValue(memory.value))
  const valueTooltip = (value: any): string => {
    const raw = typeof value === 'string' ? value : JSON.stringify(value)
    return raw
  }

  const formatKey = (key: string) => {
    return key.length > 25 ? key.substring(0, 25) + '...' : key
  }

  return (
    <TableRow
      className="border-border/50 hover:bg-muted/30 group transition-all duration-200 cursor-pointer"
      onClick={() => onView(memory)}
    >
      {/* Memory Key - More compact */}
      <TableCell className="py-2 px-2 sm:px-4">
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
      </TableCell>

      {/* Value - Hidden on mobile, shown on tablet+. Compact preview:
          type badge + one-line snippet (full rich view is the modal). */}
      <TableCell className="py-2 px-2 hidden md:table-cell">
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
      </TableCell>

      {/* Status - pill badges matching the Agents/Tasks convention. */}
      <TableCell className="py-2 px-2 hidden lg:table-cell">
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
      </TableCell>

      {/* Updated info - Compact */}
      <TableCell className="py-2 px-2 hidden sm:table-cell">
        <div className="text-xs text-muted-foreground">
          <div className="truncate">{memory.updated_by}</div>
          <div>{memory.updated_at ? new Date(memory.updated_at).toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) : 'Unknown'}</div>
        </div>
      </TableCell>

      {/* Row-action buttons. Every onClick stopPropagation so the
          row-body onClick (which opens View) doesn't fire on top of the
          action. Hover-reveal at all breakpoints, h-7/h-3.5 sizing to
          match Agents/Tasks. */}
      <TableCell className="py-2 px-1">
        <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
          <Button
            variant="ghost"
            size="sm"
            onClick={(e) => { e.stopPropagation(); onView(memory) }}
            className="h-7 w-7 p-0 text-muted-foreground hover:text-foreground hover:bg-muted"
            title="View details"
          >
            <Eye className="h-3.5 w-3.5" />
          </Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={(e) => { e.stopPropagation(); onEdit(memory) }}
            className="h-7 w-7 p-0 text-primary hover:text-primary hover:bg-primary/10"
            title="Edit memory"
          >
            <Pencil className="h-3.5 w-3.5" />
          </Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={(e) => { e.stopPropagation(); onDelete(memory) }}
            className="h-7 w-7 p-0 text-destructive hover:text-destructive hover:bg-destructive/10"
            title="Delete memory"
          >
            <Trash2 className="h-3.5 w-3.5" />
          </Button>
        </div>
      </TableCell>
    </TableRow>
  )
}

export function MemoriesDashboard() {
  const { servers, activeServerId } = useServerStore()
  const activeServer = servers.find(s => s.id === activeServerId)
  const { data, loading, error, refreshData } = useDataStore()
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

    return data.context.map(ctx => ({
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
  useEffect(() => {
    if (viewDialog.isOpen && viewDialog.data === null) viewDialog.close()
  }, [viewDialog.isOpen, viewDialog.data, viewDialog.close])
  useEffect(() => {
    if (editDialog.isOpen && editDialog.data === null) editDialog.close()
  }, [editDialog.isOpen, editDialog.data, editDialog.close])
  useEffect(() => {
    if (deleteDialog.isOpen && deleteDialog.data === null) deleteDialog.close()
  }, [deleteDialog.isOpen, deleteDialog.data, deleteDialog.close])

  // Fetch data on mount and when server changes
  useEffect(() => {
    if (activeServerId && activeServer?.status === 'connected') {
      refreshData()
    }
  }, [activeServerId, activeServer?.status, refreshData])

  // Filter and sort memories
  const filteredMemories = React.useMemo(() => {
    let filtered = memories.filter(memory =>
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

  const handleView = (memory: Memory) => {
    viewDialog.open(memory.context_key)
  }

  const handleEdit = (memory: Memory) => {
    editDialog.open(memory.context_key)
  }

  const handleDelete = (memory: Memory) => {
    deleteDialog.open(memory.context_key)
  }

  // Wave 2 (cleanup-wave-2): all three mutation handlers authenticate
  // via the operator session cookie (the apiClient.request helper
  // attaches it with ``credentials: "include"``). No admin bearer is
  // threaded through the call site anymore.
  //
  // Success/error are surfaced via the shared toast (matches Agents/
  // Tasks). The delete confirmation is the shared DeleteMemoryModal
  // (type-DELETE-to-confirm); it shows an inline error on failure —
  // we re-throw so it stays open, and also toast for consistency.
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
    context_value: any
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
    context_value: any
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

  if (!isConnected) {
    return (
      <div className="h-full flex items-center justify-center">
        <div className="text-center space-y-4">
          <Network className="h-12 w-12 text-muted-foreground mx-auto" />
          <div>
            <h3 className="text-lg font-medium text-foreground mb-2">No Server Connection</h3>
            <p className="text-muted-foreground text-sm">Connect to an MCP server to manage memories</p>
          </div>
        </div>
      </div>
    )
  }

  if (loading && memories.length === 0) {
    // CC-3 audit 2026-06-02: Skeleton shape mirroring the stats + table
    // layout, so the page reads as populating in place rather than the
    // dashboard being broken. Matches the Agents page.
    return (
      <div className="w-full p-4 sm:p-6 space-y-4 sm:space-y-6">
        <div className="grid gap-3 sm:gap-4 grid-cols-1 sm:grid-cols-2 xl:grid-cols-4">
          {Array.from({ length: 4 }).map((_, i) => (
            <Skeleton key={i} className="h-24" />
          ))}
        </div>
        <Skeleton className="h-10 w-full sm:max-w-md" />
        <div className="bg-card border border-border rounded-lg p-4 space-y-3">
          {Array.from({ length: 5 }).map((_, i) => (
            <Skeleton key={i} className="h-14 w-full" />
          ))}
        </div>
      </div>
    )
  }

  if (error) {
    return (
      <div className="h-full flex items-center justify-center">
        <div className="text-center space-y-4">
          <AlertCircle className="h-12 w-12 text-destructive mx-auto" />
          <div>
            <h3 className="text-lg font-medium text-foreground mb-2">Connection Error</h3>
            <p className="text-destructive text-sm">{error}</p>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="w-full p-4 sm:p-6 space-y-4 sm:space-y-6">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
        <div>
          <h1 className="text-2xl sm:text-3xl font-semibold tracking-tight text-foreground">Memory Bank</h1>
          <p className="text-muted-foreground text-sm sm:text-base mt-1">Manage system context and memories</p>
        </div>
        <div className="flex flex-wrap items-center gap-2 sm:gap-3">
          {/* CC-19: static server-online dot (no animate-pulse). */}
          <Badge variant="outline" className="text-xs bg-primary/15 text-primary border-primary/30 font-medium">
            <span aria-hidden className="w-2 h-2 bg-primary rounded-full mr-2" />
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
            onClick={refreshData}
            disabled={loading}
            className="text-xs"
          >
            <RefreshCw className={cn("h-3.5 w-3.5 mr-1.5", loading && "animate-spin")} />
            Refresh
          </Button>
          <CreateMemoryModal onCreateMemory={handleCreateMemory} />
        </div>
      </div>

      {/* Stats */}
      <div className="grid gap-3 sm:gap-4 grid-cols-1 sm:grid-cols-2 xl:grid-cols-4">
        <StatsCard
          icon={Database}
          label="Total"
          value={stats.total}
          change={stats.total > 0 ? `${memories.length} entries` : undefined}
          trend="neutral"
        />
        <StatsCard
          icon={CheckCircle2}
          label="Healthy"
          value={stats.total - stats.stale - stats.errors}
          change={stats.total > 0 ? `${Math.round(((stats.total - stats.stale - stats.errors)/stats.total)*100)}%` : "0%"}
          trend="up"
        />
        <StatsCard
          icon={Clock}
          label="Stale"
          value={stats.stale}
          change={stats.stale > 0 ? "Need review" : "All fresh"}
          trend={stats.stale > 0 ? "down" : "neutral"}
        />
        <StatsCard
          icon={AlertCircle}
          label="Issues"
          value={stats.errors + stats.large}
          change={stats.errors + stats.large > 0 ? "Need attention" : "All good"}
          trend={stats.errors + stats.large > 0 ? "down" : "neutral"}
        />
      </div>

      {/* Controls */}
      <div className="flex flex-col sm:flex-row items-stretch sm:items-center gap-2 sm:gap-3">
        <div className="relative flex-1 sm:max-w-sm">
          <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
          <Input
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
      </div>

      {/* Memories list — CC-4/CC-6/CC-7 audit 2026-06-02: dropped
          bg-card/30 + backdrop-blur, shared EmptyState, mobile
          <MemoriesMobileList>. Initial-load Skeleton is the early
          return above (matches Agents). */}
      <div className="bg-card border border-border rounded-lg overflow-hidden">
        {filteredMemories.length === 0 ? (
          <EmptyState
            icon={Brain}
            title="No memories found"
            description={
              memories.length === 0
                ? "Create your first memory to get started."
                : "No memories match your current filters."
            }
            action={
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
              ) : undefined
            }
          />
        ) : (
          <>
            {/* Desktop table */}
            <div className="hidden sm:block overflow-x-auto">
              <Table>
                <TableHeader>
                  <TableRow className="border-border hover:bg-transparent">
                    <TableHead className="text-muted-foreground font-medium text-xs uppercase tracking-wider">Memory Key</TableHead>
                    <TableHead className="text-muted-foreground font-medium text-xs uppercase tracking-wider">Value</TableHead>
                    <TableHead className="text-muted-foreground font-medium text-xs uppercase tracking-wider">Status</TableHead>
                    <TableHead className="text-muted-foreground font-medium text-xs uppercase tracking-wider">Updated</TableHead>
                    <TableHead className="text-muted-foreground font-medium text-xs uppercase tracking-wider w-24">Actions</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {filteredMemories.map((memory) => (
                    <MemoryRow
                      key={memory.context_key}
                      memory={memory}
                      onView={handleView}
                      onEdit={handleEdit}
                      onDelete={handleDelete}
                    />
                  ))}
                </TableBody>
              </Table>
            </div>
            {/* Mobile card-list (CC-7) */}
            <div className="block sm:hidden">
              <MemoriesMobileList
                memories={filteredMemories}
                onView={handleView}
                onEdit={handleEdit}
                onDelete={handleDelete}
              />
            </div>
          </>
        )}
      </div>

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

      {/* Delete confirmation (type-DELETE-to-confirm) */}
      <DeleteMemoryModal
        memory={deleteDialog.data}
        open={deleteDialog.isOpen}
        onOpenChange={(open) => { if (!open) deleteDialog.close() }}
        onDeleteMemory={handleDeleteMemory}
      />
    </div>
  )
}
