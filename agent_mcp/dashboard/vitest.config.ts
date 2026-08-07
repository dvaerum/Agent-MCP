import { defineConfig } from "vitest/config"
import react from "@vitejs/plugin-react"
import path from "node:path"

// Minimal Vitest setup — added in Wave 2 (cleanup-wave-2) to back the
// grep-based and HTTP-shape assertions that guard the cookie-auth
// migration. The dashboard's runtime tree is intentionally NOT
// exercised here (no jsdom, no React-testing-library); these tests
// are pure-Node assertions against source text + a `fetch` stub.
//
// Why grep-based: the assertions we owe — "no call-site sends a
// `token` field in the body", "the MCP notifications client does
// NOT set Authorization" — are properties of the SOURCE, not the
// runtime, so executing the React tree would only obscure them.
// Future runtime tests can layer jsdom + RTL on top without
// disturbing this baseline.
export default defineConfig({
  // The XSS-inertness test (tests/memory-value-xss.test.ts) imports the
  // markdown component (memory-value-view.tsx) and renders it to a static
  // string via react-dom/server. The app's tsconfig sets `jsx: preserve`
  // (for Next's SWC), so vitest's own transform can't compile that JSX on
  // its own — the react plugin handles the JSX→JS transform for tests.
  plugins: [react()],
  test: {
    // The global env stays `node` so the 135 pure-Node tests are
    // unaffected. UI tests that need a DOM opt in per-file with a
    // `// @vitest-environment jsdom` docblock (see
    // components/dashboard/modals/delete-confirm-enter.test.tsx).
    include: [
      "tests/**/*.test.ts",
      "tests/**/*.test.tsx",
      "lib/**/*.test.ts",
      "components/**/*.test.tsx",
    ],
    environment: "node",
    // Hang detector, not a scheduling absorber.
    //
    // This was raised 5s→20s to stop a flake, and the bump was then
    // audited (fix/vitest-notifications-flake). Two REAL defects were
    // hiding underneath and are now fixed at the source:
    //
    //   1. `tests/mcp-notifications-*.test.ts` loaded their module
    //      graph with an in-test `await import()`, so Vitest billed
    //      transform + evaluation of the data-store/api-client/zustand
    //      graph to `testTimeout`. Measured 175ms idle but 1.3–3.1s
    //      oversubscribed — 85%+ of the test's wall time was module
    //      loading, not assertion. Static imports moved it to
    //      collection: that test went 363ms → 105ms idle and stopped
    //      timing out entirely, even at 2.5x oversubscription.
    //   2. Every jsdom UI test used user-event's default `delay: 0`,
    //      which awaits a REAL macrotask between each synthetic
    //      keystroke. `delay: null` (see tests/support/user-event.ts)
    //      took the worst test 1633ms → 820ms and roughly halved the
    //      whole distribution.
    //
    // What is left is genuine CPU starvation with no defect behind it:
    // jsdom + Radix render work that completes fine, just slowly, when
    // the box is oversubscribed. Capping the worker pool was tried and
    // rejected — it made the suite ~2x slower AND still failed 2/3.
    //
    // Measured after the fixes (16-core box, `npx vitest run`):
    //   idle                          worst `it` 820ms, suite 7.5s
    //   ambient load (~10-40)         6/6 green at 5s
    //   +16 spinners (2x oversub)     4/4 green at 5s
    //   +24 spinners (2.5x oversub)   fails at 5s (worst `it` 9.7s),
    //                                 3/3 green at 15s
    //
    // 15s is ~18x the idle worst case: enough headroom that a
    // pathologically busy dev box can't produce a false red, still far
    // below anything a genuinely stuck test would need (the hangs this
    // guards — an SSE body that never closes, an unresolved promise —
    // are unbounded, so any finite budget catches them).
    testTimeout: 15_000,
  },
  resolve: {
    alias: {
      // Mirror the tsconfig.json `@/*` → `./*` alias so a future test
      // that imports a module under test by its `@/lib/...` path
      // resolves the same way it would in the Next build.
      "@": path.resolve(__dirname, "."),
    },
  },
})
