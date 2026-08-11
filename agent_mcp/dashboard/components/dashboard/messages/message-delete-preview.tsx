"use client"

import { Badge } from "@/components/ui/badge"
import type { Message } from "@/lib/api"

/**
 * Preview block shown inside the DeleteConfirmModal `details` slot for
 * the SINGLE-message variant — reproduces the pre-foundation
 * DeleteMessageModal body (participants / SUBJECT / CONTENT PREVIEW /
 * metadata). The bulk variant has no preview (there is no single row to
 * show); it overrides `title` / `description` / `warningText` instead.
 */
export function MessageDeletePreview({ message }: { message: Message }) {
  const formatContent = (value: string) =>
    value.length > 120 ? value.substring(0, 120) + "…" : value
  return (
    <div className="space-y-3">
      <div className="text-sm font-medium text-foreground">Message to be deleted:</div>
      <div className="bg-muted/30 border border-border rounded-lg p-3 space-y-3">
        {/* Participants */}
        <div className="flex items-center gap-2 text-sm">
          <Badge variant="outline">{message.sender_id}</Badge>
          <span aria-hidden className="text-muted-foreground">→</span>
          <Badge variant="outline">{message.recipient_id}</Badge>
        </div>
        {message.subject && (
          <div>
            <div className="text-xs font-medium text-muted-foreground mb-1">SUBJECT</div>
            <div className="text-sm text-foreground">{message.subject}</div>
          </div>
        )}
        <div>
          <div className="text-xs font-medium text-muted-foreground mb-1">CONTENT PREVIEW</div>
          <div className="text-sm text-muted-foreground bg-background border border-border rounded px-2 py-1 font-mono max-h-16 overflow-hidden">
            {formatContent(message.message_content)}
          </div>
        </div>
        <div className="flex items-center gap-4 text-xs text-muted-foreground pt-2 border-t border-border">
          <span>{message.timestamp.slice(0, 19)}</span>
          <span>Type: {message.message_type}</span>
          <span>Priority: {message.priority}</span>
        </div>
      </div>
    </div>
  )
}
