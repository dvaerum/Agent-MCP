/**
 * AX-4 — theme applied before first paint, no FOUC.
 *
 * Two halves, both required:
 *   1. app/layout.tsx ships a BLOCKING inline <script> in <head> that
 *      reads the persisted theme and toggles the `dark` class before the
 *      body renders.
 *   2. components/providers/theme-provider.tsx no longer gates rendering
 *      behind a `!mounted` blank-render — that guard was the flash.
 *
 * Grep-based on source bytes (project Vitest baseline): both are
 * source-shape properties the build type-check can't enforce.
 */
import { describe, expect, it } from "vitest"
import { readFileSync } from "node:fs"
import { resolve } from "node:path"

const ROOT = resolve(__dirname, "..")
const read = (rel: string) => readFileSync(resolve(ROOT, rel), "utf8")

describe("AX-4: blocking pre-paint theme script", () => {
  const layout = read("app/layout.tsx")

  it("injects a raw inline script in the document head", () => {
    expect(/dangerouslySetInnerHTML=\{\{\s*__html:/.test(layout)).toBe(true)
  })

  it("reads the persisted theme from the zustand storage key", () => {
    expect(layout.includes('"theme-storage"')).toBe(true)
  })

  it("toggles the dark class from the resolved theme", () => {
    expect(/classList[\s\S]*add\("dark"\)/.test(layout)).toBe(true)
    expect(layout.includes('prefers-color-scheme: dark')).toBe(true)
  })

  it("keeps suppressHydrationWarning on <html> (class is set outside React)", () => {
    expect(/<html[^>]*suppressHydrationWarning/.test(layout)).toBe(true)
  })
})

describe("AX-4: no blank-render guard in ThemeProvider", () => {
  const provider = read("components/providers/theme-provider.tsx")

  it("dropped the !mounted guard", () => {
    expect(provider.includes("!mounted")).toBe(false)
    expect(provider.includes("setMounted")).toBe(false)
  })

  it("dropped the min-h-screen placeholder wrapper", () => {
    expect(provider.includes("min-h-screen bg-background")).toBe(false)
  })
})
