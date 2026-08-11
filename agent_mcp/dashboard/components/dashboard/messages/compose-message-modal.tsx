"use client"

import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { Send } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { toastError } from "@/components/ui/toast"
import type { Message } from "@/lib/api"
import { FormDialog } from "@/components/dashboard/shared/form-dialog"
import {
  BROADCAST,
  MESSAGE_TYPES,
  PRIORITIES,
  callMessages,
} from "@/components/dashboard/messages/messages-api"

interface ComposeMessageModalProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  /**
   * The parent message to reply to, or null for a fresh compose. Read
   * on the open transition to seed the form; a reply is the parent's
   * RECIPIENT answering its SENDER (see the seeding logic below).
   */
  parent: Message | null
  /** Resolve a parent_message_id to a human-readable label. */
  labelForParent: (parentId: string | null) => string
  /** Refresh the list after a successful send. */
  onSent: () => void
}

export function ComposeMessageModal({
  open,
  onOpenChange,
  parent,
  labelForParent,
  onSent,
}: ComposeMessageModalProps) {
  const [composeRecipient, setComposeRecipient] = useState("")
  const [composeContent, setComposeContent] = useState("")
  const [composeType, setComposeType] = useState("text")
  const [composePriority, setComposePriority] = useState("normal")
  // v5.0.22 subject + reply state.
  const [composeSubject, setComposeSubject] = useState("")
  const [composeReplyParentId, setComposeReplyParentId] = useState<string | null>(
    null,
  )
  // feat/reply-as-recipient: when replying, the operator answers AS the
  // parent message's recipient (e.g. "manager"), back to its sender. This
  // holds the reply-as identity; null for a fresh compose or when the
  // operator is replying as themselves (admin — the normal case, which
  // sends no sender_id override).
  const [composeReplyAs, setComposeReplyAs] = useState<string | null>(null)
  const [suggestLoading, setSuggestLoading] = useState(false)
  const [suggestHint, setSuggestHint] = useState<string | null>(null)

  // Participants drive the Compose recipient dropdown only (needs the
  // BROADCAST "*" option, which is NOT an agent and is outside
  // <AgentSelect>'s contract; the hardcoded "admin" entry mirrors
  // data-store::shouldDisplayAgent so the compose UX matches the rest
  // of the dashboard).
  const [liveParticipants, setLiveParticipants] = useState<
    { agent_id: string; status?: string }[]
  >([])

  const loadParticipants = useCallback(async () => {
    try {
      const data = (await callMessages("POST", "/participants", {})) as {
        live?: unknown
      }
      const live = Array.isArray(data?.live) ? data.live : []
      setLiveParticipants(live)
    } catch {
      // Soft-fail: dropdown just shows the hardcoded admin entry.
      setLiveParticipants([])
    }
  }, [])

  // Seed the form on the open transition. A reply resets subject+content
  // and points the recipient/reply-as at the parent's participants; a
  // fresh compose (parent === null) preserves any in-progress draft so
  // reopening after an accidental close doesn't wipe it. Participants are
  // (re)pulled each open — only needed while the form is up.
  const wasOpen = useRef(false)
  useEffect(() => {
    if (open && !wasOpen.current) {
      void loadParticipants()
      if (parent) {
        // A reply is the message's RECIPIENT answering its SENDER. So we
        // reply AS `parent.recipient_id` and send back TO
        // `parent.sender_id`. Example: `backend-dev → manager` yields
        // "Reply as manager" sending `manager → backend-dev`.
        //
        // A listed row carries a concrete per-recipient `recipient_id`
        // (broadcast fan-out is stored per recipient), so `replyAs` is a
        // real agent. Guard the degenerate broadcast token "*"/empty:
        // fall back to the old behavior (reply to the other party as the
        // operator) rather than compose a message authored by "*".
        const me = "admin" // dashboard runs as admin per ADR-0003
        const replyAs = parent.recipient_id
        const replyTo = parent.sender_id
        const broadcastLike = !replyAs || replyAs === "*"
        if (broadcastLike) {
          const otherParty =
            parent.sender_id === me ? parent.recipient_id : parent.sender_id
          setComposeRecipient(otherParty)
          setComposeReplyAs(null)
        } else {
          setComposeRecipient(replyTo)
          // Only carry an override when actually acting AS an agent (i.e.
          // the reply-as identity is not the operator's own). Replying as
          // admin is the normal operator-replying-as-themselves case.
          setComposeReplyAs(replyAs === me ? null : replyAs)
        }
        setComposeSubject("")
        setComposeContent("")
        setComposeReplyParentId(parent.message_id)
      }
    }
    wasOpen.current = open
  }, [open, parent, loadParticipants])

  // Compose recipient list (live-only — admin pinned, then workers).
  // The currently-selected recipient is always appended if it isn't a
  // live participant: a reply can target an agent that has since gone
  // offline, and a Radix Select with a value that has no matching
  // <SelectItem> renders a blank trigger. Keeping the selected id in the
  // list guarantees the value always renders. BROADCAST is its own
  // hardcoded item, so it's excluded here.
  const recipientOptions = useMemo(() => {
    const ids = new Set<string>(["admin"])
    for (const a of liveParticipants) {
      if (a.agent_id) ids.add(a.agent_id)
    }
    if (composeRecipient && composeRecipient !== BROADCAST) {
      ids.add(composeRecipient)
    }
    return Array.from(ids)
  }, [liveParticipants, composeRecipient])

  // The send mutation. Throws on failure so the shared shell keeps the
  // dialog open with the operator's draft intact + surfaces the toast.
  const send = useCallback(async () => {
    if (!composeRecipient || !composeContent) return
    // BROADCAST sentinel maps to recipient_id="*" on the backend.
    const recipient =
      composeRecipient === BROADCAST ? "*" : composeRecipient
    const body: Record<string, unknown> = {
      recipient_id: recipient,
      message_content: composeContent,
      message_type: composeType,
      priority: composePriority,
    }
    if (composeReplyParentId) {
      body.parent_message_id = composeReplyParentId
    } else if (composeSubject.trim()) {
      body.subject = composeSubject.trim()
    }
    // feat/reply-as-recipient: when replying AS an agent (not the
    // operator's own identity), override the stored sender so the reply
    // is authored in that agent's voice. The backend validates + audits
    // this (operator-only). Omitted for a normal send / reply-as-admin.
    if (composeReplyAs) {
      body.sender_id = composeReplyAs
    }
    await callMessages("POST", "", body)
    // Reset the draft on success (the shell then toasts + closes).
    setComposeContent("")
    setComposeSubject("")
    setComposeReplyParentId(null)
    setComposeReplyAs(null)
    onSent()
  }, [
    composeRecipient,
    composeContent,
    composeType,
    composePriority,
    composeReplyParentId,
    composeSubject,
    composeReplyAs,
    onSent,
  ])

  // v5.0.22: ask the backend (which delegates to Ollama if
  // AGENT_MCP_SUBJECT_MODEL is configured) to propose a subject.
  const suggestSubject = async () => {
    if (!composeContent.trim()) return
    setSuggestLoading(true)
    setSuggestHint(null)
    try {
      const data = (await callMessages("POST", "/suggest-subject", {
        content: composeContent,
      })) as { subject?: unknown }
      if (data?.subject) {
        setComposeSubject(String(data.subject))
      } else {
        setSuggestHint(
          "No suggestion available — type a subject manually " +
            "(or set AGENT_MCP_SUBJECT_MODEL server-side to enable Ollama).",
        )
      }
    } catch (e) {
      // Soft-fail — the user can still type a subject manually.
      toastError(e, "Failed to suggest a subject")
    } finally {
      setSuggestLoading(false)
    }
  }

  return (
    <FormDialog
      open={open}
      onOpenChange={onOpenChange}
      title="Compose message"
      description="Send a message to an agent, or broadcast to all workers."
      icon={Send}
      wide
      onSubmit={send}
      submitLabel="Send"
      submittingLabel="Sending…"
      submitDisabled={!composeRecipient || !composeContent}
      successMessage="Message sent."
      errorMessage="Failed to send message"
    >
      <div className="grid gap-3 md:grid-cols-3">
        <div>
          <Label htmlFor="compose-recipient" className="text-xs">Recipient agent_id</Label>
          <Select value={composeRecipient} onValueChange={setComposeRecipient}>
            <SelectTrigger id="compose-recipient" aria-label="Recipient agent_id">
              <SelectValue placeholder="select agent" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={BROADCAST}>
                (broadcast to all workers)
              </SelectItem>
              {recipientOptions.map((id) => (
                <SelectItem key={id} value={id}>{id}</SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div>
          <Label htmlFor="compose-type" className="text-xs">Type</Label>
          <Select value={composeType} onValueChange={setComposeType}>
            <SelectTrigger id="compose-type" aria-label="Message type"><SelectValue /></SelectTrigger>
            <SelectContent>
              {MESSAGE_TYPES.map((t) => (
                <SelectItem key={t} value={t}>{t}</SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div>
          <Label htmlFor="compose-priority" className="text-xs">Priority</Label>
          <Select value={composePriority} onValueChange={setComposePriority}>
            <SelectTrigger id="compose-priority" aria-label="Priority"><SelectValue /></SelectTrigger>
            <SelectContent>
              {PRIORITIES.map((p) => (
                <SelectItem key={p} value={p}>{p}</SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      </div>
      {/* v5.0.22: Subject input + Suggest button. Hidden when replying —
          replies always have subject = NULL per the schema contract. */}
      {composeReplyParentId ? (
        <div className="rounded-md border border-muted bg-muted/30 px-3 py-2 text-xs text-muted-foreground">
          {/* feat/reply-as-recipient: make the operator's voice explicit —
              they are replying AS the parent's recipient, back TO its
              sender. Shown only when acting as an agent (composeReplyAs
              set); a plain reply-as-admin keeps the ordinary "reply to"
              line. */}
          {composeReplyAs ? (
            <div className="mb-1 font-medium text-foreground">
              Replying as {composeReplyAs} → {composeRecipient}
            </div>
          ) : null}
          ↳ reply to:{" "}
          <span className="font-medium text-foreground">
            {labelForParent(composeReplyParentId)}
          </span>
          <Button
            variant="ghost"
            size="sm"
            className="ml-2 h-6 px-2"
            onClick={() => {
              setComposeReplyParentId(null)
              setComposeReplyAs(null)
            }}
          >
            Cancel reply
          </Button>
        </div>
      ) : (
        <div>
          <Label htmlFor="compose-subject" className="text-xs">Subject</Label>
          <div className="flex gap-2">
            <Input
              id="compose-subject"
              aria-label="Subject"
              placeholder="Subject (optional — Suggest will fill from Ollama)"
              value={composeSubject}
              onChange={(e) => {
                setComposeSubject(e.target.value)
                if (suggestHint) setSuggestHint(null)
              }}
            />
            <Button
              type="button"
              variant="outline"
              onClick={suggestSubject}
              disabled={suggestLoading || !composeContent.trim()}
              title="Ask the configured Ollama model for a subject — POST /api/messages/suggest-subject"
            >
              {suggestLoading ? "…" : "Suggest"}
            </Button>
          </div>
          {suggestHint && (
            <p className="text-[11px] text-muted-foreground mt-1">
              {suggestHint}
            </p>
          )}
        </div>
      )}
      <div>
        <Label htmlFor="compose-content" className="text-xs">Content</Label>
        <textarea
          id="compose-content"
          aria-label="Content"
          className="w-full min-h-[100px] rounded-md border border-input bg-background p-2 text-sm"
          value={composeContent}
          onChange={(e) => setComposeContent(e.target.value)}
          placeholder="Your message"
        />
      </div>
    </FormDialog>
  )
}
