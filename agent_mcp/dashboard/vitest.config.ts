import { defineConfig } from "vitest/config"
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
  test: {
    include: ["tests/**/*.test.ts"],
    environment: "node",
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
