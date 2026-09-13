/**
 * Static-grep guards for the dashboard's session-cookie auth surface.
 *
 * Asserts that:
 *
 *   * ``agent_mcp/dashboard/lib/api/*.ts`` no longer splices
 *     ``token: tokens.admin_token`` into mutation payloads — the session
 *     cookie is what authenticates.
 *   * ``ApiClient`` redirects to ``/agent-mcp/login`` on a 401, preserving
 *     the current path in ``?next=``.
 *
 * Phase F (prancy-napping-pie): this file used to also grep-check the
 * Python backend (``agent_mcp/app/routers/*.py``, ``agent_mcp/router/
 * app.py``) for the same body-token-read pattern, plus a TODO-marker
 * sweep over the whole ``agent_mcp/**\/*.py`` tree — all three deleted
 * here, not trimmed: their subject (the Python router/app auth-handler
 * layer) is gone, superseded by ``conexus-router``/``conexus-backend``'s
 * own Rust auth gates (`rest_gate.rs`/`session_gate.rs`), which carry
 * their own test coverage. What survives is pure dashboard-TS grep
 * coverage, unaffected by that deletion.
 */

import { describe, expect, it } from "vitest"
import { readdirSync, readFileSync } from "node:fs"
import { resolve } from "node:path"

const DASHBOARD_ROOT = resolve(__dirname, "..")
// W6-followup F1 split the old lib/api.ts God-module into per-resource
// modules under lib/api/. The mutation payloads (token-strip guard) and
// the request core (401 redirect) now live across several of them, so
// read the whole directory as one blob.
const API_TS_DIR = resolve(DASHBOARD_ROOT, "lib", "api")

function readApiClient(): string {
  // Concatenate every per-resource api module (core + bundles).
  return readdirSync(API_TS_DIR)
    .filter((f) => f.endsWith(".ts"))
    .sort()
    .map((f) => readFileSync(resolve(API_TS_DIR, f), "utf8"))
    .join("\n")
}

// ── dashboard: token field stripped from mutation payloads ────────

const ADMIN_TOKEN_IN_BODY = /token:\s*tokens\.admin_token/

describe("dashboard api client: no admin token in mutation payloads", () => {
  it("apiClient mutation payloads no longer include token: tokens.admin_token", () => {
    // ``apiClient.createAgent`` / ``editAgent`` / ``terminateAgent`` /
    // ``restoreAgent`` / ``purgeAgent`` / ``createTask`` / ``updateTask`` /
    // ``deleteTask`` no longer include ``token: tokens.admin_token`` in
    // mutation bodies — the cookie carries auth now.
    const text = readApiClient()
    const hits: Array<[number, string]> = []
    text.split("\n").forEach((line, i) => {
      const n = i + 1
      if (line.trim().startsWith("//")) return
      if (ADMIN_TOKEN_IN_BODY.test(line)) hits.push([n, line.trimEnd()])
    })
    expect(
      hits,
      "Dashboard mutation payloads still include token: tokens.admin_token:\n  " +
        hits.map(([n, ln]) => `${n}: ${ln}`).join("\n  "),
    ).toEqual([])
  })
})

// ── ApiClient 401 redirect handler exists ─────────────────────────

describe("dashboard api client: 401 redirect handler", () => {
  it("redirects to /agent-mcp/login on a 401, preserving the path in ?next=", () => {
    // ApiClient must redirect to /agent-mcp/login on a 401, preserving
    // the current path in ``?next=`` so post-login the operator lands
    // back where they started.
    const text = readApiClient()
    // Loose match: must reference both ``401`` and ``/agent-mcp/login``
    // somewhere in the file plus ``next=`` for the preserved path.
    expect(text.includes("401"), "ApiClient must inspect the 401 status code").toBe(true)
    expect(
      text.includes("/agent-mcp/login"),
      "ApiClient must redirect to /agent-mcp/login on 401",
    ).toBe(true)
    expect(
      text.includes("next=") || text.includes("next ="),
      "ApiClient 401 redirect must preserve the current path via ?next=",
    ).toBe(true)
  })
})
