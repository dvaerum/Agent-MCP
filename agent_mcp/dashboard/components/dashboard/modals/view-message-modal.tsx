"use client"

import { MessageSquare, Send, Mail, MailOpen, Trash2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import type { Message } from '@/lib/api'

// Render a relative-time hint like "5 hours ago" / "in 3 minutes" so
// admins don't have to do the timezone math themselves. Falls back to
// the raw value if it can't be parsed. (Kept as a local copy of the
// messages-dashboard helper it was extracted alongside — the detail
// modal is its only consumer.)
function relativeTime(iso: string): string {
  const t = Date.parse(iso)
  if (Number.isNaN(t)) return iso
  const deltaMs = t - Date.now()
  const abs = Math.abs(deltaMs)
  const sec = Math.round(abs / 1000)
  const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" })
  const sign = deltaMs >= 0 ? 1 : -1
  if (sec < 60) return rtf.format(sign * sec, "second")
  const min = Math.round(sec / 60)
  if (min < 60) return rtf.format(sign * min, "minute")
  const hr = Math.round(min / 60)
  if (hr < 24) return rtf.format(sign * hr, "hour")
  const day = Math.round(hr / 24)
  if (day < 30) return rtf.format(sign * day, "day")
  const month = Math.round(day / 30)
  if (month < 12) return rtf.format(sign * month, "month")
  return rtf.format(sign * Math.round(month / 12), "year")
}

interface ViewMessageModalProps {
  message: Message | null
  open: boolean
  onOpenChange: (open: boolean) => void
  // In-modal footer actions. Reply opens the compose form pre-wired to
  // this message; Mark-read toggles read state IN PLACE (the modal
  // stays open — the live-lookup dialog re-renders with the fresh
  // row); Delete routes through the delete-confirm dialog.
  onReply: () => void
  onToggleRead: () => void
  onDelete: () => void
}

export function ViewMessageModal({
  message,
  open,
  onOpenChange,
  onReply,
  onToggleRead,
  onDelete,
}: ViewMessageModalProps) {
  if (!message) return null

  const isRead = message.read === 1 || message.read === true
  const isDelivered = message.delivered === 1 || message.delivered === true

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="w-[calc(100vw-2rem)] sm:!max-w-2xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <MessageSquare className="h-5 w-5 text-primary" />
            Message detail
          </DialogTitle>
          <DialogDescription>
            {new Date(message.timestamp).toLocaleString()} ·{" "}
            {relativeTime(message.timestamp)}
          </DialogDescription>
        </DialogHeader>

        <div className="grid grid-cols-2 gap-x-4 gap-y-3 text-sm">
          <div>
            <div className="text-xs text-muted-foreground">Sender</div>
            <div className="font-mono break-all">{message.sender_id}</div>
          </div>
          <div>
            <div className="text-xs text-muted-foreground">Recipient</div>
            <div className="font-mono break-all">{message.recipient_id}</div>
          </div>
          <div>
            <div className="text-xs text-muted-foreground">Type</div>
            <div>
              <Badge variant="outline">{message.message_type}</Badge>
            </div>
          </div>
          <div>
            <div className="text-xs text-muted-foreground">Priority</div>
            <div>
              <Badge variant="outline">{message.priority}</Badge>
            </div>
          </div>
          <div>
            <div className="text-xs text-muted-foreground">Delivered</div>
            <div>
              {isDelivered ? (
                <Badge variant="secondary">✓ delivered</Badge>
              ) : (
                <Badge variant="outline">✗ pending</Badge>
              )}
            </div>
          </div>
          <div>
            <div className="text-xs text-muted-foreground">Read</div>
            <div>
              {isRead ? (
                <Badge variant="secondary">✓ read</Badge>
              ) : (
                <Badge variant="outline">✗ unread</Badge>
              )}
            </div>
          </div>
        </div>

        <div className="space-y-1">
          <div className="text-xs text-muted-foreground">Content</div>
          <pre className="whitespace-pre-wrap break-words rounded-md border bg-muted/50 p-3 font-mono text-xs max-h-[40vh] overflow-auto">
            {message.message_content}
          </pre>
        </div>

        <div className="text-[10px] font-mono text-muted-foreground break-all">
          Message ID: {message.message_id}
        </div>

        <DialogFooter>
          {/* v5.0.22: Reply opens the compose form pre-wired with
              parent_message_id pinned to this row. */}
          <Button variant="outline" size="sm" onClick={onReply}>
            <Send className="h-4 w-4 mr-1" />
            Reply
          </Button>
          <Button variant="outline" size="sm" onClick={onToggleRead}>
            {isRead ? (
              <>
                <Mail className="h-4 w-4 mr-1" />
                Mark unread
              </>
            ) : (
              <>
                <MailOpen className="h-4 w-4 mr-1" />
                Mark read
              </>
            )}
          </Button>
          <Button variant="destructive" size="sm" onClick={onDelete}>
            <Trash2 className="h-4 w-4 mr-1" />
            Delete
          </Button>
          <Button variant="ghost" size="sm" onClick={() => onOpenChange(false)}>
            Close
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
