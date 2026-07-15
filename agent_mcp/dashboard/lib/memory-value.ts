/**
 * Memory value decoding + classification (Wave 13).
 *
 * The Memories dashboard stores each entry's value as a JSON-encoded
 * string in `project_context.value`. Rendering it with a naive
 * `JSON.stringify(value)` collapses JSON to one escaped line, shows
 * markdown as literal `\n`/`**`, and surfaces double-encoded JSON as
 * `"{\"branch\": \"nix-and-ai\"}"`. This helper decodes the raw stored
 * string once (twice for the double-encoded case) and classifies the
 * *logical* content so the UI can render it human-readably.
 *
 * SECURITY: memory content is AGENT-AUTHORED and therefore untrusted.
 * This module never renders anything — it only classifies + exposes an
 * `isSafeHref` allowlist that the rendering layer uses to neutralise
 * `javascript:`/`data:` links. See `memory-value-view.tsx`.
 */

export type MemoryFormat = "json" | "markdown" | "url" | "text"

export interface DecodedMemoryValue {
  /** Detected logical format of the value. */
  format: MemoryFormat
  /**
   * The decoded payload. For `json` this is the parsed object/array; for
   * `markdown`/`url`/`text` it is the logical string.
   */
  payload: unknown
  /** The exact stored string, verbatim — the Raw-view escape hatch. */
  raw: string
}

// Whole trimmed value is a single http(s) URL.
const URL_RE = /^https?:\/\/\S+$/

// Any one of these markers is enough to treat a string as markdown.
const MARKDOWN_RES: RegExp[] = [
  /^#{1,6}\s/m, // ATX heading
  /^\s*[-*]\s+\S/m, // unordered list item
  /^\s*\d+\.\s+\S/m, // ordered list item
  /```/, // fenced code block
  /\*\*[^*\n]+\*\*/, // **bold**
  /\[[^\]\n]+\]\([^)\n]+\)/, // [text](url) link
  /^\s*\|.+\|\s*$/m, // table row
  /^>\s+\S/m, // blockquote
]

/**
 * Href allowlist. Only `http:`, `https:` and `mailto:` are permitted;
 * everything else (`javascript:`, `data:`, `vbscript:`, protocol-relative
 * `//host`, scheme-less relatives) is rejected. Used by the rendering
 * layer to drop unsafe links to inert text.
 */
export function isSafeHref(href: string | null | undefined): boolean {
  if (!href) return false
  const scheme = href.trim().match(/^([a-zA-Z][a-zA-Z0-9+.-]*):/)?.[1]?.toLowerCase()
  if (!scheme) return false // no explicit scheme → reject (blocks //evil.com, relatives)
  return scheme === "http" || scheme === "https" || scheme === "mailto"
}

function looksLikeUrl(s: string): boolean {
  return URL_RE.test(s.trim())
}

function looksLikeMarkdown(s: string): boolean {
  return MARKDOWN_RES.some((re) => re.test(s))
}

/** Classify an already-unwrapped logical string into url/markdown/text. */
function classifyString(s: string): DecodedMemoryValue["format"] {
  if (looksLikeUrl(s)) return "url"
  if (looksLikeMarkdown(s)) return "markdown"
  return "text"
}

/**
 * Decode + classify a stored memory value.
 *
 * Accepts the raw stored string. For resilience it also accepts an
 * already-parsed value (the API layer types `value` as `any`): a
 * non-string input is re-stringified so the same decode path applies.
 */
export function decodeMemoryValue(input: unknown): DecodedMemoryValue {
  const raw = typeof input === "string" ? input : safeStringify(input)

  let parsed: unknown
  try {
    parsed = JSON.parse(raw)
  } catch {
    // Non-JSON legacy row: the raw text is itself the logical string.
    return { format: classifyString(raw), payload: raw, raw }
  }

  if (parsed !== null && typeof parsed === "object") {
    // object or array → JSON.
    return { format: "json", payload: parsed, raw }
  }

  if (typeof parsed === "string") {
    // Try one more parse to unwrap double-encoded JSON.
    try {
      const inner = JSON.parse(parsed)
      if (inner !== null && typeof inner === "object") {
        return { format: "json", payload: inner, raw }
      }
    } catch {
      // Not double-encoded — fall through to classify the string.
    }
    return { format: classifyString(parsed), payload: parsed, raw }
  }

  // number / boolean / null → text (stringified).
  return { format: "text", payload: String(parsed), raw }
}

function safeStringify(value: unknown): string {
  try {
    return JSON.stringify(value) ?? String(value)
  } catch {
    return String(value)
  }
}

export interface MemoryValuePreview {
  /** Short type label, e.g. "JSON · 3 keys", "Markdown", "URL", "Text". */
  label: string
  /** One-line, human-readable snippet of the content. */
  snippet: string
}

/**
 * Compact preview for the list/table: a type badge label + a single-line
 * snippet. The caller ellipsizes the snippet with CSS.
 */
export function memoryValuePreview(decoded: DecodedMemoryValue): MemoryValuePreview {
  switch (decoded.format) {
    case "json": {
      const payload = decoded.payload
      if (Array.isArray(payload)) {
        const n = payload.length
        return {
          label: `JSON · ${n} ${n === 1 ? "item" : "items"}`,
          snippet: oneLine(JSON.stringify(payload)),
        }
      }
      const keys = payload && typeof payload === "object" ? Object.keys(payload) : []
      return {
        label: `JSON · ${keys.length} ${keys.length === 1 ? "key" : "keys"}`,
        snippet: keys.length ? oneLine(JSON.stringify(payload)) : "{}",
      }
    }
    case "url":
      return { label: "URL", snippet: oneLine(String(decoded.payload)) }
    case "markdown":
      return { label: "Markdown", snippet: firstMeaningfulLine(String(decoded.payload)) }
    case "text":
    default:
      return { label: "Text", snippet: firstMeaningfulLine(String(decoded.payload)) }
  }
}

function oneLine(s: string): string {
  return s.replace(/\s+/g, " ").trim()
}

function firstMeaningfulLine(s: string): string {
  const line = s.split(/\r?\n/).find((l) => l.trim().length > 0) ?? ""
  return line.trim()
}
