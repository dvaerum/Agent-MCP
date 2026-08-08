// @vitest-environment jsdom
//
// The theme toggle is the header's right-most control — the one the
// project chip used to overlap on a phone (see
// components/server/project-picker.test.tsx). With the chip contained
// it is fully visible again; this pins the other half of "visible AND
// tappable": the same 40px mobile hit target the header's hamburger
// already uses (components/layout/header.tsx), shrinking to the shadcn
// 36px default from `sm` up.
import { describe, it, expect, afterEach } from "vitest"
import { render, cleanup, screen } from "@testing-library/react"

import { ThemeToggle } from "@/components/layout/theme-toggle"

afterEach(() => cleanup())

describe("<ThemeToggle>", () => {
  it("clears the 40px mobile hit-target floor", () => {
    render(<ThemeToggle />)
    const btn = screen.getByRole("button", { name: /toggle theme/i })
    expect(btn.className).toContain("h-10")
    expect(btn.className).toContain("w-10")
    expect(btn.className).toContain("sm:h-9")
    expect(btn.className).toContain("sm:w-9")
  })

  it("never shrinks out of the header row", () => {
    render(<ThemeToggle />)
    expect(
      screen.getByRole("button", { name: /toggle theme/i }).className,
    ).toContain("shrink-0")
  })
})
