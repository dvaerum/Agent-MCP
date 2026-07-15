/**
 * XSS-inertness of the memory markdown renderer (lib/memory-value +
 * components/dashboard/memory-value-view) — Wave 13, REQUIRED.
 *
 * Memory content is agent-authored → untrusted → stored-XSS risk. The
 * renderer uses react-markdown WITHOUT rehype-raw, so raw HTML is inert
 * escaped text, and links are gated through the `isSafeHref` allowlist.
 * We render `SafeMarkdown` to a static HTML string (react-dom/server,
 * no browser) and assert nothing dangerous survives.
 *
 * Uses `React.createElement` rather than JSX so this stays a `.ts` file
 * that the pure-Node vitest transform handles without a JSX plugin.
 */

import React from "react"
import { renderToStaticMarkup } from "react-dom/server"
import { describe, expect, it } from "vitest"
import { SafeMarkdown } from "@/components/dashboard/memory-value-view"

function render(source: string): string {
  return renderToStaticMarkup(React.createElement(SafeMarkdown, { source }))
}

describe("SafeMarkdown — stored-XSS is inert", () => {
  it("a <script> tag renders as escaped text, never an executable element", () => {
    const html = render("Hello <script>alert(1)</script> world")
    expect(html).not.toContain("<script>")
    expect(html).not.toContain("</script>")
    // The literal text is preserved (escaped) so the user still sees it.
    expect(html).toContain("alert(1)")
  })

  it("an <img onerror> payload is inert (escaped text, not a live element)", () => {
    const html = render("<img src=x onerror=alert(1)>")
    // No live <img> element — the raw HTML is escaped to text. The literal
    // word "onerror" may survive inside that escaped text, which is inert.
    expect(html).not.toMatch(/<img\b/i)
    expect(html).not.toMatch(/<img[^>]*onerror/i)
    // Proof it was escaped rather than dropped/executed.
    expect(html).toContain("&lt;img")
  })

  it("a javascript: markdown link is dropped to inert text (no href)", () => {
    const html = render("[click me](javascript:alert(1))")
    expect(html.toLowerCase()).not.toContain("javascript:")
    expect(html).not.toContain('href="javascript')
    // The visible link text still shows.
    expect(html).toContain("click me")
  })

  it("a data: markdown link is dropped to inert text", () => {
    const html = render("[x](data:text/html,<script>alert(1)</script>)")
    expect(html).not.toContain('href="data:')
    expect(html).not.toContain("<script>")
  })

  it("a legitimate https link keeps its href with noopener/noreferrer + _blank", () => {
    const html = render("[docs](https://example.com/safe)")
    expect(html).toContain('href="https://example.com/safe"')
    expect(html).toContain('rel="noopener noreferrer"')
    expect(html).toContain('target="_blank"')
  })
})
