"use client"

import { useEffect, useState } from 'react'
import {
  MessageSquare,
  Send,
  Mail,
  MailOpen,
  Trash2,
  Loader2,
} from 'lucide-react'
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
import { cn } from '@/lib/utils'
import { getMessageThread, type Message } from '@/lib/api'
import { projectContext } from '@/lib/project-context'

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
  // this message (threads onto this conversation); Mark-read toggles read
  // state IN PLACE (the modal stays open — the live-lookup dialog
  // re-renders with the fresh row); Delete routes through the
  // delete-confirm dialog.
  onReply: () => void
  onToggleRead: () => void
  onDelete: () => void
}

// One chat-style row in the flat chronological conversation. `opened`
// marks the message the user actually clicked so it gets a ring/accent
// and they can see which one they came in on.
function ConversationRow({
  msg,
  opened,
}: {
  msg: Message
  opened: boolean
}) {
  // Direction cue: messages FROM admin lean one way, TO admin the other.
  // Purely visual — a subtle left accent so a back-and-forth reads as a
  // conversation rather than a flat log.
  const fromAdmin = msg.sender_id === 'admin'
  return (
    <div
      className={cn(
        "rounded-md border p-3 text-sm",
        fromAdmin
          ? "border-l-2 border-l-primary/40 bg-muted/30"
          : "border-l-2 border-l-muted-foreground/20",
        opened && "ring-2 ring-primary ring-offset-1 ring-offset-background",
      )}
    >
      <div className="flex items-center justify-between gap-2 mb-1">
        <div className="flex items-center gap-1.5 text-xs font-mono">
          <span className="font-medium break-all">{msg.sender_id}</span>
          <span className="text-muted-foreground">→</span>
          <span className="break-all">{msg.recipient_id}</span>
          {opened && (
            <Badge variant="secondary" className="ml-1 text-[10px]">
              opened
            </Badge>
          )}
        </div>
        <span
          className="text-[11px] text-muted-foreground whitespace-nowrap"
          title={new Date(msg.timestamp).toLocaleString()}
        >
          {relativeTime(msg.timestamp)}
        </span>
      </div>
      <pre className="whitespace-pre-wrap break-words font-mono text-xs">
        {msg.message_content}
      </pre>
    </div>
  )
}

export function ViewMessageModal({
  message,
  open,
  onOpenChange,
  onReply,
  onToggleRead,
  onDelete,
}: ViewMessageModalProps) {
  // Feature 1: on open, fetch the whole thread this message belongs to so
  // the modal renders a flat chronological conversation instead of a lone
  // message. Falls back to just the single `message` on error (or while
  // the fetch is in flight, for the header/detail derivations below).
  const [thread, setThread] = useState<Message[]>([])
  const [loading, setLoading] = useState(false)

  const openedId = message?.message_id ?? null

  useEffect(() => {
    if (!open || !message) {
      setThread([])
      return
    }
    let cancelled = false
    setLoading(true)
    getMessageThread(projectContext.projectName ?? "", message.message_id)
      .then((rows) => {
        if (cancelled) return
        // A thread should always contain at least the opened message; if
        // the endpoint returns nothing unexpected, fall back to the one
        // message we already have so the modal never blows up.
        setThread(rows.length > 0 ? rows : [message])
      })
      .catch(() => {
        if (!cancelled) setThread([message])
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [open, openedId, message])

  if (!message) return null

  const isRead = message.read === 1 || message.read === true
  const isDelivered = message.delivered === 1 || message.delivered === true

  // The root is the first message of the (oldest-first) thread. Its
  // subject titles the conversation. A single-message thread (a root with
  // no replies) drops the conversation chrome and shows the normal detail.
  const root = thread.length > 0 ? thread[0] : message
  const isConversation = thread.length > 1
  const conversationTitle =
    root.subject && root.subject.trim() ? root.subject : "Conversation"

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="w-[calc(100vw-2rem)] sm:!max-w-2xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <MessageSquare className="h-5 w-5 text-primary" />
            {isConversation ? conversationTitle : "Message detail"}
          </DialogTitle>
          <DialogDescription>
            {isConversation ? (
              <>
                {thread.length} messages ·{" "}
                {new Date(root.timestamp).toLocaleString()}
              </>
            ) : (
              <>
                {new Date(message.timestamp).toLocaleString()} ·{" "}
                {relativeTime(message.timestamp)}
              </>
            )}
          </DialogDescription>
        </DialogHeader>

        {loading && (
          <div className="flex items-center gap-2 text-xs text-muted-foreground">
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
            Loading conversation…
          </div>
        )}

        {isConversation ? (
          // Flat chronological conversation: root pinned at the top, then
          // each message below in time order. The clicked message is
          // ring-highlighted so the admin sees which one they opened.
          <div className="space-y-2 max-h-[55vh] overflow-auto pr-1">
            {thread.map((msg) => (
              <ConversationRow
                key={msg.message_id}
                msg={msg}
                opened={msg.message_id === message.message_id}
              />
            ))}
          </div>
        ) : (
          <>
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
          </>
        )}

        <div className="text-[10px] font-mono text-muted-foreground break-all">
          Message ID: {message.message_id}
        </div>

        <DialogFooter>
          {/* v5.0.22: Reply opens the compose form pre-wired with
              parent_message_id pinned to this row (threads onto this
              conversation). */}
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
