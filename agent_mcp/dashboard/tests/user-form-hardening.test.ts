/**
 * UX correctness guards for the router-level Users dashboard
 * (``components/dashboard/users-dashboard.tsx``).
 *
 * Both assertions are grep-based on source bytes, matching the
 * project's existing Vitest baseline (no jsdom / RTL). The properties
 * we pin are shapes of the source the ``next build`` type-check cannot
 * enforce:
 *
 *   UX-05  The Add-user password field carries a client-side
 *          ``minLength`` matching the server's canonical
 *          ``identity.PASSWORD_MIN_LENGTH`` (12), so a too-short
 *          password is rejected inline instead of bouncing off the
 *          server with an opaque 400.
 *
 *   UX-08  The Delete-user modal is behind a type-to-confirm guard: a
 *          controlled ``confirmText`` input gated against the exact
 *          username, and the destructive button disabled until it
 *          matches.
 */

import { describe, expect, it } from "vitest"
import { readFileSync } from "node:fs"
import { resolve } from "node:path"

const DASHBOARD_ROOT = resolve(__dirname, "..")
const read = (rel: string) =>
  readFileSync(resolve(DASHBOARD_ROOT, rel), "utf8")

const SRC = read("components/dashboard/users-dashboard.tsx")

describe("UX-05: add-user password min length", () => {
  it("declares PASSWORD_MIN_LENGTH = 12 to mirror the server rule", () => {
    expect(/PASSWORD_MIN_LENGTH\s*=\s*12\b/.test(SRC)).toBe(true)
  })

  it("binds minLength on the password Input", () => {
    expect(/minLength=\{PASSWORD_MIN_LENGTH\}/.test(SRC)).toBe(true)
  })

  it("disables submit while the password is shorter than the minimum", () => {
    expect(
      /password\.length\s*<\s*PASSWORD_MIN_LENGTH/.test(SRC),
      "Create button must gate on password length",
    ).toBe(true)
  })
})

describe("UX-08: delete-user type-to-confirm", () => {
  it("gates a confirmText input against the exact username", () => {
    expect(/confirmText\s*===\s*user\.username/.test(SRC)).toBe(true)
  })

  it("disables the destructive Delete button until confirmed", () => {
    expect(
      /disabled=\{submitting\s*\|\|\s*!confirmed\}/.test(SRC),
      "Delete button must stay disabled until the username is typed",
    ).toBe(true)
  })
})
