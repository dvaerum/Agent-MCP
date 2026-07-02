"use client"

import * as React from "react"
import { X } from "lucide-react"
import { cn } from "@/lib/utils"
import { useDataStore } from "@/lib/stores/data-store"
import {
  addCapabilityTags,
  collectCapabilitySuggestions,
  normalizeCapabilityTag,
} from "@/components/dashboard/shared/capability-tags"

// Re-export the pure helpers so component consumers can import the
// widget and its normalization/suggestion utilities from one path.
export {
  addCapabilityTags,
  collectCapabilitySuggestions,
  normalizeCapabilityTag,
} from "@/components/dashboard/shared/capability-tags"

/**
 * Shared `<CapabilityTagInput>` — a chips / tag input for the two
 * places the dashboard collects capability labels:
 *
 *   - Agent edit → `agents.capabilities`
 *   - Create task → `tasks.required_capabilities`
 *
 * Why a free-entry tag input (NOT an enum picker)
 * -----------------------------------------------
 *
 * Agent `capabilities` and task `required_capabilities` are FREE-TEXT
 * routing skill tags — the wake-loop router matches
 * `agent.capabilities ⊇ task.required_capabilities` over these tags.
 * They are NOT the Wave 9 `KNOWN_CAPABILITIES` permission enum (that
 * enum gates operator/group authorization and lives in a different
 * code path). Building an enum picker here would break routing by
 * forbidding any tag the operator hasn't pre-registered. So this is a
 * free-entry input: the operator types any tag, and we *suggest*
 * (never restrict to) tags already in use across live data.
 *
 * Normalization mirrors the server
 * --------------------------------
 *
 * The server's single source of truth is `normalize_capabilities`
 * (`agent_mcp/utils/capability_normalization.py`): each tag is
 * `str(raw).strip().lower()`, empty entries dropped, deduped on first
 * occurrence, first-occurrence order preserved. We mirror that shape
 * client-side so what the operator sees as chips is byte-for-byte what
 * gets stored — no surprise re-casing after save.
 */

// ---- Component ---------------------------------------------------------

export type CapabilityTagInputProps = {
  /** Current tags. Already normalized (lowercase, deduped). */
  value: string[]
  /** Called with the next normalized tag list. */
  onChange: (tags: string[]) => void
  /**
   * Autocomplete suggestions. When omitted, the component sources them
   * from the live data-store (union of in-use agent + task tags).
   */
  suggestions?: string[]
  placeholder?: string
  disabled?: boolean
  /** id for `<label htmlFor>` wiring on the text input. */
  id?: string
  className?: string
}

export function CapabilityTagInput({
  value,
  onChange,
  suggestions,
  placeholder = "Add a tag, press Enter",
  disabled,
  id,
  className,
}: CapabilityTagInputProps): React.ReactElement {
  const [inputValue, setInputValue] = React.useState("")
  const [focused, setFocused] = React.useState(false)
  const inputRef = React.useRef<HTMLInputElement>(null)
  // Stable id for the suggestion listbox so the combobox input can
  // reference it via aria-controls (ARIA combobox requirement).
  const reactId = React.useId()
  const listboxId = `${id ?? "cap-tags"}-${reactId}-listbox`

  // Source suggestions from the store when the caller doesn't provide
  // an explicit list — mirrors <AgentSelect>'s "read the live store"
  // pattern so both consumers get real in-use tags for free.
  const data = useDataStore((s) => s.data)
  const storeSuggestions = React.useMemo(
    () => collectCapabilitySuggestions(data?.agents, data?.tasks),
    [data],
  )
  const allSuggestions = suggestions ?? storeSuggestions

  // Suggestions not already selected, matched against the current
  // input as a case-insensitive substring.
  const filteredSuggestions = React.useMemo(() => {
    const selected = new Set(value)
    const q = normalizeCapabilityTag(inputValue)
    return allSuggestions
      .filter((s) => !selected.has(s))
      .filter((s) => (q ? s.includes(q) : true))
  }, [allSuggestions, value, inputValue])

  const showDropdown = focused && filteredSuggestions.length > 0

  const commit = (raw: string) => {
    const next = addCapabilityTags(value, raw)
    if (next.length !== value.length) onChange(next)
    setInputValue("")
  }

  const removeAt = (index: number) => {
    onChange(value.filter((_, i) => i !== index))
    // Keep focus on the input so keyboard flow continues.
    inputRef.current?.focus()
  }

  const handleKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter" || e.key === ",") {
      // Enter/comma commits the typed tag. Prevent default so Enter
      // doesn't submit the surrounding <form> and comma doesn't land
      // as a literal character.
      e.preventDefault()
      if (inputValue.trim()) commit(inputValue)
      return
    }
    if (e.key === "Backspace" && !inputValue && value.length > 0) {
      // Backspace on an empty input removes the last chip.
      e.preventDefault()
      removeAt(value.length - 1)
    }
  }

  return (
    <div className={cn("relative", className)}>
      <div
        className={cn(
          "flex flex-wrap items-center gap-1.5 rounded-md border border-border bg-background px-2 py-1.5 min-h-9 text-sm",
          "focus-within:ring-1 focus-within:ring-ring",
          disabled && "opacity-50 pointer-events-none",
        )}
        onClick={() => inputRef.current?.focus()}
      >
        {value.map((tag, i) => (
          <span
            key={tag}
            className="inline-flex items-center gap-1 rounded bg-muted px-2 py-0.5 text-xs font-mono text-foreground"
          >
            {tag}
            <button
              type="button"
              aria-label={`Remove ${tag}`}
              onClick={(e) => {
                e.stopPropagation()
                removeAt(i)
              }}
              disabled={disabled}
              className="text-muted-foreground hover:text-destructive focus:text-destructive focus:outline-none"
            >
              <X className="h-3 w-3" />
            </button>
          </span>
        ))}
        <input
          ref={inputRef}
          id={id}
          type="text"
          role="combobox"
          aria-expanded={showDropdown}
          aria-controls={listboxId}
          aria-autocomplete="list"
          value={inputValue}
          disabled={disabled}
          onChange={(e) => setInputValue(e.target.value)}
          onKeyDown={handleKeyDown}
          onFocus={() => setFocused(true)}
          // Delay blur so a suggestion mousedown-click can register
          // before the dropdown unmounts.
          onBlur={() => setTimeout(() => setFocused(false), 120)}
          placeholder={value.length === 0 ? placeholder : ""}
          className="flex-1 min-w-[8ch] bg-transparent outline-none placeholder:text-muted-foreground"
        />
      </div>
      {showDropdown && (
        <ul
          id={listboxId}
          role="listbox"
          className="absolute z-50 mt-1 max-h-48 w-full overflow-y-auto rounded-md border border-border bg-popover py-1 shadow-md"
        >
          {filteredSuggestions.map((s) => (
            <li key={s} role="option" aria-selected={false}>
              <button
                type="button"
                // onMouseDown (not onClick) so the input's delayed blur
                // doesn't close the dropdown before the pick registers.
                onMouseDown={(e) => {
                  e.preventDefault()
                  commit(s)
                  inputRef.current?.focus()
                }}
                className="block w-full px-3 py-1.5 text-left text-sm font-mono hover:bg-muted focus:bg-muted focus:outline-none"
              >
                {s}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
