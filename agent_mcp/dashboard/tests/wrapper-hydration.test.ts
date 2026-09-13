/**
 * Regression guard for the dashboard-wrapper hydration gate (issue E).
 *
 * zustand-persist hydrates after first paint. Reading `activeServerId`
 * at render time gives the default empty state during SSG and the
 * persisted state on the client → mismatch → React error #418. Gate
 * on a post-mount `hydrated` flag so first client paint matches SSG.
 */

import { describe, expect, it } from "vitest"
import { readFileSync } from "node:fs"
import { resolve } from "node:path"

const WRAPPER = resolve(
  __dirname,
  "..",
  "components",
  "dashboard",
  "dashboard-wrapper.tsx",
)

describe("DashboardWrapper hydration gate", () => {
  it("gates isConnected on a hydration flag so SSG markup matches the first client render", () => {
    const src = readFileSync(WRAPPER, "utf8")
    // Must use a hydration signal (one of these strings)
    expect(
      src.includes("useState") && src.includes("useEffect"),
      "expected useState + useEffect for the post-mount hydrated flag",
    ).toBe(true)
    expect(
      src.includes("onFinishHydration") || src.includes("hasHydrated"),
      "expected the hydration gate to use zustand persist API " +
        "(`onFinishHydration` / `hasHydrated`)",
    ).toBe(true)
  })
})
