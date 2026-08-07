// @vitest-environment jsdom
//
// UI regression test for the type-to-confirm delete dialogs: after the
// operator types the confirmation word, pressing Enter must trigger the
// delete (previously a no-op — the confirm <Input> had no submit wiring).
// Covers all four class-swept dialogs. RED before the onEnterSubmit fix,
// GREEN after.
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest"
import { render, cleanup, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

// Radix Dialog locks `pointer-events: none` on <body> while open; that
// makes user-event's default pointer-events guard throw. Disable the
// guard — we drive the input via keyboard, not real pointer hit-testing.
const ue = () => userEvent.setup({ pointerEventsCheck: 0 })

import { DeleteConfirmModal } from "@/components/dashboard/modals/delete-confirm-modal"

// The groups delete modal calls the router API directly; stub it so the
// handler runs without network and we can assert it fired. (The users
// page no longer ships its own delete modal — it delegates to the
// shared <DeleteConfirmModal> with `requiredWord={username}` +
// `matchCase`, pinned by the matchCase case below.)
const requestMock = vi.fn((..._args: unknown[]) => Promise.resolve({}))
vi.mock("@/lib/router-api", () => ({
  routerApi: { request: (...args: unknown[]) => requestMock(...args) },
}))

import { routerApi } from "@/lib/router-api"
import { routerGroupUrl } from "@/lib/urls"

import type { Message } from "@/lib/api"

const message: Message = {
  message_id: "m1",
  sender_id: "a",
  recipient_id: "b",
  message_content: "hi",
  message_type: "chat",
  priority: "normal",
  timestamp: "2026-01-01T00:00:00Z",
  delivered: 1,
  read: 0,
  subject: "subj",
  parent_message_id: null,
}

const user = {
  user_id: "u1",
  username: "alice",
  email: null,
  is_sysadmin: false,
  created_at: "2026-01-01T00:00:00Z",
  last_login_at: null,
}

const group = {
  group_id: "g1",
  name: "devs",
  is_sysadmin: false,
  created_at: "2026-01-01T00:00:00Z",
  member_count: 2,
}

beforeEach(() => {
  requestMock.mockClear()
})
afterEach(() => cleanup())

describe("Enter submits type-to-confirm delete dialogs", () => {
  it("DeleteConfirmModal: Enter fires confirm after correct confirmation", async () => {
    const u = ue()
    const onConfirm = vi.fn(() => Promise.resolve())
    render(
      <DeleteConfirmModal
        open
        onOpenChange={() => {}}
        onConfirm={onConfirm}
        entityLabel="Memory"
      />,
    )
    const input = document.getElementById("confirmation") as HTMLElement
    await u.type(input, "DELETE")
    await u.keyboard("{Enter}")
    await waitFor(() => expect(onConfirm).toHaveBeenCalledTimes(1))
  })

  it("DeleteConfirmModal: Enter with wrong text does NOT fire confirm", async () => {
    const u = ue()
    const onConfirm = vi.fn(() => Promise.resolve())
    render(
      <DeleteConfirmModal
        open
        onOpenChange={() => {}}
        onConfirm={onConfirm}
        entityLabel="Memory"
      />,
    )
    const input = document.getElementById("confirmation") as HTMLElement
    await u.type(input, "WRONG")
    await u.keyboard("{Enter}")
    expect(onConfirm).not.toHaveBeenCalled()
  })

  it("DeleteConfirmModal: matchCase requires exact-case name (users/groups contract)", async () => {
    const u = ue()
    const onConfirm = vi.fn(() => Promise.resolve())
    render(
      <DeleteConfirmModal
        open
        onOpenChange={() => {}}
        onConfirm={onConfirm}
        entityLabel="User"
        requiredWord="alice"
        matchCase
      />,
    )
    const input = document.getElementById("confirmation") as HTMLElement
    await u.type(input, "ALICE")
    await u.keyboard("{Enter}")
    expect(onConfirm).not.toHaveBeenCalled()
    await u.clear(input)
    await u.type(input, "alice")
    await u.keyboard("{Enter}")
    await waitFor(() => expect(onConfirm).toHaveBeenCalledTimes(1))
  })

  // The messages page's single/bulk delete dialogs are the shared
  // <DeleteConfirmModal> too (delete-message-modal.tsx was subsumed by
  // the Messages migration, exactly as delete-memory-modal.tsx was by
  // the Memories one). Keep the Enter-submit contract pinned for the
  // message variants — details preview for the single row, a
  // count-titled variant with no details for the bulk delete.
  it("DeleteConfirmModal (message): Enter fires confirm after correct confirmation", async () => {
    const u = ue()
    const onConfirm = vi.fn(() => Promise.resolve())
    render(
      <DeleteConfirmModal
        open
        onOpenChange={() => {}}
        onConfirm={onConfirm}
        entityLabel="Message"
        details={<div>{message.message_content}</div>}
      />,
    )
    const input = document.getElementById("confirmation") as HTMLElement
    await u.type(input, "DELETE")
    await u.keyboard("{Enter}")
    await waitFor(() => expect(onConfirm).toHaveBeenCalledTimes(1))
  })

  it("DeleteConfirmModal (message): Enter with wrong text does NOT fire confirm", async () => {
    const u = ue()
    const onConfirm = vi.fn(() => Promise.resolve())
    render(
      <DeleteConfirmModal
        open
        onOpenChange={() => {}}
        onConfirm={onConfirm}
        entityLabel="Message"
        details={<div>{message.message_content}</div>}
      />,
    )
    const input = document.getElementById("confirmation") as HTMLElement
    await u.type(input, "nope")
    await u.keyboard("{Enter}")
    expect(onConfirm).not.toHaveBeenCalled()
  })

  it("DeleteConfirmModal (bulk messages): count-titled variant still Enter-submits", async () => {
    const u = ue()
    const onConfirm = vi.fn(() => Promise.resolve())
    render(
      <DeleteConfirmModal
        open
        onOpenChange={() => {}}
        onConfirm={onConfirm}
        entityLabel="Message"
        inputId="bulk-confirmation"
        title="Delete 3 Messages"
        description="This action cannot be undone. The 3 selected messages will be permanently deleted."
        confirmLabel="Delete 3 Messages"
      />,
    )
    const input = document.getElementById("bulk-confirmation") as HTMLElement
    await u.type(input, "DELETE")
    await u.keyboard("{Enter}")
    await waitFor(() => expect(onConfirm).toHaveBeenCalledTimes(1))
  })

  // Users delete: the page-level modal was retired in favour of the
  // shared <DeleteConfirmModal>. Its Enter + exact-case-username
  // contract is covered by the `matchCase` case above; the wiring
  // (requiredWord={username} + matchCase + inputId) is pinned by
  // tests/user-form-hardening.test.ts (UX-08).
  it("users page delegates its delete to the shared modal with the username as the confirm word", async () => {
    const u = ue()
    const onConfirm = vi.fn(() => Promise.resolve())
    render(
      <DeleteConfirmModal
        open
        onOpenChange={() => {}}
        onConfirm={onConfirm}
        entityLabel="User"
        requiredWord={user.username}
        matchCase
        inputId="delete-user-confirm"
      />,
    )
    const input = document.getElementById("delete-user-confirm") as HTMLElement
    await u.type(input, "bob")
    await u.keyboard("{Enter}")
    expect(onConfirm).not.toHaveBeenCalled()
    await u.clear(input)
    await u.type(input, user.username)
    await u.keyboard("{Enter}")
    await waitFor(() => expect(onConfirm).toHaveBeenCalledTimes(1))
  })

  // Groups migrated onto the shared scaffold: the bespoke
  // `DeleteGroupModal` is gone and groups-dashboard now renders
  // <DeleteConfirmModal requiredWord={group.name} matchCase
  // inputId="delete-group-confirm">. These two cases render that exact
  // configuration so the Enter-to-submit contract stays pinned.
  const groupsDeleteModal = (onOpenChange = () => {}) => (
    <DeleteConfirmModal
      open
      onOpenChange={onOpenChange}
      entityLabel="Group"
      requiredWord={group.name}
      matchCase
      inputId="delete-group-confirm"
      onConfirm={async () => {
        await routerApi.request(routerGroupUrl(group.group_id), {
          method: "DELETE",
        })
      }}
    />
  )

  it("groups delete: Enter fires delete after typing the group name", async () => {
    const u = ue()
    render(groupsDeleteModal())
    const input = document.getElementById("delete-group-confirm") as HTMLElement
    await u.type(input, group.name)
    await u.keyboard("{Enter}")
    await waitFor(() => expect(requestMock).toHaveBeenCalledTimes(1))
  })

  it("groups delete: Enter with wrong text does NOT fire delete", async () => {
    const u = ue()
    render(groupsDeleteModal())
    const input = document.getElementById("delete-group-confirm") as HTMLElement
    await u.type(input, "wrong-name")
    await u.keyboard("{Enter}")
    expect(requestMock).not.toHaveBeenCalled()
  })
})
