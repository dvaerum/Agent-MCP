/**
 * Regression guards for the dashboard PathPrefix adapter refactor
 * (Candidate C, architecture review 2026-06-01).
 *
 * The original bootstrap was a 3-useEffect dance inside
 * `components/providers/api-client-initializer.tsx`:
 *   1. Auto-seed a synthetic server-store entry from
 *      `window.location.pathname` (gated on zustand-persist hydration).
 *   2. Sync `activeServerId` → `apiClient.setServer(...)` and override
 *      baseUrl when the URL pattern matches.
 *   3. Cold-start retry: poll `setActiveServer` every 1.5s up to 30
 *      times (45s budget) so the dashboard reconnects once the lazily-
 *      spawned backend's socket finally appears.
 *
 * That dance was three-effect ordering theater for two facts known
 * synchronously at page load:
 *   - the project name + API root (derived from `window.location.pathname`)
 *   - the connect attempt has to be retried until the backend is up
 *     (a fetch concern, not a React-effect concern)
 *
 * Candidate C collapses it into:
 *   - A **module-level singleton** in `agent_mcp/dashboard/lib/project-context.ts`
 *     that runs at module import and exposes `projectContext` plus a
 *     React `ProjectContext`.
 *   - A **Provider in `app/layout.tsx`** that propagates the resolved
 *     values to all children that need them.
 *   - **Transparent retry inside `ApiClient.request()`**: on 502/503/504
 *     responses, retry with exponential backoff (200ms, 400ms — 3
 *     attempts total). Callers see success or a hard failure; the
 *     boundary-level retry useEffect disappears.
 *
 * These guards are text-level (the fork has no jsdom/RTL infrastructure
 * for behavioural dashboard tests). Build + manual click-through verify
 * behaviour on the PR.
 */

import { describe, expect, it } from "vitest"
import { existsSync, readFileSync } from "node:fs"
import { resolve } from "node:path"

const DASHBOARD_ROOT = resolve(__dirname, "..")
const PROJECT_CONTEXT = resolve(DASHBOARD_ROOT, "lib", "project-context.ts")
const LAYOUT = resolve(DASHBOARD_ROOT, "app", "layout.tsx")
const PROVIDER = resolve(
  DASHBOARD_ROOT,
  "components",
  "providers",
  "project-context-provider.tsx",
)
const API = resolve(DASHBOARD_ROOT, "lib", "api", "client.ts")
const OLD_INIT = resolve(
  DASHBOARD_ROOT,
  "components",
  "providers",
  "api-client-initializer.tsx",
)

const read = (p: string) => readFileSync(p, "utf8")

// -- project-context.ts ----------------------------------------------------

describe("lib/project-context.ts", () => {
  it("exists as the module-level singleton", () => {
    // The module-level singleton lives in `lib/project-context.ts`.
    expect(
      existsSync(PROJECT_CONTEXT),
      `expected ${PROJECT_CONTEXT} to exist — Candidate C makes the ` +
        "PathPrefix derivation a module-level singleton instead of a " +
        "useEffect",
    ).toBe(true)
  })

  it("exports the singleton and the React context", () => {
    // Module must export `projectContext` (the resolved values) and
    // `ProjectContext` (the React context for provider/consumer wiring).
    const src = read(PROJECT_CONTEXT)
    expect(
      src.includes("export const projectContext"),
      "expected `export const projectContext` — the module-level " +
        "singleton holding {projectName, baseUrl, apiPrefix}",
    ).toBe(true)
    expect(
      src.includes("export const ProjectContext"),
      "expected `export const ProjectContext` — the React context " +
        "used by the Provider in app/layout.tsx",
    ).toBe(true)
    expect(
      src.includes("createContext"),
      "expected `createContext` import from React for ProjectContext",
    ).toBe(true)
  })

  it("derives from pathname synchronously", () => {
    // Derivation happens at module import. The singleton inspects
    // `window.location.pathname` (SSR fallback: typeof window check)
    // and matches against the path-prefix regex.
    const src = read(PROJECT_CONTEXT)
    expect(
      src.includes("window.location.pathname"),
      "expected the singleton to read `window.location.pathname` " +
        "directly (no useEffect)",
    ).toBe(true)
    expect(
      src.includes("typeof window"),
      "expected `typeof window !== 'undefined'` SSR guard so the " +
        "module imports cleanly during Next.js prerender",
    ).toBe(true)
    // PR-B moved the URL string literals into lib/urls.ts and
    // project-context now imports the regex helper. Assert the import
    // exists; the literal-presence guard moved into the urls.ts test
    // (test_dashboard_urls_module below).
    expect(
      src.includes("APP_PROJECT_PATH_RE"),
      "expected project-context.ts to import the URL regex helper " +
        "from lib/urls.ts (PR-B centralisation)",
    ).toBe(true)
    const urlsSrc = read(resolve(DASHBOARD_ROOT, "lib", "urls.ts"))
    expect(
      urlsSrc.includes("/agent-mcp/app"),
      "expected the path-prefix literal `/agent-mcp/app` in " +
        "lib/urls.ts (PR-B renamed from /__dashboard/)",
    ).toBe(true)
    expect(
      urlsSrc.includes("/agent-mcp/api"),
      "expected the derived API root literal `/agent-mcp/api` in " +
        "lib/urls.ts (PR-B renamed from /__api/)",
    ).toBe(true)
  })
})

// -- app/layout.tsx Provider wiring ---------------------------------------

describe("app/layout.tsx Provider wiring", () => {
  it("wraps children in the ProjectContext provider", () => {
    // The Provider is wired into `app/layout.tsx` so every dashboard
    // route sees the resolved values.
    //
    // Next.js Server/Client boundary requires the actual
    // `<ProjectContext.Provider value={projectContext}>` to live in a
    // "use client" module — `app/layout.tsx` is a server component
    // (exports `metadata` + `viewport`). The Provider is therefore
    // extracted into a thin client wrapper
    // (`components/providers/project-context-provider.tsx`) that
    // layout.tsx renders. We accept either pattern: the Provider
    // rendered directly in layout, or via the wrapper component.
    const layoutSrc = read(LAYOUT)
    const direct =
      layoutSrc.includes("ProjectContext.Provider") &&
      layoutSrc.includes("projectContext")
    const wrapperInLayout = layoutSrc.includes("ProjectContextProvider")
    expect(
      direct || wrapperInLayout,
      "expected `<ProjectContext.Provider value={projectContext}>` " +
        "in app/layout.tsx OR a `<ProjectContextProvider>` client " +
        "wrapper rendered from app/layout.tsx",
    ).toBe(true)
    if (wrapperInLayout) {
      expect(
        existsSync(PROVIDER),
        "layout.tsx references ProjectContextProvider but the " +
          "wrapper module is missing",
      ).toBe(true)
      const providerSrc = read(PROVIDER)
      expect(
        providerSrc.includes("ProjectContext.Provider"),
        "expected `<ProjectContext.Provider>` inside the client " +
          `wrapper at ${PROVIDER}`,
      ).toBe(true)
      expect(
        providerSrc.includes("projectContext"),
        "expected the singleton `projectContext` to be passed as " +
          "the Provider value in the wrapper",
      ).toBe(true)
    }
  })
})

// -- ApiClient transparent retry ------------------------------------------

describe("ApiClient transparent retry on 5xx", () => {
  it("retries on 502/503/504 with exponential backoff", () => {
    // `ApiClient.request()` must retry on 502/503/504 with
    // exponential backoff (cold-start case: backend systemd unit takes
    // 10–15s to come up after a request lands on the router).
    const src = read(API)
    // Retry loop signature: bounded attempts + 5xx detection + setTimeout.
    expect(
      src.includes("attempt") && src.includes("< 3"),
      "expected a bounded `for (let attempt = 0; attempt < 3; ...)` " +
        "retry loop in ApiClient.request()",
    ).toBe(true)
    // Status 5xx detection. Accept either an explicit code list or a
    // `>= 500 && < 600` range check.
    const hasExplicit = src.includes("502") && src.includes("503")
    const hasRange = src.includes(">= 500") && src.includes("< 600")
    expect(
      hasExplicit || hasRange,
      "expected ApiClient.request() to detect 502/503/504 (either " +
        "explicit codes or `>= 500 && < 600` range)",
    ).toBe(true)
    // Exponential backoff via setTimeout. Match `* 2 **` (the doubling
    // multiplier) — robust to whitespace and base value.
    expect(
      src.includes("setTimeout") && src.includes("* 2 **"),
      "expected exponential backoff `setTimeout(..., base * 2 ** attempt)` " +
        "in the retry loop",
    ).toBe(true)
  })
})

// -- old bootstrap useEffects removed -------------------------------------

describe("old api-client-initializer bootstrap useEffects", () => {
  it("no longer contains the cold-start retry or hydration-gated seed", () => {
    // The 3-useEffect bootstrap dance in
    // `api-client-initializer.tsx` is the regression we are killing.
    // Either the file is deleted entirely, or it no longer contains
    // the cold-start `setInterval` retry loop nor the
    // `onFinishHydration`-gated auto-seed.
    //
    // A module-level singleton + ApiClient.request() retry replace both.
    if (!existsSync(OLD_INIT)) {
      // File deleted — best case, the whole bootstrap module is gone.
      return
    }
    const src = read(OLD_INIT)
    expect(
      src.includes("setInterval"),
      "the cold-start retry useEffect (setInterval-based polling) " +
        "must be removed — retry now lives transparently inside " +
        "ApiClient.request()",
    ).toBe(false)
    expect(
      src.includes("onFinishHydration") || src.includes("hasHydrated"),
      "the persist-hydration-gated auto-seed useEffect must be " +
        "removed — derivation is now synchronous at module import in " +
        "lib/project-context.ts",
    ).toBe(false)
  })

  it("layout.tsx no longer renders the old ApiClientInitializer", () => {
    // `<ApiClientInitializer />` was the boundary that ran the
    // useEffect dance. After the refactor, layout.tsx must not render
    // it (the Provider replaces it).
    const src = read(LAYOUT)
    expect(
      src.includes("<ApiClientInitializer"),
      "expected `<ApiClientInitializer />` to be removed from " +
        "app/layout.tsx — replaced by `<ProjectContext.Provider>`",
    ).toBe(false)
  })
})
