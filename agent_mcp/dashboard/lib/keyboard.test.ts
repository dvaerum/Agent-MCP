import { describe, it, expect, vi } from "vitest"
import type { KeyboardEvent } from "react"
import { onEnterSubmit } from "@/lib/keyboard"

// Build a minimal synthetic KeyboardEvent good enough for onEnterSubmit.
function makeEvent(opts: {
  key: string
  isComposing?: boolean
  keyCode?: number
}): { event: KeyboardEvent; preventDefault: ReturnType<typeof vi.fn> } {
  const preventDefault = vi.fn()
  const event = {
    key: opts.key,
    keyCode: opts.keyCode ?? 0,
    preventDefault,
    nativeEvent: { isComposing: opts.isComposing ?? false },
  } as unknown as KeyboardEvent
  return { event, preventDefault }
}

describe("onEnterSubmit", () => {
  it("fires the action and preventDefault on Enter when ready", () => {
    const action = vi.fn()
    const { event, preventDefault } = makeEvent({ key: "Enter" })
    onEnterSubmit(true, action)(event)
    expect(action).toHaveBeenCalledTimes(1)
    expect(preventDefault).toHaveBeenCalledTimes(1)
  })

  it("does not fire when not ready", () => {
    const action = vi.fn()
    const { event, preventDefault } = makeEvent({ key: "Enter" })
    onEnterSubmit(false, action)(event)
    expect(action).not.toHaveBeenCalled()
    expect(preventDefault).not.toHaveBeenCalled()
  })

  it("ignores non-Enter keys", () => {
    const action = vi.fn()
    const { event } = makeEvent({ key: "a" })
    onEnterSubmit(true, action)(event)
    expect(action).not.toHaveBeenCalled()
  })

  it("ignores Enter during IME composition (isComposing)", () => {
    const action = vi.fn()
    const { event } = makeEvent({ key: "Enter", isComposing: true })
    onEnterSubmit(true, action)(event)
    expect(action).not.toHaveBeenCalled()
  })

  it("ignores Enter during IME composition (keyCode 229)", () => {
    const action = vi.fn()
    const { event } = makeEvent({ key: "Enter", keyCode: 229 })
    onEnterSubmit(true, action)(event)
    expect(action).not.toHaveBeenCalled()
  })
})
