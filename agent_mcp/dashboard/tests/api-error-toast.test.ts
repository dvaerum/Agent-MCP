/**
 * Regression guards for the dashboard's API-error → toast surfacing.
 *
 * Bug surfaced by Firefox-MCP click-through on 2026-06-17 against v5.0.47
 * (commit `0ea1858`):
 *
 *   1. User opens Agents tab → clicks Deploy.
 *   2. User fills `agent_id="BadName!@#"` (invalid per PR #163's
 *      server-side regex).
 *   3. User clicks Deploy.
 *   4. Server correctly returns 400 with
 *      `{"message": "Error: invalid agent_id 'BadName!@#': must match
 *      ^[a-z][a-z0-9-]*[a-z0-9]$|^[a-z]$ ..."}`.
 *   5. Browser console logs 3 errors, the dialog **silently closes**,
 *      the Agent Fleet table renders unchanged.
 *   6. User sees nothing — no toast, no inline error, just disappearance.
 *
 * The server's validation is correct; the dashboard simply swallowed the
 * response. Two failures stack on top of each other in the client:
 *
 *   - `api.ts` `ApiClient.request` only used the response status line
 *     (`API Error: 400 Bad Request`); it discarded the parsed `message`
 *     field from the JSON body, so every caller's `error.message` was a
 *     generic "Bad Request" with no useful text.
 *   - `handleCreateAgent` (and similar mutation handlers across
 *     agents-dashboard / tasks-dashboard / memories-dashboard /
 *     prompt-book-dashboard / create-memory-modal / create-prompt-modal /
 *     edit-memory-modal / delete-memory-modal) caught the error and
 *     called `console.error` only; `CreateAgentModal`'s submit handler
 *     then immediately closed the dialog & reset the form, losing the
 *     user's input.
 *
 * This module pins the contract for the fix:
 *
 *   - The shared `ApiError` class carries `status`, `message` (the
 *     server's message verbatim), and `body` (the raw response text) so
 *     every caller has the same surface.
 *   - `api.ts::ApiClient.request` parses the JSON body on !ok responses
 *     and prefers `body.message` over the status line.
 *   - A shared toast primitive lives at
 *     `agent_mcp/dashboard/components/ui/toast.tsx` and is mounted via
 *     `<Toaster />` in `app/layout.tsx` so any module can
 *     `toastError(err)` without per-page wiring.
 *   - Mutation handlers in agents-dashboard.tsx call `toastError` on
 *     catch instead of the silent `console.error` pattern.
 *   - The Deploy modal awaits the create call and only closes / resets
 *     on success — on error the dialog stays open with the user's input
 *     intact.
 *
 * The grep-style file inspection pattern matches the house
 * source-grep convention (no jsdom in this repo for this kind of guard;
 * behaviour verified via `npm run build` plus Firefox MCP e2e).
 */

import { describe, expect, it } from "vitest"
import { readFileSync } from "node:fs"
import { resolve } from "node:path"
import { agentsPageSource } from "./support/agents-source"

const DASHBOARD_ROOT = resolve(__dirname, "..")
const readDashboard = (rel: string) =>
  readFileSync(resolve(DASHBOARD_ROOT, rel), "utf8")

const API_FILE = "lib/api/client.ts"
const AGENTS_TSX = "components/dashboard/agents-dashboard.tsx"
const LAYOUT_TSX = "app/layout.tsx"
const TOAST_TSX = "components/ui/toast.tsx"

// The Agents page is a page module + a directory of satellites since the
// <DataTablePage> migration (RegisterAgentModal now lives in
// components/dashboard/agents/register-agent-modal.tsx); guards about
// "the Agents page" read all of it.
function read(rel: string): string {
  if (rel === AGENTS_TSX) {
    return agentsPageSource()
  }
  return readDashboard(rel)
}

// ---------- ApiError class on the api.ts seam ---------------------------

describe("ApiError class", () => {
  it("is exported as a named class", () => {
    // `api.ts` must export a named `ApiError` class so callers and the
    // toast helper can `instanceof`-check it (and so future TS
    // consumers can `import { ApiError } from '@/lib/api'`).
    const src = read(API_FILE)
    expect(
      /export\s+class\s+ApiError\b/.test(src),
      "api.ts must declare and export a named `ApiError` class so " +
        "mutation handlers can distinguish API errors from generic " +
        "network / abort failures",
    ).toBe(true)
  })

  it("carries status and body fields", () => {
    // `ApiError` must carry `status` (HTTP code) and `body` (raw
    // response text) in addition to the standard Error `message` —
    // callers and tests need both to format meaningful toasts.
    const src = read(API_FILE)
    // Find the ApiError block (greedy until next class / top-level
    // export to keep the slice bounded).
    const m = src.match(/export\s+class\s+ApiError\b[^{]*\{([\s\S]*?)\n\}\n/)
    expect(m, "Could not locate ApiError class body in api.ts").not.toBeNull()
    const body = m![1]!
    expect(body.includes("status"), "ApiError must expose a `status` field").toBe(
      true,
    )
    expect(
      body.includes("body"),
      "ApiError must expose a `body` field (raw response text) so " +
        "callers can inspect / log the full server response",
    ).toBe(true)
  })
})

// ---------- Request layer surfaces the server's message ----------------

describe("request layer surfaces the server message", () => {
  it("parses the server message from the body", () => {
    // `ApiClient.request` must attempt to parse the response body as
    // JSON on !ok responses and prefer `body.message` over the bare
    // HTTP status line — otherwise the server's carefully-worded 400
    // text (PR #163) never reaches the UI.
    const src = read(API_FILE)
    // The fix must (1) JSON.parse the errorText and (2) use a
    // `.message` field off the parsed body. Match both signals.
    expect(
      src.includes("JSON.parse"),
      "api.ts must JSON.parse error response bodies so the server's " +
        "{message: ...} payload can be surfaced to the UI",
    ).toBe(true)
    expect(
      /\.message\b/.test(src),
      "api.ts must read the parsed body's `message` field",
    ).toBe(true)
  })

  it("throws ApiError, not a generic Error", () => {
    // On a !ok response, `ApiClient.request` must `throw new
    // ApiError(...)` carrying the status + parsed message, not the
    // generic `throw new Error('API Error: 400 Bad Request')` that
    // silently dropped the body pre-fix.
    const src = read(API_FILE)
    expect(
      /throw\s+new\s+ApiError\b/.test(src),
      "api.ts request layer must `throw new ApiError(...)` on !ok " +
        "responses (was `throw new Error('API Error: ...')` which " +
        "discarded the server message)",
    ).toBe(true)
  })
})

// ---------- Toast primitive lives at the shared seam -------------------

describe("shared toast primitive", () => {
  it("file exists", () => {
    // A shared toast primitive must live at `components/ui/toast.tsx`
    // so every dashboard tab imports the same component (matches the
    // shadcn convention used by the rest of components/ui/).
    expect(
      () => readDashboard(TOAST_TSX),
      `Missing shared toast primitive at ${TOAST_TSX} — mutation ` +
        "handlers need a single seam to render API errors",
    ).not.toThrow()
  })

  it("exports Toaster and toastError helpers", () => {
    // The toast module must export a `Toaster` component (mounted in
    // layout.tsx) and a `toastError(err)` helper so callers don't need
    // to know the toast store internals.
    const src = read(TOAST_TSX)
    expect(
      /export\s+(function|const)\s+Toaster\b/.test(src),
      "toast.tsx must export a `Toaster` portal component",
    ).toBe(true)
    expect(
      /export\s+(function|const)\s+toastError\b/.test(src),
      "toast.tsx must export a `toastError` helper that accepts an " +
        "ApiError (or any Error) and surfaces it to the user",
    ).toBe(true)
  })
})

describe("app/layout mounts the toaster", () => {
  it("mounts <Toaster />", () => {
    // `app/layout.tsx` must mount the `<Toaster />` portal so the toast
    // container exists in the React tree from the first render.
    const src = read(LAYOUT_TSX)
    expect(
      src.includes("Toaster"),
      "app/layout.tsx must mount <Toaster /> so toasts surface across " +
        "every dashboard tab",
    ).toBe(true)
  })
})

// ---------- agents-dashboard Deploy handler ----------------------------

describe("agents-dashboard Deploy handler", () => {
  it("calls toastError on catch", () => {
    // The Deploy submit handler in `agents-dashboard.tsx` must call
    // `toastError` on catch — silent `console.error` is exactly the bug
    // the 2026-06-17 Firefox-MCP click-through caught.
    const src = read(AGENTS_TSX)
    expect(
      src.includes("toastError"),
      "agents-dashboard.tsx must import and call `toastError` (was " +
        "silent `console.error` only — server message never reached " +
        "the user)",
    ).toBe(true)
  })

  it("RegisterAgentModal keeps the dialog open on error", () => {
    // `RegisterAgentModal.handleSubmit` (Wave 7 PR 3 made it the sole
    // agent-creation surface; the legacy `CreateAgentModal` is gone)
    // must await its submit and only call `setOpen(false)` after a
    // successful resolution — on error the dialog stays open so the
    // user doesn't lose their typed input.
    const src = read(AGENTS_TSX)
    // Locate the RegisterAgentModal block.
    const m = src.match(/const\s+RegisterAgentModal\s*=[\s\S]*?\n\}\n/)
    expect(m, "Could not locate RegisterAgentModal in agents-dashboard.tsx").not.toBeNull()
    const modal = m![0]
    // The submit handler must be async so it can await the API call.
    expect(
      /const\s+handleSubmit\s*=\s*async\b/.test(modal),
      "RegisterAgentModal.handleSubmit must be async so it can await " +
        "the register call and only close on success",
    ).toBe(true)
    // The submit handler must await the apiClient.registerAgent call
    // (otherwise the success-pane render races the request).
    expect(
      /await\s+apiClient\.registerAgent\b/.test(modal),
      "RegisterAgentModal.handleSubmit must `await " +
        "apiClient.registerAgent(...)` so dialog state can react to " +
        "success vs failure",
    ).toBe(true)
  })
})
