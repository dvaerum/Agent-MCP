/**
 * `decodeMemoryValue` / `isSafeHref` / `memoryValuePreview`
 * (lib/memory-value.ts) — Wave 13.
 *
 * The Memories dashboard stores each value as a JSON-encoded string.
 * This helper decodes + classifies it (json / markdown / url / text),
 * unwrapping the double-encoded-JSON case and surviving non-JSON legacy
 * rows. Coverage below mirrors the real stored formats from the live
 * washing-brothers project.
 */

import { describe, expect, it } from "vitest"
import {
  decodeMemoryValue,
  isSafeHref,
  memoryValuePreview,
} from "@/lib/memory-value"

// `enc` mimics how a value lands in the DB: a JSON-encoded string.
const enc = (v: unknown) => JSON.stringify(v)

describe("decodeMemoryValue — format classification", () => {
  it("JSON object → json, payload is the parsed object", () => {
    const d = decodeMemoryValue(enc({ branch: "nix-and-ai", ok: true }))
    expect(d.format).toBe("json")
    expect(d.payload).toEqual({ branch: "nix-and-ai", ok: true })
  })

  it("JSON array → json, payload is the parsed array", () => {
    const d = decodeMemoryValue(enc([1, 2, 3]))
    expect(d.format).toBe("json")
    expect(d.payload).toEqual([1, 2, 3])
  })

  it("double-encoded JSON → json, payload is the INNER object", () => {
    // A JSON string whose content is itself JSON.
    const inner = { branch: "nix-and-ai" }
    const doubleEncoded = enc(JSON.stringify(inner)) // == "\"{\\\"branch\\\":\\\"nix-and-ai\\\"}\""
    const d = decodeMemoryValue(doubleEncoded)
    expect(d.format).toBe("json")
    expect(d.payload).toEqual(inner)
  })

  it("plain URL string → url, payload is the trimmed url string", () => {
    const d = decodeMemoryValue(enc("https://example.com/path?q=1"))
    expect(d.format).toBe("url")
    expect(d.payload).toBe("https://example.com/path?q=1")
  })

  it("http URL is also detected", () => {
    expect(decodeMemoryValue(enc("http://example.com")).format).toBe("url")
  })

  it("a string with a URL embedded in prose is NOT a bare url", () => {
    const d = decodeMemoryValue(enc("see https://example.com for details"))
    expect(d.format).not.toBe("url")
  })

  it("markdown heading → markdown", () => {
    expect(decodeMemoryValue(enc("# Title\n\nbody")).format).toBe("markdown")
  })

  it("markdown list → markdown", () => {
    expect(decodeMemoryValue(enc("- one\n- two")).format).toBe("markdown")
  })

  it("markdown bold → markdown", () => {
    expect(decodeMemoryValue(enc("this is **important** stuff")).format).toBe("markdown")
  })

  it("markdown link → markdown", () => {
    expect(decodeMemoryValue(enc("see [docs](https://x.com)")).format).toBe("markdown")
  })

  it("fenced code block → markdown", () => {
    expect(decodeMemoryValue(enc("```\ncode\n```")).format).toBe("markdown")
  })

  it("plain string → text, newlines preserved in payload", () => {
    const d = decodeMemoryValue(enc("line one\nline two"))
    expect(d.format).toBe("text")
    expect(d.payload).toBe("line one\nline two")
  })

  it("number → text (stringified)", () => {
    const d = decodeMemoryValue(enc(42))
    expect(d.format).toBe("text")
    expect(d.payload).toBe("42")
  })

  it("boolean → text (stringified)", () => {
    const d = decodeMemoryValue(enc(true))
    expect(d.format).toBe("text")
    expect(d.payload).toBe("true")
  })

  it("null → text (stringified)", () => {
    const d = decodeMemoryValue(enc(null))
    expect(d.format).toBe("text")
    expect(d.payload).toBe("null")
  })

  it("non-JSON legacy row (JSON.parse throws) → classified as its raw string", () => {
    // `# Legacy` is not valid JSON — the throw path treats it as the
    // logical string and classifies markdown.
    const d = decodeMemoryValue("# Legacy heading")
    expect(d.format).toBe("markdown")
    expect(d.payload).toBe("# Legacy heading")
    expect(d.raw).toBe("# Legacy heading")
  })

  it("non-JSON legacy plain-text row → text", () => {
    const d = decodeMemoryValue("just some legacy text")
    expect(d.format).toBe("text")
    expect(d.payload).toBe("just some legacy text")
  })

  it("raw is always the exact input string, verbatim", () => {
    const raw = enc({ a: 1 })
    expect(decodeMemoryValue(raw).raw).toBe(raw)
  })

  it("accepts an already-parsed object (defensive) → json", () => {
    const d = decodeMemoryValue({ already: "parsed" })
    expect(d.format).toBe("json")
    expect(d.payload).toEqual({ already: "parsed" })
  })
})

describe("isSafeHref — link allowlist", () => {
  it.each([
    "http://example.com",
    "https://example.com/path",
    "HTTPS://EXAMPLE.COM",
    "mailto:foo@bar.com",
  ])("allows %s", (href) => {
    expect(isSafeHref(href)).toBe(true)
  })

  it.each([
    "javascript:alert(1)",
    "JavaScript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "vbscript:msgbox(1)",
    "//evil.com",
    "/relative/path",
    "#anchor",
    "",
    null,
    undefined,
  ])("rejects %s", (href) => {
    expect(isSafeHref(href as string)).toBe(false)
  })
})

describe("memoryValuePreview — compact list badges", () => {
  it("json object → key count", () => {
    const p = memoryValuePreview(decodeMemoryValue(enc({ a: 1, b: 2, c: 3 })))
    expect(p.label).toBe("JSON · 3 keys")
  })

  it("json object with one key → singular", () => {
    const p = memoryValuePreview(decodeMemoryValue(enc({ a: 1 })))
    expect(p.label).toBe("JSON · 1 key")
  })

  it("json array → item count", () => {
    const p = memoryValuePreview(decodeMemoryValue(enc([1, 2, 3, 4, 5])))
    expect(p.label).toBe("JSON · 5 items")
  })

  it("url → URL label + the url snippet", () => {
    const p = memoryValuePreview(decodeMemoryValue(enc("https://example.com")))
    expect(p.label).toBe("URL")
    expect(p.snippet).toBe("https://example.com")
  })

  it("markdown → Markdown label + first non-empty line", () => {
    const p = memoryValuePreview(decodeMemoryValue(enc("\n\n# Heading\nbody")))
    expect(p.label).toBe("Markdown")
    expect(p.snippet).toBe("# Heading")
  })

  it("text → Text label + first line", () => {
    const p = memoryValuePreview(decodeMemoryValue(enc("first line\nsecond")))
    expect(p.label).toBe("Text")
    expect(p.snippet).toBe("first line")
  })
})
