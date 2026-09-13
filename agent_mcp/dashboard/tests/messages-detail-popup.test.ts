/**
 * Regression guards for the Messages tab row-click detail popup.
 *
 * Today rows are truncated and the only way to see a full message is to
 * query the SQLite DB directly. This PR adds a click-on-the-row → modal
 * that shows every field of the message in a readable layout, plus
 * inline actions (Mark read/unread, Delete, Close).
 *
 * Text-parse regression guards (same convention as
 * messages-tab.test.ts / messages-dropdown.test.ts); we don't have jsdom
 * infrastructure and behavior is verified by `npm run build` plus
 * manual click-through in the live dashboard.
 */

import { describe, expect, it } from "vitest"
import { readFileSync } from "node:fs"
import { resolve } from "node:path"
import { messagesPageSource } from "./support/messages-source"

const DASHBOARD_ROOT = resolve(__dirname, "..")
const read = (rel: string) =>
  readFileSync(resolve(DASHBOARD_ROOT, rel), "utf8")

// Messages-page-parity PR: the detail popup was extracted out of
// messages-dashboard.tsx into its own <ViewMessageModal> (parity with
// memories' <ViewMemoryModal>). Wave 5 (refactor/w5-messages) then moved
// that modal into the `messages/` satellite directory alongside the
// extracted column spec. The modal-content guards below read the modal
// file; the row-interaction guards read the page + its satellites as one
// blob (the checkbox/delete cells now live in use-messages-columns.tsx).
const MODAL = "components/dashboard/messages/view-message-modal.tsx"

const readMessagesDashboard = () => messagesPageSource()
const readModal = () => read(MODAL)

// ---------- Modal primitives ------------------------------------

describe("detail popup modal primitives", () => {
  it("imports Dialog primitives", () => {
    const src = readModal()
    // We reuse the existing shadcn Dialog (already in
    // components/ui/dialog.tsx) so we don't add a new modal stack.
    for (const name of [
      "Dialog",
      "DialogContent",
      "DialogHeader",
      "DialogFooter",
      "DialogTitle",
    ]) {
      expect(
        src.includes(name),
        `expected Dialog primitive '${name}' to be imported`,
      ).toBe(true)
    }
    expect(
      src.includes("@/components/ui/dialog"),
      "expected the import to come from @/components/ui/dialog",
    ).toBe(true)
  })
})

// ---------- Row-click opens the modal ---------------------------

describe("row-click opens the detail modal", () => {
  it("has a click handler opening the detail dialog", () => {
    const src = readMessagesDashboard()
    // State that drives the detail modal. After the useDialog<T>()
    // migration (Candidate F1) the state lives on a hook named
    // detailDialog rather than a useState pair, but the substring
    // "detail" is still present as the hook name.
    expect(
      src.includes("detailDialog"),
      "expected detailDialog (useDialog<Message>()) to back the modal",
    ).toBe(true)
    // The row body must carry a click handler that opens the modal.
    //
    // Pre-scaffold the page hand-rolled `<TableRow onClick=…>`. After
    // the <DataTablePage> migration the row element is owned by the
    // shared <ResponsiveDataTable>, which attaches the handler the page
    // passes as `onRowClick` (and does so for BOTH the desktop row and
    // the mobile card — strictly more coverage than the old inline
    // TableRow). Accept either spelling; the invariant is that the
    // row-body click opens the detail dialog.
    expect(
      src.includes("<TableRow") || src.includes("onRowClick"),
      "expected a row-body click handler — either an inline " +
        "<TableRow onClick> or an onRowClick passed to the shared " +
        "<DataTablePage> / <ResponsiveDataTable>",
    ).toBe(true)
    // The handler must reference the hook's .open(...) method (the
    // post-migration replacement for setDetailMessage).
    expect(
      src.includes("detailDialog.open"),
      "expected detailDialog.open(m) to be wired onto the row's onClick",
    ).toBe(true)
  })

  it("stops propagation on the checkbox cell", () => {
    // Clicking the checkbox column must NOT open the modal.
    const src = readMessagesDashboard()
    // The existing checkbox + per-row delete cells already wrap their
    // onClick with stopPropagation; this PR keeps that contract so the
    // bulk-select / per-row delete behaviour is preserved.
    expect(
      src.includes("stopPropagation"),
      "expected stopPropagation on the checkbox / action cells so " +
        "they don't bubble up and open the detail modal",
    ).toBe(true)
  })
})

// ---------- Modal content ---------------------------------------

describe("detail popup modal content", () => {
  it("shows the full content block un-truncated", () => {
    const src = readModal()
    // Full content must be rendered in a pre-wrap / monospace block —
    // not truncated. We accept whitespace-pre-wrap (Tailwind) as the
    // marker; the row table cell still uses `truncate`.
    expect(
      src.includes("whitespace-pre-wrap"),
      "expected the detail modal to render message_content with " +
        "whitespace-pre-wrap so newlines are preserved",
    ).toBe(true)
  })

  it("renders every field label", () => {
    const src = readModal()
    // The modal labels every field — these are user-facing strings.
    for (const label of [
      "Message ID",
      "Sender",
      "Recipient",
      "Type",
      "Priority",
      "Delivered",
      "Read",
      "Content",
    ]) {
      expect(
        src.includes(label),
        `expected the detail modal to label '${label}'`,
      ).toBe(true)
    }
  })
})

// ---------- Modal actions ---------------------------------------

describe("detail popup modal actions", () => {
  it("has a mark-read/mark-unread toggle", () => {
    const src = readModal()
    // The single button toggles between "Mark read" and "Mark unread"
    // based on the current row's read flag — both strings must appear
    // so either branch can render. Mark-read stays inline (the modal
    // stays open — live-lookup re-renders it with the fresh row).
    expect(
      src.includes("Mark read") && src.includes("Mark unread"),
      "expected the detail modal footer to expose a Mark read / " +
        "Mark unread toggle button",
    ).toBe(true)
  })

  it("routes delete through the shared confirm dialog", () => {
    // Messages-page-parity PR: the modal Delete no longer fires an
    // unconfirmed DELETE. It routes through a type-DELETE-to-confirm
    // dialog — closing the real no-confirm gap the audit flagged.
    //
    // Scaffold migration: that dialog is now the unified
    // <DeleteConfirmModal> (delete-message-modal.tsx was subsumed, the
    // same way delete-memory-modal.tsx was by PR #581) — single-row
    // delete passes the message preview as `details`, bulk delete
    // overrides title/description/warning with the count-aware copy.
    const modal = readModal()
    expect(
      /\bDelete\b\s*<\/Button>/.test(modal),
      "expected a Delete button in the detail modal footer",
    ).toBe(true)
    expect(
      modal.includes("onDelete"),
      "expected the modal Delete button to defer to the parent via " +
        "an onDelete prop (parent opens the confirm dialog)",
    ).toBe(true)
    const dash = readMessagesDashboard()
    expect(
      (dash.includes("DeleteConfirmModal") ||
        dash.includes("DeleteMessageModal")) &&
        dash.includes("deleteDialog.open"),
      "expected the dashboard to route deletes through the shared " +
        "type-DELETE-to-confirm dialog (no unconfirmed delete)",
    ).toBe(true)
  })

  it("has an explicit Close button", () => {
    const src = readModal()
    // "Close" footer button is explicit (in addition to the Dialog's
    // built-in X close affordance). Tolerate whitespace between the
    // opening Button tag and the literal text.
    expect(
      /\bClose\b\s*<\/Button>/.test(src),
      "expected an explicit Close button in the modal footer",
    ).toBe(true)
  })
})
