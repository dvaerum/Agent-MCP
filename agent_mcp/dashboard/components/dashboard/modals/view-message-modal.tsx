"use client"

import { useEffect, useRef, useState, type Ref } from 'react'
import {
  MessageSquare,
  Send,
  Mail,
  MailOpen,
  CheckCheck,
  Clock,
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
import {
  priorityBadgeClass,
  messageTypeBadgeClass,
} from '@/components/dashboard/shared/message-badges'
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
  // Toggle a SPECIFIC message's read flag (per-row in the conversation,
  // or the opened message in the single-message view). Takes the message
  // so a multi-message thread is unambiguous about which one flips.
  onToggleRead: (msg: Message) => void
  onDelete: () => void
}

// One chat-style row in the flat chronological conversation. `opened`
// marks the message the user actually clicked so it gets a ring/accent
// and they can see which one they came in on.
function ConversationRow({
  msg,
  opened,
  onToggleRead,
  innerRef,
}: {
  msg: Message
  opened: boolean
  // Toggle THIS message's read flag. Per-row so, in a multi-message
  // thread, it's unambiguous which message is being marked (unlike a
  // single footer button that silently targets only the opened one).
  onToggleRead: (msg: Message) => void
  // Attached only to the opened row so the modal can scroll it into the
  // center of the (root-first) conversation scroll container — otherwise
  // a deep reply is buried below the fold.
  innerRef?: Ref<HTMLDivElement>
}) {
  // Direction cue: messages FROM admin lean one way, TO admin the other.
  // Purely visual — a subtle left accent so a back-and-forth reads as a
  // conversation rather than a flat log.
  const fromAdmin = msg.sender_id === 'admin'
  const isRead = msg.read === 1 || msg.read === true
  return (
    <div
      ref={innerRef}
      className={cn(
        "rounded-md border p-3 text-sm",
        fromAdmin
          ? "border-l-2 border-l-primary/40 bg-muted/30"
          : "border-l-2 border-l-muted-foreground/20",
        // Unread messages read stronger; read ones fade back, so the
        // read/unread split is legible at a glance down the thread.
        !isRead && "border-l-primary",
        opened && "ring-2 ring-primary ring-offset-1 ring-offset-background",
      )}
    >
      {/* Stack on mobile so the sender→recipient line gets the full row
          width — otherwise the read/unread + timestamp controls squeeze it
          to ~1 char and `break-all` stacks the ids vertically (one letter
          per line). Side-by-side again from sm up. */}
      <div className="flex flex-col gap-1 mb-1 sm:flex-row sm:items-center sm:justify-between sm:gap-2">
        <div className="flex min-w-0 items-center gap-1.5 text-xs font-mono">
          <span className="font-medium break-all">{msg.sender_id}</span>
          <span className="shrink-0 text-muted-foreground">→</span>
          <span className="break-all">{msg.recipient_id}</span>
          {opened && (
            <Badge variant="secondary" className="ml-1 shrink-0 text-[10px]">
              opened
            </Badge>
          )}
        </div>
        <div className="flex shrink-0 items-center gap-2">
          {/* Per-message read status + toggle. The button's label IS the
              current state, and clicking it flips this specific message —
              so both "which are read" and "what will this toggle" are
              unambiguous per row. */}
          <button
            type="button"
            onClick={() => onToggleRead(msg)}
            aria-pressed={isRead}
            aria-label={isRead ? "Mark this message unread" : "Mark this message read"}
            title={isRead ? "Mark this message unread" : "Mark this message read"}
            className={cn(
              "inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-medium border transition-colors",
              isRead
                ? "text-muted-foreground border-transparent hover:bg-muted"
                : "text-primary border-primary/40 hover:bg-primary/10",
            )}
          >
            {isRead ? (
              <><MailOpen className="h-3 w-3" /> read</>
            ) : (
              <><Mail className="h-3 w-3" /> unread</>
            )}
          </button>
          <span
            className="text-[11px] text-muted-foreground whitespace-nowrap"
            title={new Date(msg.timestamp).toLocaleString()}
          >
            {relativeTime(msg.timestamp)}
          </span>
        </div>
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

  // Ref on the opened row so we can scroll it into view once the thread
  // finishes loading. In a long, root-first conversation the clicked
  // message is buried below the fold — the ring highlight alone doesn't
  // help if the row is off-screen.
  const openedRowRef = useRef<HTMLDivElement>(null)

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

  // Once the thread has loaded, scroll the opened message into the
  // center of the conversation scroll container. Guarded on the ref
  // (null for a single-message thread — the conversation branch, and
  // thus the row ref, only renders when thread.length > 1, so this is a
  // no-op there). Runs after loading flips false so the rows exist.
  useEffect(() => {
    if (loading) return
    if (thread.length <= 1) return
    const el = openedRowRef.current
    if (!el) return
    el.scrollIntoView({ block: "center" })
  }, [loading, thread, openedId])

  if (!message) return null

  const isRead = message.read === 1 || message.read === true
  const isDelivered = message.delivered === 1 || message.delivered === true

  // The root is the first message of the (oldest-first) thread. Its
  // subject titles the conversation. A single-message thread (a root with
  // no replies) drops the conversation chrome and shows the normal detail.
  const root = thread.length > 0 ? thread[0] : message
  const isConversation = thread.length > 1

  // Toggle a message's read flag: tell the parent (persists + refreshes
  // its flat list) AND optimistically flip it in the local thread state so
  // this modal's per-row indicator updates instantly.
  const handleToggleRead = (m: Message) => {
    onToggleRead(m)
    const nextRead = !(m.read === 1 || m.read === true)
    setThread((prev) =>
      prev.map((t) =>
        t.message_id === m.message_id ? { ...t, read: nextRead } : t,
      ),
    )
  }
  const conversationTitle =
    root.subject && root.subject.trim() ? root.subject : "Conversation"

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {/* [&>*]:min-w-0 lets the grid children shrink below their
          min-content — without it a long, unbreakable recipient_id in the
          nowrap "Reply as …" footer button forces the whole grid wider
          than the dialog and the popup clips on the right (mobile/WebKit).
          overflow-hidden clips any residual. */}
      <DialogContent className="w-[calc(100vw-2rem)] sm:!max-w-2xl overflow-hidden [&>*]:min-w-0">
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
            {thread.map((msg) => {
              const opened = msg.message_id === message.message_id
              return (
                <ConversationRow
                  key={msg.message_id}
                  msg={msg}
                  opened={opened}
                  onToggleRead={handleToggleRead}
                  innerRef={opened ? openedRowRef : undefined}
                />
              )
            })}
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
                  <Badge variant="outline" className={messageTypeBadgeClass(message.message_type)}>
                    {message.message_type}
                  </Badge>
                </div>
              </div>
              <div>
                <div className="text-xs text-muted-foreground">Priority</div>
                <div>
                  <Badge variant="outline" className={priorityBadgeClass(message.priority)}>
                    {message.priority}
                  </Badge>
                </div>
              </div>
              <div>
                <div className="text-xs text-muted-foreground">Delivered</div>
                <div>
                  {isDelivered ? (
                    <Badge variant="secondary" className="gap-1">
                      <CheckCheck className="h-3 w-3" /> delivered
                    </Badge>
                  ) : (
                    <Badge variant="outline" className="gap-1 text-muted-foreground">
                      <Clock className="h-3 w-3" /> pending
                    </Badge>
                  )}
                </div>
              </div>
              <div>
                <div className="text-xs text-muted-foreground">Read</div>
                <div>
                  {/* Envelope: open = read, closed = unread. Click to
                      toggle this message's read flag. */}
                  <button
                    type="button"
                    onClick={() => handleToggleRead(message)}
                    aria-pressed={isRead}
                    aria-label={isRead ? "Mark unread" : "Mark read"}
                    title={isRead ? "Mark unread" : "Mark read"}
                  >
                    {isRead ? (
                      <Badge variant="secondary" className="gap-1 cursor-pointer hover:bg-secondary/70">
                        <MailOpen className="h-3 w-3" /> read
                      </Badge>
                    ) : (
                      <Badge variant="outline" className="gap-1 cursor-pointer border-primary/40 text-primary hover:bg-primary/10">
                        <Mail className="h-3 w-3" /> unread
                      </Badge>
                    )}
                  </button>
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
              conversation).
              feat/reply-as-recipient: a reply is the recipient answering
              the sender, so the button names WHOSE voice the operator will
              use — "Reply as {message.recipient_id}". Derived from the
              opened message directly; a degenerate broadcast recipient
              ("*"/empty) falls back to a bare "Reply". */}
          {/* min-w-0 + a truncating label so a long recipient_id
              ellipsizes instead of forcing the button (and the grid) past
              the viewport. */}
          <Button variant="outline" size="sm" onClick={onReply} className="min-w-0 max-w-full">
            <Send className="h-4 w-4 mr-1 shrink-0" />
            <span className="truncate min-w-0">
              {message.recipient_id && message.recipient_id !== "*"
                ? `Reply as ${message.recipient_id}`
                : "Reply"}
            </span>
          </Button>
          {/* Read-toggle moved onto each message row (the envelope icon:
              open = read, closed = unread) so it's unambiguous which
              message flips — a single footer button couldn't say that in a
              multi-message thread. */}
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
