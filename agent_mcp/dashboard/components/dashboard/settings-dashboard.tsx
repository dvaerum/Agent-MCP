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
import { apiClient } from "@/lib/api"

// Per-project agent_messages retention. Stored as an integer count of
// days in project_context["config_message_retention_days"]; absent or 0
// means "keep forever" (upstream behavior). The background pruner in
// features.message_retention enforces it.
const MESSAGE_RETENTION_KEY = "config_message_retention_days"

// Per-project worker-permission policies. Each lives in the
// project_context store under a `config_*` key. The dashboard runs as
// admin per ADR-0003 — we fetch the admin token via apiClient and
// reuse it for the PUT /memories/<key> and POST /memories calls
// (see lib/api.ts: createMemory / updateMemory).
interface PolicySpec {
  key: string
  title: string
  description: string
  // What the system does when the key is absent from project_context.
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
      "When on, send_agent_message also POSTs a tmux-pane wake-up to a local Agents-of-Empires (AoE) instance so the recipient notices the message even between polls. Disabled by default. Configure config_aoe_base_url, config_aoe_bearer_token (secret), and config_aoe_notify_template via the Memories tab. The message body itself is never forwarded — only {sender}, {recipient}, {message_id} are interpolated.",
    default: false,
  },
]

interface PolicyState {
  value: boolean
  exists: boolean   // true once we've seen the key in project_context
  pending: boolean  // true while a PUT/POST is in flight
}

// The admin token is fetched once and reused; same pattern as
// messages-dashboard.tsx.
async function adminToken(): Promise<string> {
  const tokens = await apiClient.getTokens()
  return tokens.admin_token
}

// project_context stores values as JSON-serialised strings. Booleans
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

// project_context stores retention as a JSON-encoded integer. Tolerate
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
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const refresh = async () => {
    setLoading(true)
    setError(null)
    try {
      const all = await apiClient.getAllData()
      const contexts = all.context ?? []
      setState((prev) => {
        const next = { ...prev }
        for (const p of POLICIES) {
          const row = contexts.find((c: any) => c.context_key === p.key)
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
      const retRow = contexts.find(
        (c: any) => c.context_key === MESSAGE_RETENTION_KEY,
      )
      if (retRow) {
        const days = coerceNonNegInt(retRow.value)
        setRetention({ saved: days, draft: String(days), exists: true, pending: false })
      } else {
        setRetention({ saved: 0, draft: "0", exists: false, pending: false })
      }
    } catch (e: any) {
      setError(e.message ?? String(e))
    } finally {
      setLoading(false)
    }
  }

  const saveRetention = async () => {
    const next = coerceNonNegInt(retention.draft)
    if (next === retention.saved && retention.exists) {
      // No change — nothing to do.
      return
    }
    setRetention((s) => ({ ...s, pending: true }))
    setError(null)
    try {
      const token = await adminToken()
      if (retention.exists) {
        await apiClient.updateMemory(MESSAGE_RETENTION_KEY, {
          context_value: next,
          token,
        })
      } else {
        await apiClient.createMemory({
          context_key: MESSAGE_RETENTION_KEY,
          context_value: next,
          description:
            "Days of read agent_messages to keep before background pruner deletes them. 0 = disabled.",
          token,
        })
      }
      setRetention({ saved: next, draft: String(next), exists: true, pending: false })
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
      const token = await adminToken()
      if (prevState.exists) {
        await apiClient.updateMemory(policy.key, {
          context_value: nextValue,
          token,
        })
      } else {
        await apiClient.createMemory({
          context_key: policy.key,
          context_value: nextValue,
          description: policy.title,
          token,
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
            <div className="flex-shrink-0 sm:pt-1 flex items-center gap-2 self-end sm:self-auto">
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
                  (retention.exists &&
                    coerceNonNegInt(retention.draft) === retention.saved)
                }
              >
                Save
              </Button>
            </div>
          </div>
        </CardContent>
      </Card>

      <AoeHealthCard />
    </div>
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
