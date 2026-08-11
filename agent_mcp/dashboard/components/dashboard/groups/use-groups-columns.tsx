"use client"

import React, { useMemo } from "react"
import {
  ChevronDown, ChevronRight, Pencil, Shield, Trash2, Users as UsersIcon,
} from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import type { Column } from "@/components/dashboard/shared/responsive-data-table"
import { memberLabel, type GroupRow } from "@/components/dashboard/groups/groups-api"

/**
 * Column spec for the Groups table (Wave 5 extraction — mirrors
 * `useTasksColumns` / `useMessagesColumns` / `useAgentColumns`).
 *
 * ONE source drives the desktop table (via `<ResponsiveDataTable>`) and,
 * through the page's `renderMobileCard`, the mobile card. Because the
 * mobile card reflows the summary into two lines rather than stacking
 * cells, it reuses the shared `chevron` + `rowActions` renderers this
 * hook returns alongside the columns — so the expand glyph and the
 * Edit/Delete action buttons can never drift between the two halves.
 *
 * Row-body click toggles the accordion; every action `stopPropagation`s
 * so it doesn't also expand/collapse.
 */
export interface GroupsColumnHandlers {
  /** The set of expanded group ids (drives the chevron glyph). */
  expanded: Set<string>
  /** Toggle a row's accordion. */
  toggleExpand: (id: string) => void
  /** Open the Edit dialog for a group. */
  onEdit: (group: GroupRow) => void
  /** Open the Delete confirm dialog for a group. */
  onDelete: (group: GroupRow) => void
}

export interface GroupsColumns {
  columns: Column<GroupRow>[]
  /** Expand/collapse glyph for a row — shared with the mobile card. */
  chevron: (group: GroupRow) => React.ReactElement
  /** Edit + Delete action buttons for a row — shared with the mobile card. */
  rowActions: (group: GroupRow) => React.ReactElement
}

export function useGroupsColumns(
  handlers: GroupsColumnHandlers,
): GroupsColumns {
  const { expanded, toggleExpand, onEdit, onDelete } = handlers

  return useMemo<GroupsColumns>(() => {
    const chevron = (group: GroupRow) =>
      expanded.has(group.group_id) ? (
        <ChevronDown className="h-4 w-4" />
      ) : (
        <ChevronRight className="h-4 w-4" />
      )

    const rowActions = (group: GroupRow) => (
      <div className="flex items-center gap-1">
        <Button
          variant="ghost"
          size="icon"
          aria-label={`Edit ${group.name}`}
          onClick={(e) => {
            e.stopPropagation()
            onEdit(group)
          }}
        >
          <Pencil className="h-4 w-4" />
        </Button>
        <Button
          variant="ghost"
          size="icon"
          aria-label={`Delete ${group.name}`}
          className="text-destructive"
          onClick={(e) => {
            e.stopPropagation()
            onDelete(group)
          }}
        >
          <Trash2 className="h-4 w-4" />
        </Button>
      </div>
    )

    const columns: Column<GroupRow>[] = [
      {
        id: "expand",
        header: <span className="sr-only">Expand</span>,
        headClassName: "w-10",
        cellClassName: "w-10",
        cell: (group) => (
          <Button
            variant="ghost"
            size="icon"
            className="h-7 w-7"
            aria-label={`${
              expanded.has(group.group_id) ? "Collapse" : "Expand"
            } ${group.name}`}
            aria-expanded={expanded.has(group.group_id)}
            onClick={(e) => {
              e.stopPropagation()
              toggleExpand(group.group_id)
            }}
          >
            {chevron(group)}
          </Button>
        ),
      },
      {
        id: "name",
        header: "Group",
        cell: (group) => (
          <div className="flex items-center gap-2 min-w-0">
            <UsersIcon className="h-4 w-4 text-muted-foreground flex-shrink-0" />
            <span className="font-medium truncate">{group.name}</span>
            {group.is_sysadmin && (
              <Badge variant="default" className="flex items-center gap-1">
                <Shield className="h-3 w-3" /> sysadmin
              </Badge>
            )}
          </div>
        ),
      },
      {
        id: "members",
        header: "Members",
        cell: (group) => (
          <Badge variant="secondary" className="text-xs">
            {memberLabel(group.member_count)}
          </Badge>
        ),
      },
      {
        id: "actions",
        header: "Actions",
        headClassName: "w-24",
        cell: rowActions,
      },
    ]

    return { columns, chevron, rowActions }
  }, [expanded, toggleExpand, onEdit, onDelete])
}
