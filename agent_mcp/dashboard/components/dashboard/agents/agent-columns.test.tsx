// @vitest-environment jsdom
//
// Unit tests for the Agents column spec.
//
// The pre-scaffold `<CompactAgentRow>` encoded a real per-agent action
// policy — Admin is never editable/terminable, live agents get
// Disconnect, paused ones get Reconnect, terminated ones get
// Restore/Purge — with no way to assert it short of driving the whole
// page. `useAgentColumns` is now a seam, so the policy gets pinned here.
import { describe, it, expect, vi, afterEach } from "vitest"
import { render, cleanup, within } from "@testing-library/react"

vi.mock("@/lib/stores/data-store", () => ({
  useDataStore: (selector?: (s: unknown) => unknown) => {
    const state = { data: null, getAgentTasks: () => [] }
    return selector ? selector(state) : state
  },
}))

import { useAgentColumns } from "@/components/dashboard/agents/agent-columns"
import { ResponsiveDataTable } from "@/components/dashboard/shared/responsive-data-table"
import type { Agent } from "@/lib/api"

afterEach(() => cleanup())

const noop = () => {}
const handlers = {
  onTerminate: noop,
  onRestore: noop,
  onPurge: noop,
  openView: noop,
  onEdit: noop,
  onTaskClick: noop,
  onSendDirective: noop,
  onDisconnect: noop,
  onReconnect: noop,
}

function Harness({ agents }: { agents: Agent[] }) {
  const columns = useAgentColumns(handlers)
  return (
    <ResponsiveDataTable
      columns={columns}
      rows={agents}
      getRowId={(a) => a.agent_id}
    />
  )
}

const mk = (over: Partial<Agent>): Agent =>
  ({
    agent_id: "worker-1",
    status: "created",
    created_at: "2020-01-01T00:00:00Z",
    ...over,
  }) as unknown as Agent

function row(agentId: string): HTMLElement {
  // Assert against the desktop table only — the mobile twin renders the
  // same rows and would otherwise double every query.
  const table = document.querySelector("table")!
  return within(table).getByText(agentId).closest("tr")!
}

const titles = (el: HTMLElement) =>
  within(el)
    .getAllByRole("button")
    .map((b) => b.getAttribute("title") ?? "")

describe("useAgentColumns", () => {
  it("renders the five parity columns", () => {
    render(<Harness agents={[mk({})]} />)
    const table = document.querySelector("table")!
    for (const header of ["Agent", "Status", "Tasks", "Token", "Actions"]) {
      expect(within(table).getByText(header)).toBeTruthy()
    }
  })

  it("collapses `pending` into the OFFLINE badge but keeps its tooltip", () => {
    render(<Harness agents={[mk({})]} />)
    const r = row("worker-1")
    const badge = within(r).getByText("OFFLINE")
    expect(badge.getAttribute("title")).toMatch(/no MCP session yet/i)
  })

  it("labels a live agent ONLINE and a terminated one TERMINATED", () => {
    render(
      <Harness
        agents={[
          mk({ agent_id: "live", online: true } as Partial<Agent>),
          mk({ agent_id: "dead", status: "terminated" } as Partial<Agent>),
        ]}
      />,
    )
    expect(within(row("live")).getByText("ONLINE")).toBeTruthy()
    expect(within(row("dead")).getByText("TERMINATED")).toBeTruthy()
  })

  it("gives a live worker view / directive / edit / disconnect / terminate", () => {
    render(<Harness agents={[mk({})]} />)
    const t = titles(row("worker-1"))
    expect(t.some((x) => x.startsWith("View details"))).toBe(true)
    expect(t.some((x) => x.startsWith("Send directive"))).toBe(true)
    expect(t.some((x) => x.startsWith("Edit agent"))).toBe(true)
    expect(t.some((x) => x.startsWith("Disconnect"))).toBe(true)
    expect(t.some((x) => x.startsWith("Terminate"))).toBe(true)
    expect(t.some((x) => x.startsWith("Restore"))).toBe(false)
  })

  it("swaps Disconnect for Reconnect (and flags PAUSED) when the loop is off", () => {
    render(<Harness agents={[mk({ auto_event_loop: false } as Partial<Agent>)]} />)
    const r = row("worker-1")
    expect(within(r).getByText("PAUSED")).toBeTruthy()
    const t = titles(r)
    expect(t.some((x) => x.startsWith("Reconnect"))).toBe(true)
    expect(t.some((x) => x.startsWith("Disconnect"))).toBe(false)
  })

  it("offers Restore + Purge (never Terminate) on a terminated row", () => {
    render(<Harness agents={[mk({ status: "terminated" } as Partial<Agent>)]} />)
    const t = titles(row("worker-1"))
    expect(t).toContain("Restore")
    expect(t).toContain("Purge")
    expect(t.some((x) => x.startsWith("Terminate"))).toBe(false)
    expect(t.some((x) => x.startsWith("Send directive"))).toBe(false)
  })

  it("never offers destructive or edit actions on the Admin pseudo-agent", () => {
    render(<Harness agents={[mk({ agent_id: "Admin" })]} />)
    const t = titles(row("Admin"))
    expect(t).toEqual(["View details"])
  })

  it("keeps action cells hover-revealed so the row stays uncluttered", () => {
    render(<Harness agents={[mk({})]} />)
    const cell = within(row("worker-1"))
      .getByTitle("View details")
      .closest("div")!
    expect(cell.className).toContain("opacity-0")
    expect(cell.className).toContain("group-hover:opacity-100")
  })
})
