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

import {
  AGENTS_TABLE_CLASS,
  useAgentColumns,
} from "@/components/dashboard/agents/agent-columns"
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
      tableClassName={AGENTS_TABLE_CLASS}
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

/**
 * Cell containment (fix/agents-status-badge-overflow).
 *
 * Measured live at 1280×800 on a 6-agent project: the STATUS cell laid
 * its badges out in a NON-wrapping flex row (`flex items-center gap-2`,
 * every `<Badge>` carrying `shrink-0 whitespace-nowrap` from
 * `badgeVariants`) inside a `table-fixed` `w-32` column. Intrinsic
 * content was 157px against a 112px content box, so the second badge
 * started 29px past the cell's right edge and painted ON TOP of the
 * TASKS column's "No active task". A row carrying WORKING *and*
 * WAITING overflowed by 121px. The ACTIONS cell had the same defect —
 * five 28px icon buttons + four 4px gaps = 156px in a `w-36` column's
 * 128px box, 20px past its own edge.
 *
 * jsdom has no layout engine, so these tests pin the STRUCTURE that
 * makes the overflow impossible — the badge row wraps, no badge can be
 * wider than the cell, the column reserves room for the common
 * two-badge case — not the pixels. The geometry itself was verified in
 * Firefox against the live project (see the PR body); a future change
 * that re-narrows the column or drops `flex-wrap` will flip these tests
 * but only a browser can re-measure the result.
 */
describe("useAgentColumns cell containment", () => {
  // The realistic worst case seen live: presence + delivery transport +
  // an in-flight wait_for_events long-poll, and no active task (so the
  // TASKS cell underneath is text the badges would paint over).
  const busy = () =>
    mk({
      agent_id: "pikvm-mcp-server@georgs-mac-mini",
      online: true,
      transport_status: "working",
      wait_for_events_in_flight: true,
    } as Partial<Agent>)

  const statusRow = (): HTMLElement => {
    render(<Harness agents={[busy()]} />)
    const r = row("pikvm-mcp-server@georgs-mac-mini")
    return within(r).getByText("ONLINE").parentElement!
  }

  const head = (label: string): HTMLElement =>
    within(document.querySelector("table")!).getByText(label)

  it("wraps the STATUS badges onto a second line instead of overflowing", () => {
    const container = statusRow()
    // All three badges are still rendered — degrading must not hide
    // information, only re-flow it.
    expect(within(container).getByText("ONLINE")).toBeTruthy()
    expect(within(container).getByText("WORKING")).toBeTruthy()
    expect(within(container).getByText("WAITING")).toBeTruthy()
    // `flex-wrap` is the whole fix: <Badge> is `shrink-0`, so a
    // non-wrapping row has no way to stay inside a fixed-width cell.
    expect(container.className).toContain("flex-wrap")
  })

  it("caps each STATUS badge at the cell width so one long label clips", () => {
    const container = statusRow()
    // A single badge wider than the whole column would still escape a
    // wrapping row; `max-w-full` + the badge's own `overflow-hidden`
    // clips it inside the cell instead.
    for (const label of ["ONLINE", "WORKING", "WAITING"]) {
      expect(within(container).getByText(label).className).toContain("max-w-full")
    }
  })

  it("reserves STATUS column width for the common two-badge row", () => {
    render(<Harness agents={[busy()]} />)
    // w-32 (128px) could not hold even ONE badge pair, so every live
    // row wrapped. w-44 (176px) holds two; three still wrap, by design.
    expect(head("Status").className).toContain("w-44")
  })

  it("wraps the ACTIONS toolbar and gives it room for five buttons", () => {
    render(<Harness agents={[busy()]} />)
    const r = row("pikvm-mcp-server@georgs-mac-mini")
    const toolbar = within(r).getByTitle("View details").closest("div")!
    expect(toolbar.className).toContain("flex-wrap")
    // w-36 (128px content box) could not hold the five-button live
    // toolbar (156px). The terminated variant (text Restore/Purge
    // buttons) still exceeds w-44 and relies on flex-wrap above.
    expect(head("Actions").className).toContain("w-44")
  })

  it("lets the TOKEN code shrink so the copied flash cannot overflow", () => {
    render(
      <Harness
        agents={[mk({ auth_token: "d0da58f9cafebabe" } as Partial<Agent>)]}
      />,
    )
    const code = within(row("worker-1")).getByText("d0da58f9...")
    // A flex item defaults to `min-width:auto` — without `min-w-0` the
    // monospace token refuses to shrink and the transient "copied"
    // span pushes the row past the cell edge.
    expect(code.className).toContain("min-w-0")
  })
})

/**
 * Column sizing model (refactor/agents-table-column-sizing).
 *
 * PR #595 bought STATUS/ACTIONS containment by trimming TASKS, which
 * squeezed the elastic AGENT column from 277px to 203px at 1280 — and
 * AGENT is the column holding the row's primary key. Rather than keep
 * trading pixels, the widths were re-derived from a live measurement
 * (Firefox, 6-agent project, `getBoundingClientRect()` per cell):
 *
 *   STATUS   content 160px; ONLINE 70.2 + gap 4 + WORKING 84.9 = 159.1
 *            → `w-44` is the exact minimum for the common pair.
 *   ACTIONS  5 × 28px buttons + 4 × 4px gaps = 156px → `w-44` likewise.
 *   TOKEN    8-char elision (≤65px) + gap 8 + 24px button = 97px, so
 *            `w-36` (128px box) reserved 31px nobody used → `w-32`.
 *   TASKS    the "{n} assigned, {m} contributed" sub-line is ~132px, so
 *            the 224px reserve was ~90px of slack at the narrow end
 *            while never growing at the wide end → `max(13rem, 18%)`:
 *            a floor for 1280 and a proportional share above ~1156px.
 *
 * Three things this file can pin and one it cannot:
 *   * the per-column width classes (below) — the measured contract;
 *   * that AGENT stays the elastic column (no width class at all), so
 *     it absorbs the slack the trims freed;
 *   * that the table carries a `min-w-*` floor, without which
 *     `table-fixed` squeezes the elastic column to ZERO instead of
 *     letting the wrapper scroll (measured on `main` at 1024×800:
 *     AGENT = 0px wide and 36 elements painting outside their own
 *     `<td>` — the very defect #595 fixed, one breakpoint down);
 *   * NOT the resulting pixels. jsdom has no layout engine. The
 *     geometry was verified in Firefox at 1280 / 1440 / 1920 against
 *     the live project (numbers in the PR body).
 */
describe("useAgentColumns column sizing", () => {
  const head = (label: string): HTMLElement =>
    within(document.querySelector("table")!).getByText(label)

  it("keeps AGENT elastic so it absorbs every column's leftover", () => {
    render(<Harness agents={[mk({})]} />)
    // A width class here would hand AGENT a fixed reserve like all the
    // others and leave NO column to take up the slack.
    expect(head("Agent").className).not.toMatch(/(^|\s)w-/)
  })

  it("pins the measured content minimum of every fixed column", () => {
    render(<Harness agents={[mk({})]} />)
    expect(head("Status").className).toContain("w-44")
    expect(head("Actions").className).toContain("w-44")
    // Trimmed from w-36: the cell's intrinsic content is 97px, so the
    // 128px content box was 31px of reserve that only starved AGENT.
    expect(head("Token").className).toContain("w-32")
  })

  it("sizes TASKS by its sub-line, in px rather than a proportion", () => {
    render(<Harness agents={[mk({})]} />)
    // Proportional sizing was tried here and measured to be broken:
    // fixed table layout drops `max(13rem,18%)` and falls back to the
    // auto split (221.4px instead of 208px on a 922.8px table), and a
    // bare percentage has no floor. See the note on the column.
    expect(head("Tasks").className).toContain("w-52")
    expect(head("Tasks").className).not.toMatch(/w-\[max\(/)
  })

  it("lets the AGENT cell wrap, which shadcn's nowrap <td> otherwise forbids", () => {
    render(<Harness agents={[mk({})]} />)
    const cell = within(document.querySelector("table")!)
      .getByText("worker-1")
      .closest("td")!
    // Without this the id's `break-words` is inert: <TableCell> ships
    // `whitespace-nowrap`, so the id stays on one line however much
    // room the column has, and truncation is the only option.
    expect(cell.className).toContain("whitespace-normal")
  })

  it("floors the table so table-fixed cannot squeeze a column to zero", () => {
    render(<Harness agents={[mk({})]} />)
    const table = document.querySelector("table")!
    expect(table.className).toContain("table-fixed")
    // Without this, a container narrower than the sum of the fixed
    // columns leaves the elastic column 0px and its content paints
    // over its neighbour. With it, the `overflow-x-auto` wrapper
    // scrolls instead — every column keeps its minimum at any width.
    expect(table.className).toContain("min-w-[56rem]")
  })
})

/**
 * Agent-id legibility.
 *
 * The id is the primary key an operator scans by, and for this fleet
 * the DISTINGUISHING part is the host suffix — `…@georgs-mac-mini` vs
 * `…@nixos-developer-system` — which is exactly what a single truncated
 * line cuts off. At 1280 the id line is a 137px box against a 293px
 * id: 17 of 39 characters.
 *
 * The cell was already two lines tall; the second one spent itself on
 * `#{agent_id.slice(-6)}` — the last six characters of the string on
 * the line above. Pure redundancy, and ambiguous on the live fleet
 * (`#system` for two different agents). Reusing that line for the rest
 * of the id doubles the visible characters at zero layout cost.
 */
describe("useAgentColumns agent id", () => {
  const LONG = "pikvm-mcp-server@nixos-developer-system"

  const idEl = (): HTMLElement => {
    render(<Harness agents={[mk({ agent_id: LONG })]} />)
    return within(document.querySelector("table")!).getByText(LONG)
  }

  it("drops the short-id line that only repeated the id's own tail", () => {
    const el = idEl()
    expect(
      within(el.closest("td")!).queryByText(`#${LONG.slice(-6)}`),
    ).toBeNull()
  })

  it("lets the id use both lines and break inside an unspaced id", () => {
    const el = idEl()
    // `line-clamp-2` caps the height (a pathological id must not grow
    // the row) AND brings `overflow:hidden`, so nothing escapes the
    // cell. Agent ids have no spaces, so without `break-words` the
    // second line would never be reached.
    expect(el.className).toContain("line-clamp-2")
    expect(el.className).toContain("break-words")
    expect(el.className).not.toContain("truncate")
  })

  it("keeps the full id reachable where it still clips", () => {
    expect(idEl().getAttribute("title")).toBe(LONG)
  })
})
