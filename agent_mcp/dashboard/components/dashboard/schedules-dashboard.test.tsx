// @vitest-environment jsdom
//
// Regression coverage for two Schedules-page bugs fixed together:
//
//   Bug 1 — the per-row "Send" button called `openDirective(s.agent_id)`,
//   opening the shared `<SendDirectiveModal>` with a BLANK textarea. The
//   operator had to retype the schedule's own prompt to poke it now. Fixed
//   to call `apiClient.pokeAgent` directly with the row's own prompt, no
//   modal involved.
//
//   Bug 2 — the "Next fire" column was fed by a hand-rolled one-shot
//   `useState` + `useEffect` fetch with no polling/SSE invalidation, so it
//   froze at whatever it read on mount. Migrated onto `useSchedulesQuery`
//   (TanStack Query) — mirrors `messages-dashboard.test.tsx`'s mock
//   recipe for a query-backed dashboard page.
import { describe, it, expect, vi, afterEach, beforeEach } from "vitest"
import { render, cleanup, screen, fireEvent, waitFor, act } from "@testing-library/react"
import { setMatchMedia } from "@/tests/support/match-media"

const schedule = {
  directive_id: "sd_1",
  agent_id: "alice",
  prompt: "do it",
  interval_seconds: 60,
  next_due_at: "2026-01-15T12:00:15.000Z",
  enabled: true,
  status: "active",
  until_at: null,
  max_runs: null,
  run_count: 0,
  created_at: "2026-01-15T11:00:00.000Z",
  created_by: "op",
  updated_at: null,
  updated_by: null,
}

// Mutable so individual tests can drive the query into other shapes.
// Shape mirrors the subset of `useSchedulesQuery`'s result the page
// reads (see `tasks-query-cache.test.tsx` / `messages-dashboard.test.tsx`
// for the same convention).
const query = {
  data: [schedule] as unknown[],
  isLoading: false,
  isFetching: false,
  error: null as Error | null,
  refetch: vi.fn(),
  dataUpdatedAt: Date.now(),
}

const pokeAgent = vi.fn()

vi.mock("@/lib/queries/schedules", () => ({
  useSchedulesQuery: () => query,
}))
vi.mock("@/lib/stores/server-store", () => ({
  useServerStore: () => ({
    servers: [{ id: "s1", name: "proj", status: "connected" }],
    activeServerId: "s1",
  }),
}))
vi.mock("@/lib/queries/all-data", () => ({
  useActiveAgents: () => [],
}))
vi.mock("@/components/ui/toast", () => ({
  toastSuccess: vi.fn(),
  toastError: vi.fn(),
}))
vi.mock("@/lib/api", () => ({
  apiClient: {
    pokeAgent: (...args: unknown[]) => pokeAgent(...args),
    getSettingsSchema: vi.fn().mockResolvedValue({ schema: [] }),
    getSchedules: vi.fn(),
    updateSchedule: vi.fn(),
    deleteSchedule: vi.fn(),
    createSchedule: vi.fn(),
  },
  ApiError: class ApiError extends Error {},
}))
// Stub the shared modal so this test verifies ONLY schedules-dashboard's
// own open/no-open decision, not the modal's internals (already covered
// by `send-directive-action.test.ts`'s source-grep + the modal's own
// behavior is out of scope here).
vi.mock("@/components/dashboard/shared/send-directive-modal", () => ({
  SendDirectiveModal: ({ open }: { open: boolean }) =>
    open ? <div data-testid="send-directive-modal-stub" /> : null,
}))

import { SchedulesDashboard } from "@/components/dashboard/schedules-dashboard"
import { toastSuccess } from "@/components/ui/toast"

beforeEach(() => setMatchMedia(false))
afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  query.data = [schedule]
  query.error = null
  query.isLoading = false
  query.isFetching = false
})

describe("<SchedulesDashboard> per-row send (Bug 1)", () => {
  it("renders a row for the fetched schedule (Bug 2 wiring pin)", () => {
    render(<SchedulesDashboard />)
    expect(screen.getByText("alice")).toBeTruthy()
    expect(screen.getByText("do it")).toBeTruthy()
  })

  it("clicking the row's send button fires the row's own prompt directly, no modal", async () => {
    pokeAgent.mockResolvedValue({ delivered: true, poke_id: "p1", agent_id: "alice", success: true, message: "" })
    render(<SchedulesDashboard />)
    fireEvent.click(screen.getByTestId("poke-sd_1"))
    await waitFor(() => expect(pokeAgent).toHaveBeenCalledWith("alice", { prompt: "do it" }))
    expect(screen.queryByTestId("send-directive-modal-stub")).toBeNull()
  })

  it("delivered response toasts 'Delivered to'", async () => {
    pokeAgent.mockResolvedValue({ delivered: true, poke_id: "p1", agent_id: "alice", success: true, message: "" })
    render(<SchedulesDashboard />)
    fireEvent.click(screen.getByTestId("poke-sd_1"))
    await waitFor(() =>
      expect(toastSuccess).toHaveBeenCalledWith(
        expect.stringContaining("Delivered to"),
        "Directive delivered",
      ),
    )
  })

  it("queued response (delivered: false) toasts 'Queued for'", async () => {
    pokeAgent.mockResolvedValue({ delivered: false, poke_id: "p1", agent_id: "alice", success: true, message: "" })
    render(<SchedulesDashboard />)
    fireEvent.click(screen.getByTestId("poke-sd_1"))
    await waitFor(() =>
      expect(toastSuccess).toHaveBeenCalledWith(
        expect.stringContaining("Queued for"),
        "Directive queued",
      ),
    )
  })

  it("the standalone 'Send directive' button DOES open the shared modal", () => {
    render(<SchedulesDashboard />)
    expect(screen.queryByTestId("send-directive-modal-stub")).toBeNull()
    fireEvent.click(screen.getByTestId("send-directive-btn"))
    expect(screen.getByTestId("send-directive-modal-stub")).toBeTruthy()
  })
})

describe("<SchedulesDashboard> live 'Next fire' tick (Bug 2)", () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date("2026-01-15T12:00:00.000Z"))
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it("re-renders the 'Next fire' cell every ~15s without a new fetch", () => {
    // next_due_at is 15s out at mount → within the "now" (<30s) branch.
    render(<SchedulesDashboard />)
    expect(screen.getByText("now")).toBeTruthy()

    // Advance past the due time without changing the underlying data —
    // the 15s tick must force the cell to recompute against a fresh Date.
    act(() => {
      vi.advanceTimersByTime(15_000)
      vi.advanceTimersByTime(15_000)
    })
    expect(screen.getByText("overdue")).toBeTruthy()
    expect(query.refetch).not.toHaveBeenCalled()
  })
})
