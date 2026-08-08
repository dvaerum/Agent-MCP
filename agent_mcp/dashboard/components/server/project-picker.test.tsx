// @vitest-environment jsdom
//
// Layout containment for the app-shell project chip
// (fix/agents-status-badge-overflow).
//
// Measured live at 390×844: the chip rendered its trigger as an
// `inline-flex` button with `min-w-[200px]` and `whitespace-nowrap`
// inside the header's `flex-1 min-w-0` slot (173px). Nothing capped
// it, so the 31-character live project name sized the button to 301px
// — 128px past its slot and 64px past the viewport — and the theme
// toggle ended up INSIDE the chip's box. The inner `truncate` span
// never engaged because its wrapper had no `min-w-0` and the button
// itself was never width-constrained.
//
// jsdom cannot measure any of that, so these tests pin the structure:
// the trigger is capped at its slot, the mobile min-width floor is
// gone, and the label wrapper can shrink so `truncate` engages. The
// geometry was verified in Firefox at 390×844 (chip 153..326, theme
// toggle 338..374, no overlap, no off-screen box).
import { describe, it, expect, vi, afterEach } from "vitest"
import { render, cleanup, screen } from "@testing-library/react"

const state = {
  envelope: null as unknown,
  loading: false,
  error: null as string | null,
  fetchOverview: vi.fn(),
}

vi.mock("@/lib/stores/projects-store", () => ({
  useProjectsStore: () => state,
}))

import { ProjectPicker } from "@/components/server/project-picker"

afterEach(() => {
  cleanup()
  state.envelope = null
})

const LONG = "pikvm-on-nixos-with-mcp-support"

/** The chip's trigger, whichever tenancy branch rendered it. */
function chip(): HTMLElement {
  return screen.getByRole("button")
}

describe("<ProjectPicker> chip containment", () => {
  it("caps the multi-tenant chip at its header slot", () => {
    state.envelope = { multi_tenant: true, projects: [{ name: LONG }] }
    render(<ProjectPicker />)
    const b = chip()
    expect(b.className).toContain("max-w-full")
    // The 200px floor is a DESKTOP affordance. Unqualified it beats
    // `max-w-full` (min-width wins over max-width) and the chip
    // overflows a 390px phone header regardless.
    expect(b.className).toContain("sm:min-w-[200px]")
    expect(b.className).not.toMatch(/(^|\s)min-w-\[200px\]/)
  })

  it("lets the chip label shrink so its truncate engages", () => {
    state.envelope = { multi_tenant: true, projects: [{ name: LONG }] }
    render(<ProjectPicker />)
    const label = screen.getByText("Select Project")
    expect(label.className).toContain("truncate")
    // `truncate` is inert while the wrapper sizes to max-content.
    expect(label.parentElement!.className).toContain("min-w-0")
  })

  it("keeps the full project name reachable on hover when truncated", () => {
    state.envelope = { multi_tenant: true, projects: [{ name: LONG }] }
    render(<ProjectPicker />)
    expect(chip().getAttribute("title")).toBe("Select Project")
  })

  it("applies the same containment to the single-tenant chip", () => {
    state.envelope = {
      multi_tenant: false,
      single_tenant_name: LONG,
      projects: [{ name: LONG }],
    }
    render(<ProjectPicker />)
    const b = chip()
    expect(b.className).toContain("max-w-full")
    expect(b.className).toContain("sm:min-w-[200px]")
    expect(b.className).not.toMatch(/(^|\s)min-w-\[200px\]/)
    expect(screen.getByText(LONG).parentElement!.className).toContain("min-w-0")
  })
})
