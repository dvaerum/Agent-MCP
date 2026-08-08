"use client"

// Shared "Send directive" (ad-hoc poke) modal.
//
// Why this exists
// ---------------
// The poke — push a one-shot directive to an agent, delivered
// immediately if it's parked in wait_for_events, else queued as its
// highest-priority next check-in — used to live *only* as a per-row
// button on the Schedules page (wired to `s.agent_id` on a
// scheduled_directive row). An agent with no schedule had no row, so
// there was no way to poke it from the UI at all. This component is the
// single source of truth for the poke UX, mounted on both:
//
//   - the Agents page (a per-agent "Send directive" row action +
//     detail-dialog button), so *any* agent is reachable regardless of
//     whether it has a schedule; and
//   - the Schedules page (a standalone top-of-page button with an agent
//     picker, plus the legacy per-row shortcut).
//
// Two modes, one component:
//   - `lockedAgentId` set  → target is fixed (row action); no picker.
//   - `lockedAgentId` null → render the AgentSelect picker so the
//     operator chooses the target (standalone control).
//
// Delivered-vs-queued feedback: the backend response carries
// `delivered` (TRUE iff the agent had a parked wait_for_events waiter
// that the poke's waiter-wake just released). The toast reflects the
// real outcome — "Delivered to X" vs "Queued for X — will arrive on its
// next check-in" — instead of a generic "sent". We also surface the
// agent's *current* listening state in the modal (from the live
// `wait_for_events_in_flight` field the store already polls) so the
// operator knows what to expect before hitting Send.

import * as React from "react"
import { Radio, RadioTower } from "lucide-react"

import { Button } from "@/components/ui/button"
import { Label } from "@/components/ui/label"
import { Textarea } from "@/components/ui/textarea"
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { toastError, toastSuccess } from "@/components/ui/toast"
import { AgentSelect } from "@/components/dashboard/shared/agent-select"
import { apiClient } from "@/lib/api"
import { useDataStore } from "@/lib/stores/data-store"

export interface SendDirectiveModalProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  /**
   * When set, the target agent is fixed (a per-agent row/detail action)
   * and no picker is rendered. When null/undefined the modal renders the
   * shared AgentSelect so the operator chooses the target (the
   * standalone Schedules-page control).
   */
  lockedAgentId?: string | null
}

export function SendDirectiveModal({
  open,
  onOpenChange,
  lockedAgentId,
}: SendDirectiveModalProps): React.ReactElement {
  const locked = lockedAgentId != null && lockedAgentId !== ""
  const [pickedAgentId, setPickedAgentId] = React.useState<string | null>(null)
  const [prompt, setPrompt] = React.useState("")
  const [sending, setSending] = React.useState(false)

  const agentId = locked ? (lockedAgentId as string) : pickedAgentId

  // The picker (AgentSelect) and the listening hint both read the shared
  // useDataStore, which is only hydrated by `/api/all-data`
  // (`fetchAllData`) — and only the Agents/Overview pages call it. On a
  // cold Schedules-page load the store is empty, so without this the
  // standalone picker would render zero agents. Hydrate on open so the
  // modal is self-sufficient wherever it's mounted; force=true also
  // refreshes the live `wait_for_events_in_flight` flag the hint uses.
  const fetchAllData = useDataStore((s) => s.fetchAllData)

  // Re-seed on open: a locked target resets the picker so a stale prior
  // selection can't leak across opens.
  React.useEffect(() => {
    if (open) {
      setPickedAgentId(null)
      setPrompt("")
      void fetchAllData(true)
    }
  }, [open, lockedAgentId, fetchAllData])

  // Live listening state for the resolved target — read from the same
  // store field the Agents-table "WAITING" chip is derived from. Cheap
  // and correct: the store polls /api/all-data, which snapshots the
  // in-flight wait_for_events registry server-side.
  const data = useDataStore((s) => s.data)
  const targetAgent = React.useMemo(
    () => (agentId ? data?.agents?.find((a) => a.agent_id === agentId) ?? null : null),
    [data?.agents, agentId],
  )
  const listening = targetAgent?.wait_for_events_in_flight === true

  const reset = () => {
    setPickedAgentId(null)
    setPrompt("")
  }

  const submit = async () => {
    if (!agentId || !prompt.trim()) return
    setSending(true)
    try {
      const res = await apiClient.pokeAgent(agentId, { prompt })
      if (res.delivered) {
        toastSuccess(
          `Delivered to ${agentId} — the agent was listening and picked it up now.`,
          "Directive delivered",
        )
      } else {
        toastSuccess(
          `Queued for ${agentId} — will arrive on its next check-in (highest priority).`,
          "Directive queued",
        )
      }
      onOpenChange(false)
      reset()
    } catch (e) {
      toastError(e, "Failed to send directive")
    } finally {
      setSending(false)
    }
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(o) => {
        onOpenChange(o)
        if (!o) reset()
      }}
    >
      <DialogContent
        data-testid="send-directive-modal"
        className="w-[calc(100vw-2rem)] sm:!max-w-lg"
      >
        <DialogHeader>
          <DialogTitle>
            {locked ? `Send directive to ${lockedAgentId}` : "Send directive"}
          </DialogTitle>
          <DialogDescription>
            Push a one-shot directive to an agent. Delivered immediately if
            the agent is listening, otherwise queued as its highest-priority
            next check-in.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3">
          {!locked && (
            <div className="space-y-1">
              <Label htmlFor="send-directive-agent">Agent</Label>
              <AgentSelect
                id="send-directive-agent"
                ariaLabel="Target agent"
                value={pickedAgentId}
                onChange={setPickedAgentId}
                pinAdmin={false}
                placeholder="Select an agent"
              />
            </div>
          )}

          <div className="space-y-1">
            <Label htmlFor="send-directive-prompt">Directive</Label>
            <Textarea
              id="send-directive-prompt"
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              placeholder="e.g. stop and report your current status"
              data-testid="send-directive-prompt"
            />
          </div>

          {/* Live listening hint for the resolved target. Best-effort:
              reflects the last poll of wait_for_events_in_flight. */}
          {agentId && (
            listening ? (
              <div
                className="flex items-center gap-2 text-xs text-primary"
                data-testid="send-directive-listening"
              >
                <RadioTower className="h-3.5 w-3.5" />
                <span>
                  {agentId} is listening now — this will be delivered
                  immediately.
                </span>
              </div>
            ) : (
              <div
                className="flex items-center gap-2 text-xs text-muted-foreground"
                data-testid="send-directive-not-listening"
              >
                <Radio className="h-3.5 w-3.5" />
                <span>
                  {agentId} isn&apos;t listening — this will be queued for
                  its next check-in.
                </span>
              </div>
            )
          )}
        </div>

        <DialogFooter>
          <Button
            variant="outline"
            onClick={() => {
              onOpenChange(false)
              reset()
            }}
          >
            Cancel
          </Button>
          <Button
            onClick={() => void submit()}
            disabled={sending || !agentId || !prompt.trim()}
            data-testid="send-directive-submit"
          >
            {sending ? "Sending…" : "Send now"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
