// @vitest-environment jsdom
//
// The server-picker surfaces used six native `window.confirm()` calls.
// Those bypass the tier model entirely: no styling, no
// `role="alertdialog"`, no auditability, and they block the main
// thread. RED before `use-server-confirms.tsx` existed.
import { describe, it, expect, vi, afterEach } from "vitest"
import { render, cleanup, screen, waitFor } from "@testing-library/react"
import { setupUser } from "@/tests/support/user-event"

import { useServerConfirms } from "@/components/server/use-server-confirms"

const server = {
  id: "s1",
  name: "prod",
  host: "10.0.0.1",
  port: 8080,
  status: "connected" as const,
}

function Harness({
  onRemove,
  onClear,
}: {
  onRemove: (id: string) => void
  onClear: () => void
}) {
  const { requestRemove, requestClear, confirmModals } = useServerConfirms({
    removeServer: onRemove,
    clearPersistedData: onClear,
    serverCount: 3,
  })
  return (
    <>
      <button onClick={() => requestRemove(server)}>remove</button>
      <button onClick={() => requestClear()}>clear</button>
      {confirmModals}
    </>
  )
}

afterEach(() => cleanup())

describe("useServerConfirms", () => {
  it("removing a server asks first and names it", async () => {
    const onRemove = vi.fn()
    render(<Harness onRemove={onRemove} onClear={vi.fn()} />)

    await setupUser().click(screen.getByText("remove"))
    expect(screen.getByRole("alertdialog")).toBeTruthy()
    expect(screen.getByText(/prod/)).toBeTruthy()
    expect(onRemove).not.toHaveBeenCalled()

    await setupUser().click(
      screen.getByRole("button", { name: /Remove server/ }),
    )
    await waitFor(() => expect(onRemove).toHaveBeenCalledWith("s1"))
  })

  it("cancelling a remove does nothing", async () => {
    const onRemove = vi.fn()
    render(<Harness onRemove={onRemove} onClear={vi.fn()} />)
    await setupUser().click(screen.getByText("remove"))
    await setupUser().click(screen.getByRole("button", { name: "Cancel" }))
    expect(onRemove).not.toHaveBeenCalled()
  })

  it("clearing all configs names the count", async () => {
    const onClear = vi.fn()
    render(<Harness onRemove={vi.fn()} onClear={onClear} />)
    await setupUser().click(screen.getByText("clear"))
    expect(screen.getByText(/3 saved server/)).toBeTruthy()
    await setupUser().click(
      screen.getByRole("button", { name: /Clear all/ }),
    )
    await waitFor(() => expect(onClear).toHaveBeenCalled())
  })

  it("renders nothing until something is requested", () => {
    render(<Harness onRemove={vi.fn()} onClear={vi.fn()} />)
    expect(screen.queryByRole("alertdialog")).toBeNull()
  })
})
