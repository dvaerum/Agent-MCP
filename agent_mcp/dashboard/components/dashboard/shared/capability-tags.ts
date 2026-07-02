/**
 * Pure helpers behind <CapabilityTagInput>. Kept in a JSX-free module
 * so the dashboard's node-environment vitest suite (no jsdom / RTL —
 * see vitest.config.ts) can import and test them directly without
 * transforming a React component tree.
 *
 * Agent `capabilities` and task `required_capabilities` are FREE-TEXT
 * routing skill tags (the wake-loop router matches
 * `agent.capabilities ⊇ task.required_capabilities`), NOT the Wave 9
 * `KNOWN_CAPABILITIES` permission enum. So there is no allow-list here:
 * any tag is valid; we only *suggest* tags already in use.
 *
 * Normalization mirrors the server's single source of truth,
 * `normalize_capabilities` (agent_mcp/utils/capability_normalization.py):
 * each tag is `str(raw).strip().lower()`, empties dropped, deduped on
 * first occurrence, first-occurrence order preserved.
 */

/**
 * Normalize a single raw tag: coerce to string, strip, lowercase.
 * Returns "" for whitespace-only / nullish input so callers can drop it.
 */
export function normalizeCapabilityTag(raw: unknown): string {
  return String(raw ?? "").trim().toLowerCase()
}

/**
 * Add one or more raw tags (a single token, or a comma-separated string
 * the user pasted) to `existing`, mirroring the server's normalize +
 * dedupe-on-first-occurrence semantics. Existing tags win; order of
 * first occurrence is preserved; empty / duplicate entries are dropped.
 * Returns a new array (never mutates `existing`).
 */
export function addCapabilityTags(
  existing: readonly string[],
  raw: string,
): string[] {
  const out = [...existing]
  const seen = new Set(existing)
  for (const piece of raw.split(",")) {
    const tag = normalizeCapabilityTag(piece)
    if (!tag || seen.has(tag)) continue
    seen.add(tag)
    out.push(tag)
  }
  return out
}

/**
 * Coerce a capability column value into a string list. Handles the
 * three shapes the backend emits: a real array, a JSON-encoded array
 * string, or a comma-separated string.
 */
function coerceTagList(caps: unknown): string[] {
  if (Array.isArray(caps)) return caps.map((c) => String(c))
  if (typeof caps === "string") {
    const s = caps.trim()
    if (!s) return []
    try {
      const parsed = JSON.parse(s)
      if (Array.isArray(parsed)) return parsed.map((c) => String(c))
    } catch {
      // Not JSON — fall through to comma-split.
    }
    return s.split(",")
  }
  return []
}

/**
 * Gather the distinct, sorted union of capability tags already in use
 * across live data — agents' `capabilities` and tasks'
 * `required_capabilities`. This is the autocomplete suggestion source:
 * real, in-use tags, no invented enum.
 */
export function collectCapabilitySuggestions(
  agents?: ReadonlyArray<{ capabilities?: unknown }> | null,
  tasks?: ReadonlyArray<{ required_capabilities?: unknown }> | null,
): string[] {
  const set = new Set<string>()
  const absorb = (caps: unknown) => {
    for (const tag of coerceTagList(caps)) {
      const norm = normalizeCapabilityTag(tag)
      if (norm) set.add(norm)
    }
  }
  for (const a of agents ?? []) absorb(a?.capabilities)
  for (const t of tasks ?? []) absorb(t?.required_capabilities)
  return Array.from(set).sort()
}
