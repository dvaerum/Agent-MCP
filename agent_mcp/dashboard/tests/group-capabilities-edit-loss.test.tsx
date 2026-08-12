// @vitest-environment jsdom
//
// AUDIT AF-A / Bug 2 — group-capabilities edit-loss race.
//
// `save()` optimistically writes the PUT result into local state AND fires
// a background refetch via `invalidateGroupCapabilities`. The resync effect
// that folds a fresh GET back into `loaded`/`selected` must NOT clobber a
// capability the sysadmin toggled within ~1 RTT of Save — otherwise that
// edit is silently lost when the (stale) refetch resolves.
//
// Two properties pinned here:
//   1. A toggle made after Save but before the post-save refetch resolves
//      SURVIVES the refetch (no edit-loss); the Save button stays visible.
//   2. A plain, non-racing save still reconciles the checklist to the
//      server's set (the resync isn't just disabled).
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest"
import React from "react"
import {
  render,
  cleanup,
  fireEvent,
  waitFor,
  act,
} from "@testing-library/react"
import { QueryClientProvider } from "@tanstack/react-query"
import { queryClient } from "@/lib/query-client"
import { routerApi } from "@/lib/router-api"
import { GroupCapabilitiesSection } from "@/components/dashboard/groups/group-capabilities-section"

// Toasts render into a portal / call into a store; stub them out — this
// test is about the checklist state machine, not the toast surface.
vi.mock("@/components/ui/toast", () => ({
  toastError: vi.fn(),
  toastSuccess: vi.fn(),
}))

const CAP_A = "agents.view"
const CAP_B = "agents.register"
const CAP_C = "tasks.view"

function wrapper({ children }: { children: React.ReactNode }) {
  return (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  )
}

/** The checkbox whose row renders the given capability code. */
function capCheckbox(container: HTMLElement, cap: string): HTMLInputElement {
  const code = [...container.querySelectorAll("code")].find(
    (c) => c.textContent === cap,
  )
  if (!code) throw new Error(`no checklist row for ${cap}`)
  const input = code
    .closest("label")!
    .querySelector<HTMLInputElement>('input[type="checkbox"]')
  if (!input) throw new Error(`no checkbox for ${cap}`)
  return input
}

beforeEach(() => {
  queryClient.clear()
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe("group-capabilities edit-loss race (AF-A)", () => {
  it("preserves a toggle made before the post-save refetch resolves", async () => {
    // The post-save refetch (2nd GET) is held open so we can toggle a cap
    // while it is in flight, then release it with the stale server set.
    let releaseRefetch!: (caps: string[]) => void
    const refetch = new Promise<{ capabilities: string[] }>((res) => {
      releaseRefetch = (caps) => res({ capabilities: caps })
    })
    let getCount = 0

    vi.spyOn(routerApi, "request").mockImplementation(
      (async (_url: string, opts?: RequestInit) => {
        if (opts?.method === "PUT") {
          // Server accepts A + B (the pre-race Save).
          return { success: true, capabilities: [CAP_A, CAP_B] }
        }
        getCount += 1
        if (getCount === 1) return { capabilities: [CAP_A] }
        // 2nd GET == the post-save invalidate refetch: held open.
        return refetch
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
      }) as any,
    )

    const { container } = render(
      <GroupCapabilitiesSection groupId="g-race" groupName="Race" />,
      { wrapper },
    )

    // Initial GET lands: only A checked.
    await waitFor(() => expect(capCheckbox(container, CAP_A).checked).toBe(true))
    expect(capCheckbox(container, CAP_B).checked).toBe(false)

    // Toggle B on → dirty → Save appears; click it.
    fireEvent.click(capCheckbox(container, CAP_B))
    const saveBtn = await waitFor(() =>
      [...container.querySelectorAll("button")].find(
        (b) => b.textContent?.trim() === "Save",
      ),
    )
    fireEvent.click(saveBtn!)

    // PUT resolves → optimistic {A,B}; dirty clears so Save disappears.
    // The post-save invalidate has now kicked off the refetch (2nd GET),
    // held open above — assert it actually fired so the race is real.
    await waitFor(() =>
      expect(
        [...container.querySelectorAll("button")].some(
          (b) => b.textContent?.trim() === "Save",
        ),
      ).toBe(false),
    )
    await waitFor(() => expect(getCount).toBe(2))

    // RACE: toggle C on BEFORE the refetch resolves.
    fireEvent.click(capCheckbox(container, CAP_C))
    expect(capCheckbox(container, CAP_C).checked).toBe(true)

    // Release the refetch with the STALE server set (A + B, no C) and
    // flush RQ's cache-write → observer-notify → React commit → resync
    // effect. In the buggy code the effect unconditionally reruns
    // setSelected(new Set([A,B])), unchecking C by the time this settles.
    await act(async () => {
      releaseRefetch([CAP_A, CAP_B])
      await refetch
    })
    await act(async () => {
      await new Promise((r) => setTimeout(r, 0))
    })

    // The in-flight edit survived: C is still checked and Save is back
    // (loaded=[A,B] now diverges from selected={A,B,C}).
    expect(capCheckbox(container, CAP_C).checked).toBe(true)
    expect(
      [...container.querySelectorAll("button")].some(
        (b) => b.textContent?.trim() === "Save",
      ),
    ).toBe(true)
  })

  it("a plain (non-racing) save reconciles to the server's set", async () => {
    vi.spyOn(routerApi, "request").mockImplementation(
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      (async (_url: string, opts?: RequestInit) => {
        if (opts?.method === "PUT") {
          return { success: true, capabilities: [CAP_A, CAP_B] }
        }
        return { capabilities: [CAP_A] }
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
      }) as any,
    )

    const { container } = render(
      <GroupCapabilitiesSection groupId="g-plain" groupName="Plain" />,
      { wrapper },
    )

    await waitFor(() => expect(capCheckbox(container, CAP_A).checked).toBe(true))

    fireEvent.click(capCheckbox(container, CAP_B))
    const saveBtn = await waitFor(() =>
      [...container.querySelectorAll("button")].find(
        (b) => b.textContent?.trim() === "Save",
      ),
    )

    await act(async () => {
      fireEvent.click(saveBtn!)
    })

    // Server set (A + B) is reflected, and — no divergence — the checklist
    // is clean again (Save gone).
    await waitFor(() => {
      expect(capCheckbox(container, CAP_A).checked).toBe(true)
      expect(capCheckbox(container, CAP_B).checked).toBe(true)
      expect(
        [...container.querySelectorAll("button")].some(
          (b) => b.textContent?.trim() === "Save",
        ),
      ).toBe(false)
    })
  })
})
