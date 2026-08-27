"use client"

import { useEffect, useMemo, useState } from "react"
import type { ReactElement } from "react"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { Switch } from "@/components/ui/switch"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { FormDialog } from "@/components/dashboard/shared/form-dialog"
import { useAllData } from "@/lib/queries/all-data"
import type { Agent } from "@/lib/api"

// AoE session id: 16 lowercase hex chars. Backend re-validates and
// 400s bad input; this drives the live hint + submit-disable.
export const AOE_SESSION_ID_RE = /^[0-9a-f]{16}$/

/** The diff payload POSTed to /api/agents/<id>/edit. */
export interface AgentEditUpdates {
  color?: string
  working_directory?: string
  aoe_session_id?: string
  auto_event_loop?: boolean
  agent_role?: 'worker' | 'manager'
  profile?: string
}

// Event-coord PR-1: SQLite BOOLEAN columns arrive as JS number (0/1)
// after the JSON round-trip; the JSON serializer never coerces them
// back to true/false. Default to TRUE when missing — matches the
// migration's `DEFAULT 1` backfill semantics.
export const coerceAutoEventLoop = (raw: unknown): boolean => {
  if (typeof raw === 'boolean') return raw
  if (typeof raw === 'number') return raw !== 0
  if (typeof raw === 'string') {
    const s = raw.trim().toLowerCase()
    if (s === 'true' || s === '1') return true
    if (s === 'false' || s === '0') return false
  }
  return true
}

/**
 * Edit dialog — admin-editable agent fields. Backed by
 * POST /api/agents/<id>/edit (color / working_directory / profile /
 * role / aoe binding / per-agent event-loop toggle).
 *
 * The dialog owns the FORM (seeding, diffing, validation); the caller
 * owns the MUTATION via `onSave`, which is expected to re-throw on
 * failure so the dialog stays open with the operator's edits intact.
 * That is the same split the Memories page uses for `EditMemoryModal`
 * — and it is why this dialog no longer carries its own `setError`
 * banner: mutation errors are surfaced by the caller's shared toast.
 *
 * Adopts the shared <FormDialog> shell. `onSave` already toasts (the
 * caller's shared toast), so FormDialog's own successMessage is
 * omitted — a genuine change (updates present) still gets a real
 * mutation + the caller's toast; a no-op diff (nothing changed) just
 * closes silently, matching the pre-migration behavior exactly (no
 * API call, no toast either way). Per-field disabling during submit
 * (previously gated on a local `busy` flag) is dropped — FormDialog
 * doesn't expose its `submitting` state to children, and no other
 * migrated dialog in this codebase disables individual fields during
 * submit either (they rely on the shell's own submit-button guard) —
 * this keeps the migration consistent with that convention rather
 * than growing FormDialog's API for one caller.
 */
export function EditAgentDialog({
  agent,
  open,
  onOpenChange,
  onSave,
}: {
  agent: Agent | null
  open: boolean
  onOpenChange: (open: boolean) => void
  onSave: (agentId: string, updates: AgentEditUpdates) => Promise<void>
}): ReactElement {
  const [color, setColor] = useState('')
  const [workingDirectory, setWorkingDirectory] = useState('')
  const [aoeSessionId, setAoeSessionId] = useState('')
  // Event-coord PR-1: per-agent wake-loop toggle. Default true matches
  // the migration's DEFAULT 1 backfill.
  const [autoEventLoop, setAutoEventLoop] = useState(true)
  // Phase 2 Wave 2b (plan §2e): role tier. Default 'worker' matches
  // the agents.agent_role column default (Wave 1a, v5.0.61, PR #182).
  const [agentRole, setAgentRole] = useState<'worker' | 'manager'>('worker')
  // Agent self-description (migration 0018). Operator-curatable here;
  // the agent also edits its own via the update_agent_profile MCP tool.
  const [profile, setProfile] = useState('')

  // Read the global event-loop flag from project_context so we can
  // disable + annotate the per-agent toggle when global is OFF (per
  // the locked-decisions table in the event-coord plan).
  const dataAll = useAllData()
  const globalEventLoop = useMemo<boolean>(() => {
    const row = dataAll?.context?.find(
      (c) => (c as { context_key?: unknown }).context_key === 'config_auto_event_loop_global'
    )
    if (!row) return true  // unset ⇒ default ON
    const raw = (row as { value?: unknown }).value
    if (typeof raw === 'boolean') return raw
    if (typeof raw === 'string') {
      const s = raw.trim().toLowerCase()
      if (s === 'true') return true
      if (s === 'false') return false
      try {
        const parsed = JSON.parse(s)
        if (typeof parsed === 'boolean') return parsed
      } catch { /* fall through */ }
    }
    return true
  }, [dataAll])

  // Re-seed form whenever the dialog opens for a *different* agent.
  // With live-lookup useDialog (Candidate D, 2026-06-02) the agent
  // prop reference can change on every background refresh; keying the
  // effect on agent_id keeps the admin's in-progress field edits
  // alive instead of being clobbered by the latest store snapshot.
  const agentId = agent?.agent_id
  useEffect(() => {
    if (!open || !agent) return
    setColor(agent.color || '')
    setWorkingDirectory(agent.working_directory || '')
    setAoeSessionId(agent.aoe_session_id || '')
    // Event-coord PR-1: SQLite stores BOOLEAN as INTEGER 0/1, which
    // arrives as a JS number after the JSON round-trip. Coerce to
    // strict boolean and default to TRUE when the field is missing
    // (legacy backends).
    setAutoEventLoop(coerceAutoEventLoop(agent.auto_event_loop))
    // Wave 2b: seed the Role dropdown from the row, falling back to
    // 'worker' for any legacy agent whose row pre-dates Wave 1a.
    setAgentRole(
      agent.agent_role === 'manager' ? 'manager' : 'worker'
    )
    setProfile(agent.profile || '')
    // Intentionally key on agentId, not the agent object — see above.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, agentId])

  // Live AoE-session-id validity. Empty is valid (clears the binding);
  // otherwise it must be 16 lowercase hex chars. Drives the inline hint
  // and the Save-button disable so the operator learns before submit —
  // the on-submit check below is kept as the last-resort guard.
  const aoeTrimmedLive = aoeSessionId.trim().toLowerCase()
  const aoeValid = aoeTrimmedLive === '' || AOE_SESSION_ID_RE.test(aoeTrimmedLive)

  const handleSubmit = async () => {
    if (!agent) return
    const updates: AgentEditUpdates = {}
    if (color !== (agent.color || '')) {
      updates.color = color
    }
    if (workingDirectory !== (agent.working_directory || '')) {
      updates.working_directory = workingDirectory
    }
    const aoeTrimmed = aoeSessionId.trim().toLowerCase()
    if (aoeTrimmed !== (agent.aoe_session_id || '')) {
      // Last-resort guard — submitDisabled (aoeValid) already keeps
      // the UI from reaching this with malformed input.
      if (aoeTrimmed && !AOE_SESSION_ID_RE.test(aoeTrimmed)) {
        throw new Error(
          'AoE session id must be 16 lowercase hex chars (or empty to clear).',
        )
      }
      updates.aoe_session_id = aoeTrimmed
    }
    // Event-coord PR-1: only send if changed from the agent's current
    // value (or from the default TRUE when the field is absent).
    const currentAutoEventLoop = coerceAutoEventLoop(agent.auto_event_loop)
    if (autoEventLoop !== currentAutoEventLoop) {
      updates.auto_event_loop = autoEventLoop
    }
    // Wave 2b: same diff pattern for the role tier. Only ship the
    // field when it has actually changed so an unrelated capability
    // edit doesn't redundantly re-write the role.
    const currentRole: 'worker' | 'manager' =
      agent.agent_role === 'manager' ? 'manager' : 'worker'
    if (agentRole !== currentRole) {
      updates.agent_role = agentRole
    }
    // Self-description: only ship when changed (empty string clears it).
    if (profile !== (agent.profile || '')) {
      updates.profile = profile
    }
    if (Object.keys(updates).length === 0) {
      // No-op diff: close silently (no API call, no toast) — same as
      // pre-migration. Returning without throwing lets the shell close
      // via its normal success path; successMessage is omitted so
      // nothing toasts.
      return
    }
    await onSave(agent.agent_id, updates)
  }

  return (
    <FormDialog
      open={open}
      onOpenChange={onOpenChange}
      title={`Edit agent ${agent?.agent_id}`}
      description="Update the agent's appearance, working directory, self-description, role, and event-loop settings. Status changes use Terminate / Restore / Purge."
      onSubmit={handleSubmit}
      submitLabel="Save changes"
      submittingLabel="Saving…"
      submitDisabled={!aoeValid}
    >
      <div>
        <label className="text-xs font-medium text-muted-foreground uppercase tracking-wider block mb-2">
          Color
        </label>
        <div className="flex items-center gap-2">
          <Input
            type="color"
            value={color || '#888888'}
            onChange={(e) => setColor(e.target.value)}
            className="h-9 w-12 p-1"
          />
          <Input
            value={color}
            onChange={(e) => setColor(e.target.value)}
            placeholder="#888888"
            className="font-mono"
          />
        </div>
      </div>
      <div>
        <label className="text-xs font-medium text-muted-foreground uppercase tracking-wider block mb-2">
          Working Directory
        </label>
        <Input
          value={workingDirectory}
          onChange={(e) => setWorkingDirectory(e.target.value)}
          placeholder="/workspace/agent"
          className="font-mono text-sm"
        />
      </div>
      <div>
        <label
          htmlFor="edit-agent-profile"
          className="text-xs font-medium text-muted-foreground uppercase tracking-wider block mb-2"
        >
          Self-description
        </label>
        <Textarea
          id="edit-agent-profile"
          value={profile}
          onChange={(e) => setProfile(e.target.value)}
          placeholder="What this agent does, how it works, what to ask it…"
          rows={4}
          className="text-sm"
        />
        <p className="text-[10px] text-muted-foreground mt-1">
          The agent&apos;s own profile — normally authored by the agent
          via update_agent_profile, editable here for curation. Markdown
          supported (rendered in the details view). Empty clears it.
        </p>
      </div>
      <div>
        <label className="text-xs font-medium text-muted-foreground uppercase tracking-wider block mb-2">
          AoE Session ID
        </label>
        <Input
          value={aoeSessionId}
          onChange={(e) => setAoeSessionId(e.target.value)}
          placeholder="16-char lowercase hex, e.g. 551e7a79d11f435b"
          className="font-mono text-sm"
          maxLength={16}
          pattern="[0-9a-f]{16}"
          aria-invalid={!aoeValid}
        />
        {!aoeValid && (
          <p className="text-[10px] text-destructive mt-1">
            Must be 16 lowercase hex chars (0-9, a-f), or empty to clear.
          </p>
        )}
        <p className="text-[10px] text-muted-foreground mt-1">
          Binds this agent to a specific Agents-of-Empires tmux session for the
          notification side-channel. Leave empty to fall back to title-match.
        </p>
      </div>
      {/*
        Phase 2 Wave 2b (plan §2e): Role dropdown — promote a worker to
        manager (or demote). The server-side check in
        /api/agents/<id>/edit 422s anything outside {'worker', 'manager'};
        the column CHECK constraint is the last-resort guard.
      */}
      <div>
        <label
          htmlFor="edit-agent-role"
          className="text-xs font-medium text-muted-foreground uppercase tracking-wider block mb-2"
        >
          Role
        </label>
        <Select
          value={agentRole}
          onValueChange={(value) => setAgentRole(value as 'worker' | 'manager')}
        >
          <SelectTrigger id="edit-agent-role">
            <SelectValue placeholder="Select role" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="worker">Worker</SelectItem>
            <SelectItem value="manager">Manager</SelectItem>
          </SelectContent>
        </Select>
        <p className="text-[10px] text-muted-foreground mt-1">
          Workers run assigned tasks. Managers also supervise
          subordinates (assign tasks, edit agent fields).
        </p>
      </div>
      {/*
        Event-coord PR-1: per-agent wake-loop toggle. Default TRUE.
        Disabled (greyed) when the global flag is OFF — the wake-loop
        bootstrap requires BOTH flags ON per the locked-decisions table
        in the event-coord plan. Note text explicitly directs the
        operator to Settings to flip the global flag.
      */}
      <div className="rounded-md border p-3 space-y-2">
        <div className="flex items-center justify-between gap-3">
          <div className="space-y-0.5">
            <label
              htmlFor="agent-edit-auto-event-loop"
              className="text-xs font-medium text-foreground uppercase tracking-wider"
            >
              Auto event-loop
            </label>
            <p className="text-[11px] text-muted-foreground leading-snug">
              When on (default), this agent receives the wake-loop
              bootstrap and auto-calls wait_for_events on connect.
              Both this toggle and the global Settings toggle must
              be on.
            </p>
          </div>
          <Switch
            id="agent-edit-auto-event-loop"
            checked={autoEventLoop}
            onCheckedChange={setAutoEventLoop}
            disabled={!globalEventLoop}
          />
        </div>
        {!globalEventLoop && (
          <p className="text-[11px] text-warning">
            Global event-loop is disabled — toggle it on in
            Settings to enable per-agent control.
          </p>
        )}
      </div>
    </FormDialog>
  )
}
