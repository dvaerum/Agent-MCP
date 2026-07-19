"use client"

import React, { useEffect, useState } from "react"
import { RefreshCw } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Switch } from "@/components/ui/switch"
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import {
  apiClient,
  type ProjectSetting,
  type SettingsSchemaEntry,
} from "@/lib/api"
import { toastError, toastSuccess } from "@/components/ui/toast"
import { useServerStore } from "@/lib/stores/server-store"

// ADR-0018: the Settings dashboard renders itself from the backend
// schema registry (GET /api/settings-schema). The registry — not this
// component — owns every setting's default / grouping / tier / copy;
// here we map each schema entry to a control via a type→widget
// registry (`widgetKindFor`) and lay the groups out in a fixed order
// (`GROUP_ORDER`). Reads come from GET /api/settings-data; writes go
// through PUT /settings/<key> / POST /settings (lib/api.ts:
// updateSetting / createSetting), authenticated via the operator
// session cookie set on /agent-mcp/login.

const REDACTED = "[redacted]"

// ── coercion helpers ────────────────────────────────────────────────
//
// project_settings stores values as JSON-serialised strings. Be
// liberal in what we accept when reading them back for display.

// Booleans arrive as either the bare boolean, the string "true"/"false",
// or a JSON-encoded form.
function coerceBool(raw: unknown, fallback: boolean): boolean {
  if (typeof raw === "boolean") return raw
  if (typeof raw === "string") {
    const s = raw.trim().toLowerCase()
    if (s === "true") return true
    if (s === "false") return false
    try {
      const parsed = JSON.parse(s)
      if (typeof parsed === "boolean") return parsed
    } catch {
      /* fall through */
    }
  }
  return fallback
}

// Integers arrive JSON-encoded. Tolerate quoted strings and floats;
// clamp to >= 0.
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

// Retention bounds (UX-09). The backend (features/message_retention.py)
// stores a plain non-negative integer day count: 0 = disabled (keep
// forever). The only real constraints are "whole number" and ">= 0".
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

// ── duration widget (int_duration) ──────────────────────────────────
//
// A duration setting is STORED as a plain integer number of SECONDS
// (e.g. config_event_idle_stop_seconds), but is edited as an amount +
// a minutes/hours/days unit. 0 = the setting's "infinite" sentinel
// ("never stop"). These pure helpers convert between the wire seconds
// and the {amount, unit} the control renders, and are exported for the
// vitest unit tests.
export const DURATION_UNITS = [
  { key: "minutes", label: "minutes", seconds: 60 },
  { key: "hours", label: "hours", seconds: 3600 },
  { key: "days", label: "days", seconds: 86400 },
] as const
export type DurationUnit = (typeof DURATION_UNITS)[number]["key"]

// Pick the largest unit that divides the total evenly, so 604800s shows
// as "7 days" (not "168 hours"). Falls back to minutes (rounded) for a
// value that isn't a clean multiple of any unit. 0/negative → 0 days.
export function secondsToParts(total: number): {
  amount: number
  unit: DurationUnit
} {
  if (!Number.isFinite(total) || total <= 0) return { amount: 0, unit: "days" }
  for (const u of [...DURATION_UNITS].reverse()) {
    if (total % u.seconds === 0) return { amount: total / u.seconds, unit: u.key }
  }
  return { amount: Math.max(1, Math.round(total / 60)), unit: "minutes" }
}

export function partsToSeconds(amount: number, unit: DurationUnit): number {
  const u = DURATION_UNITS.find((x) => x.key === unit) ?? DURATION_UNITS[2]
  if (!Number.isFinite(amount) || amount < 0) return 0
  return Math.floor(amount) * u.seconds
}

export function formatDuration(total: number): string {
  if (!Number.isFinite(total) || total <= 0) return "never stop"
  const { amount, unit } = secondsToParts(total)
  return `${amount} ${unit}`
}

// ── type→widget registry ────────────────────────────────────────────
//
// A schema entry's `widget` hint (falling back to `type`) selects the
// control that renders it. The five control kinds:
//   switch   → <Switch>, optimistic instant toggle (no Save button)
//   int_days → number input + Save, with retention validation (≥ 0)
//   int_ms   → number input + Save, plain non-negative int
//   text     → text input + Save (url / template strings)
//   secret   → write-only password input + Save (never prefilled)
export type WidgetKind =
  | "switch"
  | "int_days"
  | "int_ms"
  | "int_duration"
  | "text"
  | "secret"

export function widgetKindFor(entry: SettingsSchemaEntry): WidgetKind {
  switch (entry.widget) {
    case "switch":
      return "switch"
    case "int_days":
      return "int_days"
    case "int_ms":
      return "int_ms"
    case "int_duration":
      return "int_duration"
    case "url":
    case "template":
      return "text"
    case "secret":
    case "secret_path":
      return "secret"
  }
  // Fallback on the coarser `type` when the widget hint is missing or
  // unrecognised, so a new setting still renders a sensible control.
  switch (entry.type) {
    case "bool":
      return "switch"
    case "int":
      return "int_ms"
    case "secret":
      return "secret"
    case "string":
    default:
      return "text"
  }
}

// ── group layout ────────────────────────────────────────────────────
//
// Groups render as one Card each, in this fixed order with these
// titles (data-driven grouping — the schema's `group` field decides
// which card an entry lands in; entry order within a group is the
// schema's order).
export const GROUP_ORDER: ReadonlyArray<{
  group: SettingsSchemaEntry["group"]
  title: string
}> = [
  { group: "worker_permissions", title: "Worker permissions" },
  { group: "event_loop", title: "Agent event-loop" },
  { group: "agent_profiles", title: "Agent profiles" },
  { group: "retention", title: "Message retention" },
  { group: "aoe", title: "AoE integration" },
]

export interface SettingsGroup {
  group: SettingsSchemaEntry["group"]
  title: string
  entries: SettingsSchemaEntry[]
}

// Partition the flat schema into the canonical group order, preserving
// schema order within each group and dropping groups with no entries.
export function groupSchema(schema: SettingsSchemaEntry[]): SettingsGroup[] {
  return GROUP_ORDER.map(({ group, title }) => ({
    group,
    title,
    entries: schema.filter((e) => e.group === group),
  })).filter((g) => g.entries.length > 0)
}

// Tier-gating: a sysadmin-tier setting is not writable by a plain
// operator, so the control renders disabled with an inline note
// instead of letting the save 403 (the backend's `_CONFIG_AOE_KEY_RE`
// gate is still the enforcer — this only drives the UI).
export function isTierLocked(
  entry: SettingsSchemaEntry,
  caller: { sysadmin: boolean },
): boolean {
  return entry.tier === "sysadmin" && !caller.sysadmin
}

// Human-readable rendering of a setting's default, for the
// "(using default: …)" hint on entries not yet present in the store.
function formatDefault(entry: SettingsSchemaEntry): string {
  const kind = widgetKindFor(entry)
  if (kind === "switch") return Boolean(entry.default) ? "allow" : "deny"
  if (kind === "int_days") {
    return Number(entry.default) === 0
      ? "keep forever"
      : `${Number(entry.default)} days`
  }
  if (kind === "int_ms") return `${Number(entry.default)} ms`
  if (kind === "int_duration") return formatDuration(Number(entry.default))
  const d = entry.default
  return d === null || d === undefined || d === "" ? "(none)" : String(d)
}

interface BoolRow {
  value: boolean
  exists: boolean
  pending: boolean
}

type Caller = { sysadmin: boolean; confirmed_operator: boolean }

export function SettingsDashboard() {
  // Project chip (matches Agents/Tasks/Messages header).
  const { servers, activeServerId } = useServerStore()
  const activeServer = servers.find((s) => s.id === activeServerId)

  const [schema, setSchema] = useState<SettingsSchemaEntry[]>([])
  const [caller, setCaller] = useState<Caller>({
    sysadmin: false,
    confirmed_operator: false,
  })
  // Raw project_settings rows from the last refresh — non-switch
  // widgets read their stored value / redaction state from here.
  const [rows, setRows] = useState<ProjectSetting[]>([])
  // Optimistic switch state, keyed by setting key.
  const [boolState, setBoolState] = useState<Record<string, BoolRow>>({})
  // Edit drafts for int/string/secret fields (undefined = show stored).
  const [drafts, setDrafts] = useState<Record<string, string>>({})
  // Show the int_days validation hint only after the field is blurred.
  const [touched, setTouched] = useState<Record<string, boolean>>({})
  // Which non-switch field currently has a save in flight.
  const [pendingKey, setPendingKey] = useState<string | null>(null)
  // Count of agents currently inside a wait_for_events long-poll —
  // surfaced under the global event-loop toggle. Read from
  // /api/all-data's per-agent `wait_for_events_in_flight` boolean.
  const [agentsInWait, setAgentsInWait] = useState<number>(0)
  const [loading, setLoading] = useState(false)
  const [lastFetch, setLastFetch] = useState<number | null>(null)

  const refresh = async () => {
    setLoading(true)
    try {
      const [schemaRes, settingsRes, all] = await Promise.all([
        apiClient.getSettingsSchema(),
        apiClient.getSettingsData(),
        apiClient.getAllData(),
      ])
      const nextSchema = schemaRes.schema ?? []
      const nextRows = settingsRes.settings ?? []
      setSchema(nextSchema)
      setCaller(schemaRes.caller ?? { sysadmin: false, confirmed_operator: false })
      setRows(nextRows)

      const agents = (all.agents ?? []) as Array<{
        wait_for_events_in_flight?: boolean
      }>
      setAgentsInWait(
        agents.filter((a) => a.wait_for_events_in_flight === true).length,
      )

      // Seed switch state from the schema's bool entries.
      const nextBool: Record<string, BoolRow> = {}
      for (const e of nextSchema) {
        if (widgetKindFor(e) !== "switch") continue
        const row = nextRows.find((r) => r.context_key === e.key)
        nextBool[e.key] = row
          ? {
              value: coerceBool(row.value, Boolean(e.default)),
              exists: true,
              pending: false,
            }
          : { value: Boolean(e.default), exists: false, pending: false }
      }
      setBoolState(nextBool)
      setDrafts({})
      setTouched({})
      setLastFetch(Date.now())
    } catch (e) {
      toastError(e, "Failed to load settings")
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    refresh()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Switch: optimistic flip, persist via update (row exists) / create
  // (first write), revert on failure. Instant — no Save button.
  const toggle = async (entry: SettingsSchemaEntry, next: boolean) => {
    const prev = boolState[entry.key] ?? {
      value: Boolean(entry.default),
      exists: false,
      pending: false,
    }
    setBoolState((s) => ({
      ...s,
      [entry.key]: { ...prev, value: next, pending: true },
    }))
    try {
      if (prev.exists) {
        await apiClient.updateSetting(entry.key, { context_value: next })
      } else {
        await apiClient.createSetting({
          context_key: entry.key,
          context_value: next,
          description: entry.title,
        })
      }
      setBoolState((s) => ({
        ...s,
        [entry.key]: { value: next, exists: true, pending: false },
      }))
      toastSuccess(`${entry.title} updated.`)
    } catch (e) {
      // Revert the optimistic flip and surface the failure.
      setBoolState((s) => ({ ...s, [entry.key]: { ...prev, pending: false } }))
      toastError(e, "Failed to save setting")
    }
  }

  // Save an int/string/secret field. int_days runs the retention
  // validation; int_ms coerces a plain non-negative int; text/secret
  // save the string verbatim.
  const saveField = async (entry: SettingsSchemaEntry) => {
    const kind = widgetKindFor(entry)
    const draft = drafts[entry.key]
    if (draft === undefined) return
    if (kind === "int_days" && validateRetention(draft) !== null) {
      setTouched((t) => ({ ...t, [entry.key]: true }))
      return
    }
    const existed = rows.some((r) => r.context_key === entry.key)
    const value: unknown =
      kind === "int_days" || kind === "int_ms" || kind === "int_duration"
        ? coerceNonNegInt(draft)
        : draft
    setPendingKey(entry.key)
    try {
      if (existed) {
        await apiClient.updateSetting(entry.key, {
          context_value: value,
          description: entry.description,
        })
      } else {
        await apiClient.createSetting({
          context_key: entry.key,
          context_value: value,
          description: entry.description,
        })
      }
      toastSuccess(`${entry.title} updated.`)
      await refresh()
    } catch (e) {
      toastError(e, "Failed to save setting")
    } finally {
      setPendingKey(null)
    }
  }

  const groups = groupSchema(schema)

  return (
    <div className="w-full p-4 sm:p-6 space-y-4 sm:space-y-6">
      {/* Header — parity standard (matches messages-dashboard): no icon
          in the title, a subtitle, the project chip + static status dot
          + last-updated time, then Refresh. */}
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
        <div>
          <h1 className="text-2xl sm:text-3xl font-semibold tracking-tight text-foreground">
            Settings
          </h1>
          <p className="text-muted-foreground text-sm sm:text-base mt-1">
            Configure worker permissions, retention, and integrations
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2 sm:gap-3">
          {activeServer && (
            <Badge
              variant="outline"
              className="text-xs bg-primary/15 text-primary border-primary/30 font-medium"
            >
              <span aria-hidden className="w-2 h-2 bg-primary rounded-full mr-2" />
              {activeServer.name}
            </Badge>
          )}
          {lastFetch && (
            <span className="text-xs text-muted-foreground">
              Last updated: {new Date(lastFetch).toLocaleTimeString()}
            </span>
          )}
          <Button
            variant="outline"
            size="sm"
            onClick={refresh}
            disabled={loading}
            className="text-xs"
          >
            <RefreshCw
              className={`h-3.5 w-3.5 mr-1.5 ${loading ? "animate-spin" : ""}`}
            />
            Refresh
          </Button>
        </div>
      </div>

      {loading && schema.length === 0 && (
        <div className="text-sm text-muted-foreground">Loading…</div>
      )}

      {groups.map((g) => (
        <Card key={g.group}>
          <CardHeader>
            <CardTitle className="text-base">{g.title}</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            {g.entries.map((entry) => (
              <SettingRow
                key={entry.key}
                entry={entry}
                locked={isTierLocked(entry, caller)}
                row={rows.find((r) => r.context_key === entry.key)}
                boolRow={boolState[entry.key]}
                draft={drafts[entry.key]}
                touched={!!touched[entry.key]}
                pending={pendingKey === entry.key}
                agentsInWait={agentsInWait}
                onToggle={(v) => toggle(entry, v)}
                onDraft={(v) =>
                  setDrafts((d) => ({ ...d, [entry.key]: v }))
                }
                onBlur={() =>
                  setTouched((t) => ({ ...t, [entry.key]: true }))
                }
                onSave={() => saveField(entry)}
              />
            ))}
          </CardContent>
        </Card>
      ))}

      {/* AoE health probe — rendered after the AoE group card. */}
      <AoeHealthCard />
    </div>
  )
}

// A single setting row: shared left column (title + description + mono
// key line + hint) plus the control selected by `widgetKindFor`.
function SettingRow({
  entry,
  locked,
  row,
  boolRow,
  draft,
  touched,
  pending,
  agentsInWait,
  onToggle,
  onDraft,
  onBlur,
  onSave,
}: {
  entry: SettingsSchemaEntry
  locked: boolean
  row?: ProjectSetting
  boolRow?: BoolRow
  draft?: string
  touched: boolean
  pending: boolean
  agentsInWait: number
  onToggle: (v: boolean) => void
  onDraft: (v: string) => void
  onBlur: () => void
  onSave: () => void
}) {
  const kind = widgetKindFor(entry)
  const exists =
    kind === "switch" ? boolRow?.exists ?? false : row !== undefined
  const isRedacted = row?.value === REDACTED

  return (
    <div
      /* Stacked at <sm:, row at sm+. The control drops below the
         description on mobile so it doesn't squash the copy column. */
      className="flex flex-col sm:flex-row sm:items-start sm:justify-between gap-3 py-3 border-b last:border-b-0"
    >
      <div className="flex-1 min-w-0">
        <div className="font-medium text-sm">{entry.title}</div>
        <div className="text-xs text-muted-foreground mt-1">
          {entry.description}
        </div>
        <div className="text-[10px] text-muted-foreground mt-1 font-mono break-all">
          {entry.key}
          {kind === "secret" ? (
            !exists ? (
              <span className="ml-2 italic">(not set)</span>
            ) : (
              <span className="ml-2 italic">
                {isRedacted
                  ? "(value set — enter a new value to replace)"
                  : "(value set)"}
              </span>
            )
          ) : (
            !exists && (
              <span className="ml-2 italic">
                (using default: {formatDefault(entry)})
              </span>
            )
          )}
        </div>
        {/* Live "X agents currently in wait" count under the global
            event-loop toggle (read-only, from wait_for_events_in_flight).
            Hidden when the toggle is OFF — no agent should be in wait. */}
        {entry.key === "config_auto_event_loop_global" &&
          (boolRow?.value ?? Boolean(entry.default)) && (
            <div className="text-xs text-muted-foreground mt-2">
              {agentsInWait} agent{agentsInWait === 1 ? "" : "s"} currently in
              wait.
            </div>
          )}
        {locked && (
          <div className="text-xs text-muted-foreground mt-1 italic">
            sysadmin-only — ask a host admin to change this.
          </div>
        )}
      </div>
      <div className="flex-shrink-0 sm:pt-1 self-end sm:self-auto">
        <SettingControl
          entry={entry}
          kind={kind}
          locked={locked}
          row={row}
          boolRow={boolRow}
          draft={draft}
          touched={touched}
          pending={pending}
          exists={exists}
          onToggle={onToggle}
          onDraft={onDraft}
          onBlur={onBlur}
          onSave={onSave}
        />
      </div>
    </div>
  )
}

// The control half of a setting row, dispatched on the widget kind.
// Exported for unit tests (widget-registry → DOM control mapping).
export function SettingControl({
  entry,
  kind,
  locked,
  row,
  boolRow,
  draft,
  touched,
  pending,
  exists,
  onToggle,
  onDraft,
  onBlur,
  onSave,
}: {
  entry: SettingsSchemaEntry
  kind: WidgetKind
  locked: boolean
  row?: ProjectSetting
  boolRow?: BoolRow
  draft?: string
  touched: boolean
  pending: boolean
  exists: boolean
  onToggle: (v: boolean) => void
  onDraft: (v: string) => void
  onBlur: () => void
  onSave: () => void
}) {
  if (kind === "switch") {
    return (
      <Switch
        checked={boolRow?.value ?? Boolean(entry.default)}
        disabled={locked || (boolRow?.pending ?? false)}
        onCheckedChange={onToggle}
        aria-label={entry.title}
      />
    )
  }

  if (kind === "int_days") {
    const savedInt = row ? coerceNonNegInt(row.value) : Number(entry.default) || 0
    const draftVal = draft !== undefined ? draft : String(savedInt)
    const invalid = validateRetention(draftVal) !== null
    return (
      <div className="flex flex-col gap-1 items-end">
        <div className="flex items-center gap-2">
          <Input
            type="number"
            min={0}
            step={1}
            inputMode="numeric"
            value={draftVal}
            disabled={locked || pending}
            onChange={(e) => onDraft(e.target.value)}
            onBlur={onBlur}
            aria-invalid={invalid}
            className="w-24"
            aria-label={entry.title}
          />
          <span className="text-xs text-muted-foreground">days</span>
          <Button
            variant="outline"
            size="sm"
            onClick={onSave}
            disabled={
              locked ||
              pending ||
              invalid ||
              (exists && coerceNonNegInt(draftVal) === savedInt)
            }
          >
            Save
          </Button>
        </div>
        {touched && invalid && (
          <div className="text-xs text-destructive" role="alert">
            {validateRetention(draftVal)}
          </div>
        )}
      </div>
    )
  }

  if (kind === "int_duration") {
    const savedInt = row ? coerceNonNegInt(row.value) : Number(entry.default) || 0
    return (
      <DurationControl
        savedSeconds={savedInt}
        draft={draft}
        disabled={locked}
        pending={pending}
        exists={exists}
        ariaLabel={entry.title}
        onDraft={onDraft}
        onSave={onSave}
      />
    )
  }

  // int_ms / text / secret — an input + Save button.
  const isSecret = kind === "secret"
  const stored = isSecret ? "" : coerceDisplayString(row?.value)
  const draftVal = draft !== undefined ? draft : stored
  const inputType = isSecret ? "password" : kind === "int_ms" ? "number" : "text"
  const placeholder = isSecret
    ? "enter a value (stored, never displayed)"
    : String(entry.default ?? "")
  const unchanged = !isSecret && draft !== undefined && draftVal === stored
  return (
    <div className="flex items-center gap-2">
      <Input
        type={inputType}
        autoComplete={isSecret ? "new-password" : undefined}
        min={kind === "int_ms" ? 0 : undefined}
        step={kind === "int_ms" ? 1 : undefined}
        inputMode={kind === "int_ms" ? "numeric" : undefined}
        value={draftVal}
        placeholder={placeholder}
        disabled={locked || pending}
        onChange={(e) => onDraft(e.target.value)}
        className="w-56"
        aria-label={entry.title}
      />
      <Button
        variant="outline"
        size="sm"
        onClick={onSave}
        disabled={locked || pending || draft === undefined || unchanged}
      >
        Save
      </Button>
    </div>
  )
}

// The int_duration control: an amount input + a minutes/hours/days unit
// select + Save. The setting is stored as SECONDS; this widget converts
// to/from a human unit and pushes the computed seconds up to the parent
// draft so the shared save path (coerceNonNegInt → createSetting/
// updateSetting) is unchanged. 0 = the "never stop" / infinite sentinel.
function DurationControl({
  savedSeconds,
  draft,
  disabled,
  pending,
  exists,
  ariaLabel,
  onDraft,
  onSave,
}: {
  savedSeconds: number
  draft?: string
  disabled: boolean
  pending: boolean
  exists: boolean
  ariaLabel: string
  onDraft: (v: string) => void
  onSave: () => void
}) {
  const initial = secondsToParts(
    draft !== undefined ? coerceNonNegInt(draft) : savedSeconds,
  )
  const [amount, setAmount] = useState<string>(String(initial.amount))
  const [unit, setUnit] = useState<DurationUnit>(initial.unit)

  // Push the computed seconds up to the parent draft whenever amount/unit
  // change, so onSave reads the seconds from drafts[key].
  const pushDraft = (nextAmount: string, nextUnit: DurationUnit) => {
    const n = Number(nextAmount)
    onDraft(String(partsToSeconds(Number.isFinite(n) ? n : 0, nextUnit)))
  }

  const currentSeconds = partsToSeconds(Number(amount) || 0, unit)
  const unchanged = exists && currentSeconds === savedSeconds
  const isInfinite = currentSeconds === 0

  return (
    <div className="flex flex-col gap-1 items-end">
      <div className="flex items-center gap-2">
        <Input
          type="number"
          min={0}
          step={1}
          inputMode="numeric"
          value={amount}
          disabled={disabled || pending}
          onChange={(e) => {
            setAmount(e.target.value)
            pushDraft(e.target.value, unit)
          }}
          className="w-20"
          aria-label={`${ariaLabel} amount`}
        />
        <select
          value={unit}
          disabled={disabled || pending}
          onChange={(e) => {
            const u = e.target.value as DurationUnit
            setUnit(u)
            pushDraft(amount, u)
          }}
          aria-label={`${ariaLabel} unit`}
          className="h-9 rounded-md border border-input bg-background px-2 text-sm"
        >
          {DURATION_UNITS.map((u) => (
            <option key={u.key} value={u.key}>
              {u.label}
            </option>
          ))}
        </select>
        <Button
          variant="outline"
          size="sm"
          onClick={onSave}
          disabled={disabled || pending || draft === undefined || unchanged}
        >
          Save
        </Button>
      </div>
      <div className="text-xs text-muted-foreground">
        {isInfinite
          ? "0 = never stop (hold indefinitely)"
          : `= ${currentSeconds} seconds`}
      </div>
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
