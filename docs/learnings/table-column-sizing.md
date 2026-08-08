# Sizing columns in a `table-fixed` data table

Notes from re-deriving the Agents table's column widths
(`agent_mcp/dashboard/components/dashboard/agents/agent-columns.tsx`)
after two rounds of trading pixels between columns. All numbers were
measured in Firefox against the live 6-agent project via
`getBoundingClientRect()`, not estimated.

## `table-fixed` is not optional here, and it is only half a decision

Auto layout sizes columns from content, so one pathological value
resizes the whole table: a 5000-char agent name stretched this table to
**40,660px** and pushed four of five columns off-screen. `table-fixed`
caps that (40,660px → 923px) and, as a bonus, keeps the widths
data-independent so columns don't shuffle as rows come and go.

The half that's easy to forget: under fixed layout the columns with no
specified width absorb the leftover, and when the container is narrower
than the sum of the specified ones **that leftover goes negative**. The
browser clamps the column to `0px` and its content paints over the next
one. Measured on this table at a 1024×800 viewport (677px container):
the elastic AGENT column was 0px wide with 36 elements outside their own
`<td>`.

The `overflow-x-auto` wrapper does not save you — the table never
exceeds the container, so there is nothing to scroll. A `min-w-*` on the
`<table>` at least as large as (fixed columns + a floor for the elastic
one) is what makes the wrapper engage. Keep `min-w` and the column
widths in the same module; they are one decision.

## Fixed table layout silently drops `max()` mixing a length and a `%`

The appealing model for these tables is `minmax`: a floor so a column
never drops below its content minimum, a proportional share so the
columns divide slack on a wide screen instead of holding fixed reserves.
On a table column, it does not work. Probed on the live table (table
width 922.8px), setting one column's width to:

| specified width  | used width | |
|------------------|-----------:|---|
| `13rem`          |    208.0px | ✓ |
| `18%`            |    166.1px | ✓ resolved against the table width |
| `calc(18%)`      |    166.1px | ✓ |
| `max(13rem,18%)` |    221.4px | ✗ the even auto split |

`max()` is dropped and the column falls back to auto — so it gets a
*share of the slack* instead of a *floor*, the opposite of the intent,
with no warning. A bare percentage works but has no floor, so it starves
the column at the narrow end to pad it at the wide end. Until table
columns can express minmax, fixed px per column is the honest answer,
and the elastic column should be the one whose content degrades
gracefully (text clips) rather than one full of fixed-size chips or
buttons, which cannot shrink and therefore spill.

## Measure the content minimum before reserving for it

Every column reserved for its worst case, so the most information-dense
column paid for everyone else's padding. Re-measuring per cell showed
where the reserves were real and where they were not:

| column  | intrinsic content                                   | reserve |
|---------|-----------------------------------------------------|---------|
| STATUS  | ONLINE 70.2 + gap 4 + WORKING 84.9 = **159.1px**     | `w-44` (160px box) — 0.9px spare, real |
| ACTIONS | 5 × 28px buttons + 4 × 4px gaps = **156px**          | `w-44` — 4px spare, real |
| TOKEN   | 8-char elision ≤65 + gap 8 + button 24 = **97px**    | was `w-36` (128px box) — 31px nobody used |
| TASKS   | "18 assigned, 3 contributed" ≈ **132px**             | was `w-56` (208px box) — set by a sub-line, not by titles |

## The cheapest column width is the one you don't need

Before moving any pixels, check what the starved cell spends its space
on. This one rendered the agent id on line 1 (clipped at 17 of 39
characters, losing the `@host` suffix that distinguishes two agents) and
`#{agent_id.slice(-6)}` on line 2 — the last six characters of the
string directly above it. Zero information, and ambiguous in practice
(`#system` for both `pikvm-nixos@nixos-developer-system` and
`pikvm-mcp-server@nixos-developer-system`).

Giving that line back to the id doubled the visible characters at no
cost to any other column. Two gotchas:

* shadcn's `<TableCell>` ships `whitespace-nowrap`, which makes
  `break-words` inert — the text stays on one line however much room the
  box has. `whitespace-normal` on the specific column that should wrap.
* Agent ids have no spaces, so `break-words` (not just wrapping) is
  required to reach the second line at all, and `line-clamp-2` caps the
  height so a pathological id still cannot grow the row. It also brings
  `overflow: hidden`, which keeps the containment invariant.
