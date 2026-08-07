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
 *   UX-08  The Delete-user flow is behind a type-to-confirm guard
 *          gated against the exact (case-sensitive) username, with the
 *          destructive button disabled until it matches.
 *
 *          The users page no longer hand-rolls that dialog: it
 *          delegates to the shared ``<DeleteConfirmModal>``. UX-08 is
 *          therefore asserted in two halves — the page passes
 *          ``requiredWord={<username>}`` + ``matchCase``, and the
 *          shared modal actually implements the case-sensitive compare
 *          and the disabled-until-confirmed button. Both halves are
 *          required, so this is an equivalence rather than a loophole
 *          (same delegation-aware shape as the CC-3/CC-6/CC-7 audits in
 *          tests/test_dashboard_polish_mobile_pass.py).
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

const DELETE_CONFIRM = read(
  "components/dashboard/modals/delete-confirm-modal.tsx",
)

describe("UX-08: delete-user type-to-confirm", () => {
  it("routes the delete through the shared DeleteConfirmModal", () => {
    expect(SRC).toContain("<DeleteConfirmModal")
    expect(
      /from\s+["'][^"']*modals\/delete-confirm-modal["']/.test(SRC),
      "users-dashboard must import the shared delete-confirm modal",
    ).toBe(true)
  })

  it("gates the confirmation on the exact, case-sensitive username", () => {
    expect(
      /requiredWord=\{deleteTarget\.username\}/.test(SRC),
      "the confirmation word must be the account's username",
    ).toBe(true)
    expect(
      /\bmatchCase\b/.test(SRC),
      "username confirmation must be case-sensitive (matchCase)",
    ).toBe(true)
  })

  it("the shared modal implements the case-sensitive compare", () => {
    expect(
      /matchCase\s*\n?\s*\?\s*confirmationText\s*===\s*requiredWord/.test(
        DELETE_CONFIRM,
      ),
      "DeleteConfirmModal must compare exactly when matchCase is set",
    ).toBe(true)
  })

  it("the shared modal disables the destructive button until confirmed", () => {
    expect(
      /disabled=\{loading\s*\|\|\s*!isConfirmed\}/.test(DELETE_CONFIRM),
      "Delete button must stay disabled until the required word is typed",
    ).toBe(true)
  })
})
