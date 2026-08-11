// @vitest-environment jsdom
//
// Unit tests for the shared submit → loading → toast → close/stay-open
// state machine (Wave 5, CD-2). These pin the contract every extracted
// form dialog relies on: success closes + toasts; failure stays OPEN,
// toasts the error, and exposes it inline; and a double-fire runs the
// mutation exactly once.
import { describe, it, expect, vi, afterEach } from "vitest"
import { act, renderHook, waitFor } from "@testing-library/react"

const toastSuccess = vi.fn()
const toastError = vi.fn()
vi.mock("@/components/ui/toast", () => ({
  toastSuccess: (...a: unknown[]) => toastSuccess(...a),
  toastError: (...a: unknown[]) => toastError(...a),
}))

import { useAsyncSubmit } from "@/components/dashboard/shared/use-async-submit"

afterEach(() => {
  toastSuccess.mockReset()
  toastError.mockReset()
})

describe("useAsyncSubmit", () => {
  it("toasts, calls onSuccess, and closes on a successful submit", async () => {
    const onSuccess = vi.fn()
    const onOpenChange = vi.fn()
    const { result } = renderHook(() =>
      useAsyncSubmit<string>({
        onSubmit: async () => "ok",
        successMessage: (r) => `sent ${r}`,
        onSuccess,
        onOpenChange,
      }),
    )

    await act(async () => {
      await result.current.submit()
    })

    expect(toastSuccess).toHaveBeenCalledWith("sent ok")
    expect(onSuccess).toHaveBeenCalledWith("ok")
    expect(onOpenChange).toHaveBeenCalledWith(false)
    expect(result.current.error).toBeNull()
    expect(result.current.submitting).toBe(false)
  })

  it("stays OPEN, toasts the error, and exposes it inline on failure", async () => {
    const onOpenChange = vi.fn()
    const onError = vi.fn()
    const { result } = renderHook(() =>
      useAsyncSubmit({
        onSubmit: async () => {
          throw new Error("boom")
        },
        errorMessage: "could not save",
        onError,
        onOpenChange,
      }),
    )

    await act(async () => {
      await result.current.submit()
    })

    // Never closes on error.
    expect(onOpenChange).not.toHaveBeenCalled()
    expect(toastError).toHaveBeenCalledTimes(1)
    expect(onError).toHaveBeenCalled()
    expect(result.current.error).toBe("boom")
    expect(result.current.submitting).toBe(false)
    expect(toastSuccess).not.toHaveBeenCalled()
  })

  it("honours closeOnSuccess=false (result pane instead of closing)", async () => {
    const onOpenChange = vi.fn()
    const { result } = renderHook(() =>
      useAsyncSubmit({
        onSubmit: async () => undefined,
        onOpenChange,
        closeOnSuccess: false,
      }),
    )

    await act(async () => {
      await result.current.submit()
    })

    expect(onOpenChange).not.toHaveBeenCalled()
  })

  it("clears a prior error when a later submit is retried", async () => {
    let shouldFail = true
    const { result } = renderHook(() =>
      useAsyncSubmit({
        onSubmit: async () => {
          if (shouldFail) throw new Error("boom")
        },
      }),
    )

    await act(async () => {
      await result.current.submit()
    })
    expect(result.current.error).toBe("boom")

    shouldFail = false
    await act(async () => {
      await result.current.submit()
    })
    expect(result.current.error).toBeNull()
  })

  it("drops a concurrent double-submit (mutation runs once)", async () => {
    let resolve!: () => void
    const gate = new Promise<void>((r) => {
      resolve = r
    })
    const onSubmit = vi.fn(async () => {
      await gate
    })
    const { result } = renderHook(() =>
      useAsyncSubmit({ onSubmit }),
    )

    await act(async () => {
      // Fire twice before the first resolves.
      const a = result.current.submit()
      const b = result.current.submit()
      resolve()
      await Promise.all([a, b])
    })

    expect(onSubmit).toHaveBeenCalledTimes(1)
  })

  it("flips submitting true while the mutation is in flight", async () => {
    let resolve!: () => void
    const gate = new Promise<void>((r) => {
      resolve = r
    })
    const { result } = renderHook(() =>
      useAsyncSubmit({ onSubmit: async () => gate }),
    )

    let pending: Promise<void>
    act(() => {
      pending = result.current.submit()
    })
    await waitFor(() => expect(result.current.submitting).toBe(true))

    await act(async () => {
      resolve()
      await pending
    })
    expect(result.current.submitting).toBe(false)
  })
})
