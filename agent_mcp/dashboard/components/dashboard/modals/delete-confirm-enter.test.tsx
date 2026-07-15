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

import { DeleteMemoryModal } from "@/components/dashboard/modals/delete-memory-modal"
import { DeleteMessageModal } from "@/components/dashboard/modals/delete-message-modal"

// Users/groups delete modals call the router API directly; stub it so the
// handler runs without network and we can assert it fired.
const requestMock = vi.fn((..._args: unknown[]) => Promise.resolve({}))
vi.mock("@/lib/router-api", () => ({
  routerApi: { request: (...args: unknown[]) => requestMock(...args) },
}))

import { DeleteUserModal } from "@/components/dashboard/users-dashboard"
import { DeleteGroupModal } from "@/components/dashboard/groups-dashboard"

import type { Memory, Message } from "@/lib/api"

const memory: Memory = {
  context_key: "some.key",
  value: "hello",
  description: "a memory",
  updated_at: "2026-01-01T00:00:00Z",
  updated_by: "admin",
}

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
  it("DeleteMemoryModal: Enter fires delete after correct confirmation", async () => {
    const u = ue()
    const onDeleteMemory = vi.fn(() => Promise.resolve())
    render(
      <DeleteMemoryModal
        memory={memory}
        open
        onOpenChange={() => {}}
        onDeleteMemory={onDeleteMemory}
      />,
    )
    const input = document.getElementById("confirmation") as HTMLElement
    await u.type(input, "DELETE")
    await u.keyboard("{Enter}")
    await waitFor(() => expect(onDeleteMemory).toHaveBeenCalledTimes(1))
  })

  it("DeleteMemoryModal: Enter with wrong text does NOT fire delete", async () => {
    const u = ue()
    const onDeleteMemory = vi.fn(() => Promise.resolve())
    render(
      <DeleteMemoryModal
        memory={memory}
        open
        onOpenChange={() => {}}
        onDeleteMemory={onDeleteMemory}
      />,
    )
    const input = document.getElementById("confirmation") as HTMLElement
    await u.type(input, "WRONG")
    await u.keyboard("{Enter}")
    expect(onDeleteMemory).not.toHaveBeenCalled()
  })

  it("DeleteMessageModal: Enter fires confirm after correct confirmation", async () => {
    const u = ue()
    const onConfirm = vi.fn(() => Promise.resolve())
    render(
      <DeleteMessageModal
        message={message}
        open
        onOpenChange={() => {}}
        onConfirm={onConfirm}
      />,
    )
    const input = document.getElementById("confirmation") as HTMLElement
    await u.type(input, "DELETE")
    await u.keyboard("{Enter}")
    await waitFor(() => expect(onConfirm).toHaveBeenCalledTimes(1))
  })

  it("DeleteMessageModal: Enter with wrong text does NOT fire confirm", async () => {
    const u = ue()
    const onConfirm = vi.fn(() => Promise.resolve())
    render(
      <DeleteMessageModal
        message={message}
        open
        onOpenChange={() => {}}
        onConfirm={onConfirm}
      />,
    )
    const input = document.getElementById("confirmation") as HTMLElement
    await u.type(input, "nope")
    await u.keyboard("{Enter}")
    expect(onConfirm).not.toHaveBeenCalled()
  })

  it("DeleteUserModal: Enter fires delete after typing the username", async () => {
    const u = ue()
    const onDeleted = vi.fn(() => Promise.resolve())
    render(
      <DeleteUserModal
        user={user}
        open
        onOpenChange={() => {}}
        onDeleted={onDeleted}
      />,
    )
    const input = document.getElementById("delete-user-confirm") as HTMLElement
    await u.type(input, user.username)
    await u.keyboard("{Enter}")
    await waitFor(() => expect(requestMock).toHaveBeenCalledTimes(1))
  })

  it("DeleteUserModal: Enter with wrong text does NOT fire delete", async () => {
    const u = ue()
    const onDeleted = vi.fn(() => Promise.resolve())
    render(
      <DeleteUserModal
        user={user}
        open
        onOpenChange={() => {}}
        onDeleted={onDeleted}
      />,
    )
    const input = document.getElementById("delete-user-confirm") as HTMLElement
    await u.type(input, "bob")
    await u.keyboard("{Enter}")
    expect(requestMock).not.toHaveBeenCalled()
  })

  it("DeleteGroupModal: Enter fires delete after typing the group name", async () => {
    const u = ue()
    const onDeleted = vi.fn(() => Promise.resolve())
    render(
      <DeleteGroupModal
        group={group}
        open
        onOpenChange={() => {}}
        onDeleted={onDeleted}
      />,
    )
    const input = document.getElementById("delete-group-confirm") as HTMLElement
    await u.type(input, group.name)
    await u.keyboard("{Enter}")
    await waitFor(() => expect(requestMock).toHaveBeenCalledTimes(1))
  })

  it("DeleteGroupModal: Enter with wrong text does NOT fire delete", async () => {
    const u = ue()
    const onDeleted = vi.fn(() => Promise.resolve())
    render(
      <DeleteGroupModal
        group={group}
        open
        onOpenChange={() => {}}
        onDeleted={onDeleted}
      />,
    )
    const input = document.getElementById("delete-group-confirm") as HTMLElement
    await u.type(input, "wrong-name")
    await u.keyboard("{Enter}")
    expect(requestMock).not.toHaveBeenCalled()
  })
})
