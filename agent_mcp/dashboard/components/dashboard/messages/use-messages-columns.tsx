"use client"

import { useMemo } from "react"
import { MessageSquare, Trash2 } from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"
import type { Message } from "@/lib/api"
import type { Column } from "@/components/dashboard/shared/responsive-data-table"
import {
  priorityBadgeClass,
  messageTypeBadgeClass,
} from "@/components/dashboard/shared/message-badges"

/**
 * Column spec for the Messages table (Wave 5 extraction — mirrors
 * `useAgentColumns`).
 *
 * ONE source drives the desktop table (via `<ResponsiveDataTable>`)
 * and, through the page's `renderMobileCard`, the mobile card. Cells
 * reproduce the pre-foundation `<MessageRow>` exactly; the checkbox +
 * delete cells `stopPropagation` so they don't also fire the row-body
 * onClick (open detail).
 */
export interface MessagesColumnHandlers {
  /** Currently-selected message_ids (drives the per-row checkbox). */
  selectedIds: Set<string>
  /** True when every visible row is selected (header checkbox state). */
  allVisibleSelected: boolean
  /** Toggle every visible row. */
  onToggleAll: () => void
  /** Toggle one row's selection. */
  onToggleOne: (id: string) => void
  /** Open the delete-confirm dialog for one row. */
  onDelete: (id: string) => void
  /** Resolve a parent_message_id to a human-readable label. */
  labelForParent: (parentId: string | null) => string
}

export function useMessagesColumns(
  handlers: MessagesColumnHandlers,
): Column<Message>[] {
  const {
    selectedIds,
    allVisibleSelected,
    onToggleAll,
    onToggleOne,
    onDelete,
    labelForParent,
  } = handlers

  return useMemo<Column<Message>[]>(
    () => [
      {
        id: "select",
        headClassName: "w-8",
        header: (
          <input
            type="checkbox"
            aria-label="select all visible"
            checked={allVisibleSelected}
            onChange={onToggleAll}
          />
        ),
        cell: (m) => (
          <input
            type="checkbox"
            aria-label={`select message ${m.message_id}`}
            checked={selectedIds.has(m.message_id)}
            onChange={() => onToggleOne(m.message_id)}
            onClick={(e) => e.stopPropagation()}
          />
        ),
      },
      {
        id: "time",
        header: "Time",
        cellClassName: "text-xs font-mono tabular-nums",
        // Per-row entity glyph (matches memories' <Brain> convention).
        cell: (m) => (
          <div className="flex items-center gap-2">
            <MessageSquare className="h-3 w-3 text-primary flex-shrink-0" />
            <span>{m.timestamp.slice(0, 19)}</span>
          </div>
        ),
      },
      {
        id: "from",
        header: "From",
        cellClassName: "max-w-[160px]",
        cell: (m) => {
          const isRead = m.read === 1 || m.read === true
          return (
            <div className="flex items-center gap-1.5">
              {/* Leading unread dot — mirrors the mobile treatment so an
                  unread row is scannable at a glance, not just a ✓ column. */}
              {!isRead && (
                <span
                  aria-hidden
                  className="h-2 w-2 flex-shrink-0 rounded-full bg-primary"
                />
              )}
              {/* Long agent ids truncate (with a title tooltip) instead of
                  growing the column and forcing table-wide horizontal
                  overflow. */}
              <Badge
                variant="outline"
                className={cn("min-w-0 max-w-full", !isRead && "font-semibold")}
                title={m.sender_id}
              >
                <span className="truncate">{m.sender_id}</span>
              </Badge>
            </div>
          )
        },
      },
      {
        id: "to",
        header: "To",
        cellClassName: "max-w-[160px]",
        cell: (m) => (
          <Badge
            variant="outline"
            className="min-w-0 max-w-full"
            title={m.recipient_id}
          >
            <span className="truncate">{m.recipient_id}</span>
          </Badge>
        ),
      },
      {
        id: "subject",
        header: "Subject",
        cellClassName: "text-xs max-w-[200px] truncate",
        cell: (m) => {
          const isRead = m.read === 1 || m.read === true
          const isReply = !!m.parent_message_id
          return (
            <span className={cn(!isRead && "font-semibold text-foreground")}>
              {m.subject && m.subject_is_placeholder ? (
                // Placeholder: the sender set no subject, so this is an
                // auto-preview of the body (Phase 1). Shown muted + italic
                // with an "auto" tag so it reads as a stub, not a real
                // subject — a generated one fills in on the next backfill
                // sweep (Phase 2).
                <span
                  className="italic text-muted-foreground"
                  title="No subject set — auto-preview of the message. A generated subject will fill in shortly."
                >
                  {m.subject}
                  <span className="ml-1 not-italic text-[10px] font-medium uppercase tracking-wide text-muted-foreground/60">
                    auto
                  </span>
                </span>
              ) : m.subject ? (
                // Real subject: title reveals the full text on hover when the
                // cell truncates. (The placeholder branch keeps its own
                // explanatory title, so we don't clobber it here.)
                <span title={m.subject}>{m.subject}</span>
              ) : isReply ? (
                // v5.0.24 polish: human-readable parent label instead of the
                // opaque message_id.
                <span className="text-muted-foreground">
                  ↳ reply to:{" "}
                  <span className="text-foreground">
                    {labelForParent(m.parent_message_id)}
                  </span>
                </span>
              ) : (
                <span className="text-muted-foreground/50">—</span>
              )}
            </span>
          )
        },
      },
      {
        id: "type",
        header: "Type",
        cellClassName: "text-xs",
        cell: (m) => (
          <Badge variant="outline" className={messageTypeBadgeClass(m.message_type)}>
            {m.message_type}
          </Badge>
        ),
      },
      {
        id: "priority",
        header: "Priority",
        cellClassName: "text-xs",
        cell: (m) => (
          <Badge variant="outline" className={priorityBadgeClass(m.priority)}>
            {m.priority}
          </Badge>
        ),
      },
      {
        id: "read",
        header: "Read?",
        // Glyph is silent to screen readers; the sr-only text names the
        // state so it's announced.
        cell: (m) => {
          const isRead = m.read === 1 || m.read === true
          return (
            <>
              <span aria-hidden>{isRead ? "✓" : ""}</span>
              <span className="sr-only">{isRead ? "read" : "unread"}</span>
            </>
          )
        },
      },
      {
        id: "content",
        header: "Content",
        cellClassName: "max-w-[400px] truncate text-xs",
        cell: (m) => {
          const isRead = m.read === 1 || m.read === true
          return (
            <span
              className={cn(!isRead && "text-foreground")}
              title={m.message_content}
            >
              {m.message_content}
            </span>
          )
        },
      },
      {
        id: "actions",
        header: "",
        headClassName: "w-8",
        cell: (m) => (
          <Button
            variant="ghost"
            size="sm"
            aria-label="delete message"
            className="text-destructive hover:text-destructive hover:bg-destructive/10"
            onClick={(e) => {
              e.stopPropagation()
              onDelete(m.message_id)
            }}
          >
            <Trash2 className="h-4 w-4" />
          </Button>
        ),
      },
    ],
    [
      selectedIds,
      allVisibleSelected,
      onToggleAll,
      onToggleOne,
      onDelete,
      labelForParent,
    ],
  )
}
