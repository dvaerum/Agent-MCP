"use client"

import React, { useEffect, useState } from "react"
import { Settings, RefreshCw } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Switch } from "@/components/ui/switch"
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { apiClient, type ProjectSetting } from "@/lib/api"

// Per-project agent_messages retention. Stored as an integer count of
// days in project_settings["config_message_retention_days"]; absent or
// 0 means "keep forever" (upstream behavior). The background pruner in
// features.message_retention enforces it.
const MESSAGE_RETENTION_KEY = "config_message_retention_days"

// Per-project worker-permission policies. Each lives in the dedicated
// project_settings store under a `config_*` key (ADR-0016 — settings
// are operational config, separate from the agent-authored memories in
// project_context). Reads come from GET /api/settings-data; writes go
// through PUT /settings/<key> / POST /settings (see lib/api.ts:
// updateSetting / createSetting), authenticated via the operator
// session cookie set on /agent-mcp/login.
interface PolicySpec {
  key: string
  title: string
  description: string
  // What the system does when the key is absent from project_settings.
  // Mirrors the defaults applied by the backend tool gates.
  default: boolean
}

const POLICIES: PolicySpec[] = [
  {
    key: "config_allow_worker_to_worker",
    title: "Allow worker-to-worker messaging",
    description:
      "When off (default), workers can only send messages to the admin. When on, workers may use send_agent_message with any agent as recipient.",
    default: false,
  },
  {
    key: "config_allow_worker_self_assign",
    title: "Allow workers to self-assign tasks",
    description:
      "When on (default), workers may call assign_task using their own agent_token. When off, only the admin may assign tasks.",
    default: true,
  },
  {
    key: "config_allow_worker_update_own_status",
    title: "Allow workers to update their own task status",
    description:
      "When on (default), workers may call update_task_status on tasks they are assigned to. When off, only the admin may transition task status.",
    default: true,
  },
  {
    key: "config_allow_worker_create_unassigned",
    title: "Allow workers to file unassigned tasks",
    description:
      "When on (default), workers may call assign_task with no agent_token to file work into the unassigned pool for any peer to claim. When off, only the admin may create tasks.",
    default: true,
  },
  {
    key: "config_aoe_notify_enabled",
    title: "Notify Agents-of-Empires on new messages",
    description:
      "When on, send_agent_message also POSTs a tmux-pane wake-up to a local Agents-of-Empires (AoE) instance so the recipient notices the message even between polls. Disabled by default. Configure config_aoe_base_url, config_aoe_bearer_token (secret), and config_aoe_notify_template in the AoE integration card below (sysadmin-only). The message body itself is never forwarded — only {sender}, {recipient}, {message_id} are interpolated.",
    default: false,
  },
  {
    // Event-coord PR-1: global wake-loop toggle. When on, the server
    // appends the wake-loop bootstrap text to serverInfo.instructions
    // (PR-2 wires the injection) for every agent whose own
    // auto_event_loop flag is also on. When off, NO agent receives
    // the wake-loop instructions regardless of its per-agent flag —
    // and in-flight wait_for_events calls return a stop_listening
    // event (PR-2). Default ON: every existing deployment opts in
    // automatically once PR-2 ships; flip it off here to keep the
    // legacy "human-prompts-every-turn" workflow.
    key: "config_auto_event_loop_global",
    title: "Agent event-loop (wake on inbox / task events)",
    description:
      "When on (default), worker agents are instructed to call wait_for_events on session start and after each event, so they wake automatically when messages or tasks arrive. When off, the wake-loop bootstrap text is omitted from serverInfo.instructions for every agent — workers fall back to human-prompted polling. Per-agent overrides live on the Agents tab (disabled here also disables every per-agent toggle).",
    default: true,
  },
]

interface PolicyState {
  value: boolean
  exists: boolean   // true once we've seen the key in project_settings
  pending: boolean  // true while a PUT/POST is in flight
}

// Wave 2 (cleanup-wave-2): the ``adminToken()`` helper is gone.
// Dashboard mutations authenticate via the operator session cookie
// set on /agent-mcp/login — the browser attaches it to every
// ``apiClient`` call automatically.

// project_settings stores values as JSON-serialised strings. Booleans
// arrive as either the bare boolean `true` / `false`, the string
// "true" / "false", or wrapped inside an object. Be liberal in what
// we accept.
function coerceBool(raw: unknown, fallback: boolean): boolean {
  if (typeof raw === "boolean") return raw
  if (typeof raw === "string") {
    const s = raw.trim().toLowerCase()
    if (s === "true") return true
    if (s === "false") return false
    // Possibly JSON-encoded.
    try {
      const parsed = JSON.parse(s)
      if (typeof parsed === "boolean") return parsed
    } catch {
      /* fall through */
    }
  }
  return fallback
}

// project_settings stores retention as a JSON-encoded integer. Tolerate
// quoted strings and floats; clamp to >= 0.
function coerceNonNegInt(raw: unknown): number {
  let n: number
  if (typeof raw === "number") {
    n = raw
  } else if (typeof raw === "string") {
    const s = raw.trim().replace(/^"|"$/g, "")
    try {
      const parsed = JSON.parse(s)
      n = typeof parsed === "number" ? parsed : Number(s)
    } catch {
      n = Number(s)
    }
  } else {
    n = NaN
  }
  if (!Number.isFinite(n) || n < 0) return 0
  return Math.floor(n)
}

// Retention bounds (UX-09). The backend (features/message_retention.py)
// stores a plain non-negative integer day count: 0 = disabled (keep
// forever), any positive integer is a valid window; there is no upper
// bound enforced server-side. So the only real constraints are
// "whole number" and ">= 0". Validate against those on blur and show
// an inline hint rather than silently coercing garbage on Save.
const RETENTION_MIN = 0

// Returns a human-readable error when the draft is NOT a valid
// retention value (blank, negative, fractional, or non-numeric), or
// null when it is acceptable to save as-is.
function validateRetention(draft: string): string | null {
  const s = draft.trim()
  if (s === "") return "Enter a number of days (0 = keep forever)."
  if (!/^\d+$/.test(s)) {
    return `Must be a whole number of days ≥ ${RETENTION_MIN} (0 = keep forever).`
  }
  return null
}

interface RetentionState {
  // Current saved value (last seen from server). 0 means disabled.
  saved: number
  // What the user has typed into the input (string for free editing).
  draft: string
  exists: boolean
  pending: boolean
}

export function SettingsDashboard() {
  const [state, setState] = useState<Record<string, PolicyState>>(() => {
    const initial: Record<string, PolicyState> = {}
    for (const p of POLICIES) {
      initial[p.key] = { value: p.default, exists: false, pending: false }
    }
    return initial
  })
  const [retention, setRetention] = useState<RetentionState>({
    saved: 0,
    draft: "0",
    exists: false,
    pending: false,
  })
  // The raw project_settings rows from the last refresh — the AoE
  // integration card reads its keys out of this list.
  const [settingsRows, setSettingsRows] = useState<ProjectSetting[]>([])
  // Show the inline validation hint only after the field has been
  // blurred (UX-09), so we don't nag mid-typing.
  const [retentionTouched, setRetentionTouched] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // Event-coord PR-3: count of agents currently inside a
  // `wait_for_events` long-poll call. Read from /api/all-data's new
  // per-agent `wait_for_events_in_flight` boolean and refreshed
  // alongside the policy toggles. Surfaces under the global
  // event-loop toggle so operators have a live "how many agents are
  // sleeping right now" signal.
  const [agentsInWait, setAgentsInWait] = useState<number>(0)

  const refresh = async () => {
    setLoading(true)
    setError(null)
    try {
      // ADR-0016: toggles/knobs come from the project_settings store
      // (GET /api/settings-data), not from getAllData().context — the
      // config rows no longer live in project_context at all. The
      // agents list (for the in-wait counter) still comes from
      // /api/all-data.
      const [settingsRes, all] = await Promise.all([
        apiClient.getSettingsData(),
        apiClient.getAllData(),
      ])
      const rows = settingsRes.settings ?? []
      setSettingsRows(rows)
      const agents = (all.agents ?? []) as Array<{ wait_for_events_in_flight?: boolean }>
      setAgentsInWait(
        agents.filter((a) => a.wait_for_events_in_flight === true).length,
      )
      setState((prev) => {
        const next = { ...prev }
        for (const p of POLICIES) {
          const row = rows.find((c) => c.context_key === p.key)
          if (row) {
            next[p.key] = {
              value: coerceBool(row.value, p.default),
              exists: true,
              pending: false,
            }
          } else {
            next[p.key] = { value: p.default, exists: false, pending: false }
          }
        }
        return next
      })
      const retRow = rows.find(
        (c) => c.context_key === MESSAGE_RETENTION_KEY,
      )
      if (retRow) {
        const days = coerceNonNegInt(retRow.value)
        setRetention({ saved: days, draft: String(days), exists: true, pending: false })
      } else {
        setRetention({ saved: 0, draft: "0", exists: false, pending: false })
      }
      setRetentionTouched(false)
    } catch (e: any) {
      setError(e.message ?? String(e))
    } finally {
      setLoading(false)
    }
  }

  const saveRetention = async () => {
    // UX-09: refuse to save invalid input instead of silently
    // coercing it. Surface the hint and bail.
    if (validateRetention(retention.draft) !== null) {
      setRetentionTouched(true)
      return
    }
    const next = coerceNonNegInt(retention.draft)
    if (next === retention.saved && retention.exists) {
      // No change — nothing to do.
      return
    }
    setRetention((s) => ({ ...s, pending: true }))
    setError(null)
    try {
      if (retention.exists) {
        await apiClient.updateSetting(MESSAGE_RETENTION_KEY, {
          context_value: next,
        })
      } else {
        await apiClient.createSetting({
          context_key: MESSAGE_RETENTION_KEY,
          context_value: next,
          description:
            "Days of read agent_messages to keep before background pruner deletes them. 0 = disabled.",
        })
      }
      setRetention({ saved: next, draft: String(next), exists: true, pending: false })
      setRetentionTouched(false)
    } catch (e: any) {
      setRetention((s) => ({ ...s, pending: false }))
      setError(e.message ?? String(e))
    }
  }

  useEffect(() => {
    refresh()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Toggle a policy. Optimistic UI: flip immediately, send PUT (or
  // POST if the key hasn't been created yet), revert on failure.
  const toggle = async (policy: PolicySpec, nextValue: boolean) => {
    const prevState = state[policy.key]
    setState((s) => ({
      ...s,
      [policy.key]: { ...prevState, value: nextValue, pending: true },
    }))
    setError(null)
    try {
      if (prevState.exists) {
        await apiClient.updateSetting(policy.key, {
          context_value: nextValue,
        })
      } else {
        await apiClient.createSetting({
          context_key: policy.key,
          context_value: nextValue,
          description: policy.title,
        })
      }
      setState((s) => ({
        ...s,
        [policy.key]: { value: nextValue, exists: true, pending: false },
      }))
    } catch (e: any) {
      // Revert the optimistic flip.
      setState((s) => ({ ...s, [policy.key]: { ...prevState, pending: false } }))
      setError(e.message ?? String(e))
    }
  }

  return (
    <div className="p-6 space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold flex items-center gap-2">
          <Settings className="h-6 w-6 text-primary" />
          Settings
        </h1>
        <div className="flex gap-2">
          <Button variant="outline" size="sm" onClick={refresh} disabled={loading}>
            <RefreshCw className={`h-4 w-4 mr-1 ${loading ? "animate-spin" : ""}`} />
            Refresh
          </Button>
        </div>
      </div>

      {error && (
        <div className="rounded-md border border-destructive/30 bg-destructive/10 text-destructive p-3 text-sm">
          {error}
        </div>
      )}

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Worker permissions</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          {loading && !error && (
            <div className="text-sm text-muted-foreground">Loading…</div>
          )}
          {POLICIES.map((p) => {
            const s = state[p.key]
            return (
              <div
                key={p.key}
                /* CC-18 audit 2026-06-02: stacked at <sm:, row at sm+.
                   The Switch drops below the description on mobile so
                   it doesn't squash the policy-description column.
                   `sm:items-start` keeps the Switch top-aligned with
                   the title on desktop. */
                className="flex flex-col sm:flex-row sm:items-start sm:justify-between gap-3 py-3 border-b last:border-b-0"
              >
                <div className="flex-1 min-w-0">
                  <div className="font-medium text-sm">{p.title}</div>
                  <div className="text-xs text-muted-foreground mt-1">
                    {p.description}
                  </div>
                  <div className="text-[10px] text-muted-foreground mt-1 font-mono break-all">
                    {p.key}
                    {!s.exists && (
                      <span className="ml-2 italic">
                        (using default: {p.default ? "allow" : "deny"})
                      </span>
                    )}
                  </div>
                  {/* Event-coord PR-3: live "X agents currently in
                      wait" count rendered only under the global
                      event-loop toggle. Read-only — the count comes
                      from /api/all-data's `wait_for_events_in_flight`
                      booleans. Hidden when the global toggle is OFF
                      since no agent should be in wait in that state
                      anyway (existing in-flight calls receive
                      stop_listening per PR-2). */}
                  {p.key === "config_auto_event_loop_global" && s.value && (
                    <div className="text-xs text-muted-foreground mt-2">
                      {agentsInWait} agent{agentsInWait === 1 ? "" : "s"}{" "}
                      currently in wait.
                    </div>
                  )}
                </div>
                <div className="flex-shrink-0 sm:pt-1 self-end sm:self-auto">
                  <Switch
                    checked={s.value}
                    disabled={s.pending}
                    onCheckedChange={(v) => toggle(p, v)}
                    aria-label={p.title}
                  />
                </div>
              </div>
            )
          })}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Message retention</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex flex-col sm:flex-row sm:items-start sm:justify-between gap-3 py-2">
            <div className="flex-1 min-w-0">
              <div className="font-medium text-sm">
                Auto-delete read messages older than
              </div>
              <div className="text-xs text-muted-foreground mt-1">
                The background pruner runs once every 24 hours and deletes
                rows from agent_messages where read=1 and timestamp is older
                than the configured window. Unread messages are never
                pruned. Set to 0 to disable (keep forever).
              </div>
              <div className="text-[10px] text-muted-foreground mt-1 font-mono break-all">
                {MESSAGE_RETENTION_KEY}
                {!retention.exists && (
                  <span className="ml-2 italic">
                    (using default: keep forever)
                  </span>
                )}
              </div>
            </div>
            <div className="flex-shrink-0 sm:pt-1 flex flex-col gap-1 self-end sm:self-auto sm:items-end">
              <div className="flex items-center gap-2">
                <Input
                  type="number"
                  min={0}
                  step={1}
                  inputMode="numeric"
                  value={retention.draft}
                  disabled={retention.pending}
                  onChange={(e) =>
                    setRetention((s) => ({ ...s, draft: e.target.value }))
                  }
                  onBlur={() => setRetentionTouched(true)}
                  aria-invalid={validateRetention(retention.draft) !== null}
                  className="w-24"
                  aria-label="Message retention days"
                />
                <span className="text-xs text-muted-foreground">days</span>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={saveRetention}
                  disabled={
                    retention.pending ||
                    validateRetention(retention.draft) !== null ||
                    (retention.exists &&
                      coerceNonNegInt(retention.draft) === retention.saved)
                  }
                >
                  Save
                </Button>
              </div>
              {retentionTouched && validateRetention(retention.draft) && (
                <div className="text-xs text-destructive" role="alert">
                  {validateRetention(retention.draft)}
                </div>
              )}
            </div>
          </div>
        </CardContent>
      </Card>

      <AoeConfigCard rows={settingsRows} onSaved={refresh} />

      <AoeHealthCard />
    </div>
  )
}

// ── AoE integration config (ADR-0016 feature parity) ────────────────
//
// The config_aoe_* keys were previously edited via the Memories tab;
// after the settings-store cutover they no longer appear there, so
// this card is their editing surface. Server-side the keys are
// SYSADMIN-only to write (SSRF / bearer-exfil rationale — see
// tools/project_settings_tools.py); a non-sysadmin operator's save
// surfaces the 403 error inline. The bearer token is write-only: the
// server returns "[redacted]" for non-confirmed tiers, which renders
// as "value set — enter a new value to replace".

const REDACTED = "[redacted]"

interface AoeFieldSpec {
  key: string
  label: string
  placeholder: string
  secret?: boolean
}

const AOE_FIELDS: AoeFieldSpec[] = [
  {
    key: "config_aoe_base_url",
    label: "Base URL",
    placeholder: "http://127.0.0.1:8181",
  },
  {
    key: "config_aoe_bearer_token",
    label: "Bearer token",
    placeholder: "paste a token (stored, never displayed)",
    secret: true,
  },
  {
    key: "config_aoe_bearer_token_file",
    label: "Bearer token file",
    placeholder: "/run/secrets/aoe-token (rotation-friendly)",
    secret: true,
  },
  {
    key: "config_aoe_notify_template",
    label: "Notify template",
    placeholder: "{sender} → {recipient} ({message_id})",
  },
  {
    key: "config_aoe_timeout_ms",
    label: "Timeout (ms)",
    placeholder: "2000",
  },
]

// project_settings values are JSON-encoded strings — unwrap one layer
// for display ("\"http://x\"" → "http://x"; numbers → their digits).
function coerceDisplayString(raw: unknown): string {
  if (raw == null) return ""
  if (typeof raw !== "string") return String(raw)
  try {
    const parsed = JSON.parse(raw)
    if (typeof parsed === "string") return parsed
    if (typeof parsed === "number" || typeof parsed === "boolean") {
      return String(parsed)
    }
  } catch {
    /* stored as a bare string */
  }
  return raw
}

function AoeConfigCard({
  rows,
  onSaved,
}: {
  rows: ProjectSetting[]
  onSaved: () => void
}) {
  const [drafts, setDrafts] = useState<Record<string, string>>({})
  const [pendingKey, setPendingKey] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const rowFor = (key: string) => rows.find((r) => r.context_key === key)

  const save = async (field: AoeFieldSpec) => {
    const draft = drafts[field.key]
    if (draft === undefined) return
    setPendingKey(field.key)
    setError(null)
    try {
      // Timeout is an integer knob; everything else is a string.
      const value =
        field.key === "config_aoe_timeout_ms"
          ? Number(draft)
          : draft
      await apiClient.updateSetting(field.key, {
        context_value: value,
        description: `AoE integration: ${field.label}`,
      })
      setDrafts((d) => {
        const next = { ...d }
        delete next[field.key]
        return next
      })
      onSaved()
    } catch (e: any) {
      // The server-side sysadmin gate is authoritative — a 403 lands
      // here with the tool's SSRF-rationale message.
      setError(e?.message ?? String(e))
    } finally {
      setPendingKey(null)
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">AoE integration</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <p className="text-xs text-muted-foreground">
          Where the server sends Agents-of-Empires wake-up notifications.
          These keys are <span className="font-medium">sysadmin-only</span>{" "}
          — they point the host&apos;s outbound requests, so a per-project
          operator&apos;s save is rejected by the server.
        </p>
        {error && (
          <div
            className="rounded-md border border-destructive/30 bg-destructive/10 text-destructive p-3 text-sm"
            role="alert"
          >
            {error}
          </div>
        )}
        {AOE_FIELDS.map((field) => {
          const row = rowFor(field.key)
          const stored = row ? row.value : undefined
          const isRedacted = stored === REDACTED
          const displayValue = field.secret
            ? "" // write-only — never prefill a credential input
            : coerceDisplayString(stored)
          const draft = drafts[field.key]
          const inputValue = draft !== undefined ? draft : displayValue
          const isSet = row !== undefined
          return (
            <div
              key={field.key}
              className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-2 py-2 border-b last:border-b-0"
            >
              <div className="flex-1 min-w-0">
                <div className="font-medium text-sm">{field.label}</div>
                <div className="text-[10px] text-muted-foreground mt-1 font-mono break-all">
                  {field.key}
                  {!isSet && <span className="ml-2 italic">(not set)</span>}
                  {field.secret && isSet && (
                    <span className="ml-2 italic">
                      {isRedacted
                        ? "(value set — enter a new value to replace)"
                        : "(value set)"}
                    </span>
                  )}
                </div>
              </div>
              <div className="flex items-center gap-2 flex-shrink-0">
                <Input
                  type={field.secret ? "password" : "text"}
                  autoComplete={field.secret ? "new-password" : undefined}
                  value={inputValue}
                  placeholder={field.placeholder}
                  disabled={pendingKey === field.key}
                  onChange={(e) =>
                    setDrafts((d) => ({ ...d, [field.key]: e.target.value }))
                  }
                  className="w-56"
                  aria-label={`AoE ${field.label}`}
                />
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => save(field)}
                  disabled={pendingKey === field.key || draft === undefined}
                >
                  Save
                </Button>
              </div>
            </div>
          )
        })}
      </CardContent>
    </Card>
  )
}

// AoeHealthCard: shows the live status of the configured Agents-of-
// Empires instance. AoE rotates its bearer token on a schedule (it
// writes a fresh value to ~/.config/agent-of-empires/serve.token);
// admins using config_aoe_bearer_token_file get free rotation, but
// inline tokens go stale silently. This card lets the admin check
// without sending a real test message.
function AoeHealthCard() {
  type Health = Awaited<ReturnType<typeof apiClient.aoeHealth>>
  const [health, setHealth] = useState<Health | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const probe = async () => {
    setBusy(true)
    setError(null)
    try {
      const r = await apiClient.aoeHealth()
      setHealth(r)
    } catch (e: any) {
      setError(e?.message ?? String(e))
    } finally {
      setBusy(false)
    }
  }

  useEffect(() => {
    probe()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const statusColor =
    health?.status === 'ok'
      ? 'text-emerald-500'
      : health?.status === 'disabled'
      ? 'text-muted-foreground'
      : 'text-destructive'

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Agents-of-Empires status</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <p className="text-xs text-muted-foreground">
          Probes the configured AoE instance with the current bearer token
          (resolved live, including file-sourced rotations). Use this to
          confirm the token in <span className="font-mono">
          config_aoe_bearer_token</span> / <span className="font-mono">
          config_aoe_bearer_token_file</span> still works.
        </p>
        <div className="flex items-center justify-between gap-4">
          <div className="text-sm">
            {busy && <span className="text-muted-foreground">Probing…</span>}
            {!busy && error && (
              <span className="text-destructive">probe error: {error}</span>
            )}
            {!busy && !error && health && (
              <>
                <span className={`font-medium ${statusColor}`}>
                  {health.status}
                </span>
                {health.status === 'ok' && health.session_count !== undefined && (
                  <span className="ml-2 text-muted-foreground">
                    {health.session_count} sessions @ {health.base_url}
                  </span>
                )}
                {health.message && (
                  <span className="ml-2 text-muted-foreground">
                    — {health.message}
                  </span>
                )}
              </>
            )}
          </div>
          <Button variant="outline" size="sm" onClick={probe} disabled={busy}>
            <RefreshCw className={`h-4 w-4 mr-1 ${busy ? 'animate-spin' : ''}`} />
            Re-check
          </Button>
        </div>
      </CardContent>
    </Card>
  )
}
