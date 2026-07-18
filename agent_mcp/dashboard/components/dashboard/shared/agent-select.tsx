"use client"

import * as React from "react"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { useDataStore } from "@/lib/stores/data-store"
import { cn } from "@/lib/utils"

/**
 * Shared `<AgentSelect>` — a single agent-picker dropdown for every
 * site in the dashboard that needs to choose an agent.
 *
 * Why this exists
 * ---------------
 *
 * Before this component, four sites reimplemented the same dropdown
 * by hand:
 *
 *   - `CreateTaskModal` used a plain `<Input placeholder="agent-01">`.
 *     Typo-friendly, no validation that the agent exists, no
 *     visibility of available agents (Dennis's primary bug).
 *   - `EditTaskDialog` used a shadcn `<Select>` but sourced its agent
 *     list via `apiClient.getAgents()` — which returns every row
 *     including `status='terminated'`. Terminated agents leaked into
 *     the dropdown.
 *   - The messages-dashboard From/To filters and Compose recipient
 *     each had their own dropdown wiring.
 *
 * One source-of-truth dropdown fixes the leak and makes the dropdown
 * usable in a single place.
 *
 * Live agents only
 * ----------------
 *
 * The agent list is read from the existing `useDataStore` Zustand
 * store via `getActiveAgents()` — which already filters terminated
 * rows via the `shouldDisplayAgent` predicate (PR #134). Don't
 * re-implement the filter here; compose the helper. Assigning a task
 * to a terminated agent is meaningless, so the live-only rule is the
 * locked design decision for *every* call site of this component.
 *
 * The `noneLabel` convention
 * --------------------------
 *
 * Callers pass the sentinel label text — the component itself stays
 * neutral. The label is context-specific:
 *
 *   - Task forms (CreateTaskModal, EditTaskDialog) pass
 *     `noneLabel="— Unassigned —"` because the underlying field is a
 *     nullable assignment.
 *   - Filter dropdowns (messages-dashboard From/To filters) pass
 *     `noneLabel="— Any —"` because no-selection means no filter.
 *
 * When `noneLabel` is undefined, no sentinel item is rendered — the
 * dropdown shows only the live agents (plus Admin if `pinAdmin` is
 * left at its default of `true`).
 *
 * Admin pinning
 * -------------
 *
 * Admin is special-cased everywhere in the codebase and is not part
 * of the live-agents store. This component renders it inline via the
 * `pinAdmin` prop (default `true`). The rare caller that shouldn't
 * surface Admin (none today) can pass `pinAdmin={false}`.
 */

// Sentinel value used by the underlying Radix `<Select>` to represent
// `null` — Radix Select cannot use an empty string as an item value
// (it conflates with the "no selection" state). The sentinel is an
// internal implementation detail; callers see `value: string | null`.
const NONE_SENTINEL = "__none__"

export type AgentSelectProps = {
  /** Selected agent_id, or null when the noneLabel sentinel is selected. */
  value: string | null
  /** Called with the new agent_id, or null when the noneLabel sentinel is picked. */
  onChange: (agentId: string | null) => void
  /**
   * When set, renders a first-item sentinel that maps to `null`.
   * Caller-provided label — pass "— Unassigned —" for task forms,
   * "— Any —" for filter dropdowns. The component stays neutral
   * about the wording.
   */
  noneLabel?: string
  /**
   * Pin Admin at the top of the dropdown (above live workers).
   * Defaults to `true` — Admin participates in tasks/messages on
   * every dashboard surface today, so the rare `false` callers are
   * the exception. Set to `false` to omit Admin entirely.
   */
  pinAdmin?: boolean
  disabled?: boolean
  required?: boolean
  /** Trigger placeholder shown when `value` is null and no noneLabel item is selected. */
  placeholder?: string
  /** Optional className passed through to the `<SelectTrigger>`. */
  className?: string
  /** Optional id passed through to the trigger (for `<label htmlFor>` wiring). */
  id?: string
  /**
   * Optional accessible name for the trigger. Needed on filter
   * dropdowns where the visible label vanishes once a value is chosen
   * (placeholder-only), so screen-reader users still hear what the
   * control filters.
   */
  ariaLabel?: string
}

/**
 * Filter the store's live-agent rows to exclude Admin — the Admin
 * row is rendered inline via the `pinAdmin` prop. The store can hold
 * the Admin row under the capitalised `Admin` id (see PR #G1's
 * pseudo-agent shape); we exclude both casings defensively.
 */
function filterOutAdmin(agents: Array<{ agent_id: string }>): Array<{ agent_id: string }> {
  return agents.filter(
    (a) => a.agent_id !== "Admin" && a.agent_id !== "admin",
  )
}

export function AgentSelect({
  value,
  onChange,
  noneLabel,
  pinAdmin = true,
  disabled,
  required,
  placeholder,
  className,
  id,
  ariaLabel,
}: AgentSelectProps): React.ReactElement {
  // Read live agents from the store. `getActiveAgents()` is the
  // single source of truth — it filters `status='terminated'` via
  // `shouldDisplayAgent`. Subscribing to `data` (rather than calling
  // `getActiveAgents()` outside of a selector) keeps the dropdown
  // in sync when agents are created/terminated mid-session.
  const data = useDataStore((s) => s.data)
  const liveAgents = React.useMemo(() => {
    if (!data) return []
    const all = data.agents.filter((agent) => agent.status !== "terminated")
    return filterOutAdmin(all)
  }, [data])

  // Map between the public `value: string | null` and the Radix
  // Select's required string value. `null` becomes NONE_SENTINEL
  // when a noneLabel item exists; otherwise we leave the trigger
  // empty and let the placeholder render.
  const selectValue = value === null
    ? (noneLabel ? NONE_SENTINEL : undefined)
    : value

  const handleValueChange = React.useCallback(
    (v: string) => {
      if (v === NONE_SENTINEL) {
        onChange(null)
        return
      }
      onChange(v)
    },
    [onChange],
  )

  return (
    <Select
      value={selectValue}
      onValueChange={handleValueChange}
      disabled={disabled}
      required={required}
    >
      <SelectTrigger
        id={id}
        aria-label={ariaLabel}
        className={cn("w-full bg-background border-border text-foreground", className)}
      >
        <SelectValue placeholder={placeholder ?? "Select agent"} />
      </SelectTrigger>
      <SelectContent className="bg-background border-border">
        {noneLabel !== undefined && (
          <SelectItem value={NONE_SENTINEL}>{noneLabel}</SelectItem>
        )}
        {pinAdmin && (
          <SelectItem value="Admin">Admin</SelectItem>
        )}
        {liveAgents.map((a) => (
          <SelectItem key={a.agent_id} value={a.agent_id}>
            {a.agent_id}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}
