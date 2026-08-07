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
    // The jsdom UI tests (Radix dialogs + user-event) are an order of
    // magnitude slower than the pure-Node ones, and vitest fans the
    // whole suite across one worker per core. On a loaded machine a
    // single `it` can exceed the 5 s default purely from scheduling
    // pressure — a false red that says nothing about the code. 20 s is
    // still far below any real hang.
    testTimeout: 20_000,
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
