// @vitest-environment jsdom
//
// Unit tests for the shared <StatsCard>. This card was copy-pasted 4×
// (agents/tasks/memories/messages) and had already drifted — the
// down-trend colour split between text-destructive and text-orange-500,
// and only the tasks copy was memoized. These tests pin the reconciled
// single version: typed icon, memoized (displayName), semantic
// destructive down-trend, emerald up-trend, muted neutral.
import { describe, it, expect, afterEach } from "vitest"
import { render, cleanup, screen } from "@testing-library/react"
import { Database } from "lucide-react"

import { StatsCard } from "@/components/dashboard/shared/stats-card"

afterEach(() => cleanup())

describe("<StatsCard>", () => {
  it("renders label and value", () => {
    render(<StatsCard icon={Database} label="Total" value={42} />)
    expect(screen.getByText("Total")).toBeTruthy()
    expect(screen.getByText("42")).toBeTruthy()
  })

  it("renders the change line only when `change` is provided", () => {
    const { rerender } = render(
      <StatsCard icon={Database} label="Total" value={0} />,
    )
    expect(screen.queryByText("no change")).toBeNull()
    rerender(
      <StatsCard icon={Database} label="Total" value={0} change="no change" />,
    )
    expect(screen.getByText("no change")).toBeTruthy()
  })

  it("uses emerald for up, destructive for down, muted for neutral", () => {
    render(
      <StatsCard
        icon={Database}
        label="Up"
        value={1}
        change="rising"
        trend="up"
      />,
    )
    expect(screen.getByText("rising").className).toContain("text-emerald-500")

    cleanup()
    render(
      <StatsCard
        icon={Database}
        label="Down"
        value={1}
        change="falling"
        trend="down"
      />,
    )
    // Reconciled to the semantic token (matches agents/memories/messages);
    // the tasks copy's text-orange-500 was the drifted odd-one-out.
    expect(screen.getByText("falling").className).toContain("text-destructive")

    cleanup()
    render(
      <StatsCard
        icon={Database}
        label="Neutral"
        value={1}
        change="steady"
        trend="neutral"
      />,
    )
    expect(screen.getByText("steady").className).toContain(
      "text-muted-foreground",
    )
  })

  it("is memoized with a stable displayName", () => {
    expect(StatsCard.displayName).toBe("StatsCard")
  })
})
