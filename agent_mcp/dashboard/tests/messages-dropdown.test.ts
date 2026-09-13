/**
 * Regression guards for the Messages tab Compose recipient dropdown.
 *
 * The original PR #21 shipped a free-text Input for the recipient. Dennis
 * asked for a dropdown of existing agents (with admin pinned at the top)
 * so admins don't have to type agent_id by hand. This test parses the
 * .tsx so we catch silent regressions without needing jsdom.
 *
 * Also asserts the Messages tab uses POST /api/messages/query for
 * listing — the original GET-with-body call failed in the browser
 * because the Fetch spec strips bodies from GET requests.
 */

import { describe, expect, it } from "vitest"
import { readFileSync } from "node:fs"
import { resolve } from "node:path"
import { messagesPageSource } from "./support/messages-source"

const DASHBOARD_ROOT = resolve(__dirname, "..")
const read = (rel: string) =>
  readFileSync(resolve(DASHBOARD_ROOT, rel), "utf8")

// Wave 5 (refactor/w5-messages): the Messages page was split from a
// single god-file into a page module + a `messages/` satellite
// directory. These guards assert properties of the PAGE (its compose
// dropdown, its filter bar, its table), not of any one file, so they
// read the page and its satellites as one blob — the same idiom the
// Agents split established in tests/support/messages-source.ts.
const readMessagesDashboard = () => messagesPageSource()

describe("Compose recipient dropdown", () => {
  it("uses a Select, not an Input, for the recipient field", () => {
    const src = readMessagesDashboard()
    // The "Recipient" label must be followed by a <Select>, not <Input>,
    // before the next label/section starts. Find the JSX label (not the
    // state variable name) by anchoring on the Label tag.
    const idx = src.indexOf("Recipient agent_id")
    expect(idx, "Recipient label not found in messages-dashboard.tsx").toBeGreaterThan(0)
    // Look at the ~600 chars after the label — covers the JSX block.
    const nearby = src.slice(idx, idx + 600)
    expect(
      nearby.includes("<Select"),
      "expected the Recipient field to be a <Select> dropdown, " +
        `but no <Select> appeared near the label:\n${JSON.stringify(nearby)}`,
    ).toBe(true)
    expect(
      nearby.includes("<Input"),
      "expected the Recipient field to be a <Select> dropdown — " +
        `<Input> still present near the label:\n${JSON.stringify(nearby)}`,
    ).toBe(false)
  })

  it("populates recipient options from the /participants endpoint", () => {
    // Originally asserted apiClient.getAgents() was the dropdown source.
    //
    // Dennis flagged ghost agents — /api/agents returns every row
    // including status='terminated' — so the source changed to
    // /api/messages/participants, which returns {live, tombstones}
    // (terminated agents excluded). The Compose recipient renders the
    // `live` list only; the From/To filters render live + tombstones.
    const src = readMessagesDashboard()
    expect(
      src.includes("/participants") || src.includes("/messages/participants"),
      "expected the Compose form to populate recipient options from " +
        "the /api/messages/participants endpoint (live agents only)",
    ).toBe(true)
  })

  it("includes admin as a hardcoded recipient option", () => {
    const src = readMessagesDashboard()
    // "admin" must be present as a string literal (hardcoded option,
    // because /api/agents may or may not include the admin agent).
    expect(
      src.includes('"admin"') || src.includes("'admin'"),
      "expected admin to be a hardcoded recipient option",
    ).toBe(true)
  })

  it("listing uses POST /messages/query, not GET", () => {
    // The message listing must POST to ``/api/messages/query``.
    //
    // History: the original bug was a GET with a JSON body — browsers
    // strip GET bodies per the Fetch spec, so the listing silently
    // failed. The fix was a bespoke ``callMessages('POST', '/query', …)``
    // helper, later folded into ``usePagedQuery<Message>`` (PR 5). Post
    // W6-followup F3 the listing rides TanStack Query: the page mounts
    // ``useMessagesQuery`` (``lib/queries/messages.ts``), which calls
    // ``getMessages`` in the api layer (``lib/api/messages.ts``) — the lone
    // owner of the ``/messages/query`` endpoint + the POST verb now.
    //
    // This guard is repointed through the api-layer indirection (the
    // retired ``use-paged-query.ts`` hook was deleted in F3): the page wires
    // ``useMessagesQuery``, and the POST-to-``/messages/query`` lives in the
    // api module.
    const src = readMessagesDashboard()
    // 1. The page delegates the listing fetch to the TanStack query.
    expect(
      src.includes("useMessagesQuery"),
      "expected messages-dashboard to delegate the listing fetch to " +
        "useMessagesQuery (W6-followup F3)",
    ).toBe(true)
    expect(
      src.includes("usePagedQuery"),
      "expected the retired usePagedQuery hook to be gone from " +
        "messages-dashboard after the F3 migration",
    ).toBe(false)
    // 2. The api layer is the lone owner of the endpoint + POST verb.
    const apiSrc = read("lib/api/messages.ts")
    expect(
      apiSrc.includes('"/messages/query"') || apiSrc.includes("'/messages/query'"),
      "expected getMessages (lib/api/messages.ts) to POST to " +
        "'/messages/query'",
    ).toBe(true)
    expect(
      apiSrc.includes('"POST"') || apiSrc.includes("'POST'"),
      "expected getMessages's fetch path to use POST " +
        "(the GET-with-body bug must stay buried)",
    ).toBe(true)
    // And the original buggy call must be gone.
    expect(
      src.includes('"GET", ""') || src.includes("'GET', ''"),
      "expected the GET-with-empty-suffix call to be removed; it was " +
        "the original bug (browsers strip GET bodies)",
    ).toBe(false)
  })
})

// ---------- Filter dropdowns (from / to) -----------------------
// The original Filters card used <Input> text boxes for "from" and "to".
// Dennis asked for dropdowns populated from /api/agents so admins can
// pick known sender/recipient ids without typing.

describe("From/To filter dropdowns", () => {
  it("from-filter uses a Select, not an Input", () => {
    const src = readMessagesDashboard()
    // Free-text Input for from/to must be gone.
    expect(
      src.includes('placeholder="from (sender_id)"'),
      "from-filter still uses an <Input> with the old placeholder",
    ).toBe(false)
    // The replacement Select must reference the filters.from state.
    expect(
      src.includes("filters.from") && src.includes("filters.to"),
      "expected the from/to filter state names to remain",
    ).toBe(true)
  })

  it("to-filter uses a Select, not an Input", () => {
    const src = readMessagesDashboard()
    expect(
      src.includes('placeholder="to (recipient_id)"'),
      "to-filter still uses an <Input> with the old placeholder",
    ).toBe(false)
  })

  it("both filter dropdowns expose a no-filter sentinel", () => {
    // Both From and To filter dropdowns must expose a sentinel that
    // clears the filter.
    //
    // Pre-feat/agent-select-dropdown: bespoke ``<SelectItem>``s rendered
    // the literal text ``any sender`` / ``any recipient``.
    //
    // Post-feat/agent-select-dropdown (2026-06-04): the From/To filters
    // are migrated to the shared ``<AgentSelect>`` with
    // ``noneLabel="— Any —"`` — the component's caller-provided
    // sentinel-label convention. Both filters speak the same neutral
    // "Any" label because filter semantics differ from task-form
    // assignment semantics.
    const src = readMessagesDashboard()
    // Look for two AgentSelect occurrences carrying noneLabel with the
    // "Any" label — one for `from`, one for `to`.
    const matches = src
      .split("AgentSelect")
      .filter((m) => m.slice(0, 200).includes("noneLabel") && m.slice(0, 200).includes("Any"))
    expect(
      matches.length,
      "expected both the From and the To filter to render " +
        '<AgentSelect noneLabel="— Any —" /> so admins can clear ' +
        "the filter through the shared component's neutral sentinel",
    ).toBeGreaterThanOrEqual(2)
  })
})

// ---------- Compose: broadcast option --------------------------

describe("Compose recipient: broadcast option", () => {
  it("includes a broadcast recipient option", () => {
    const src = readMessagesDashboard()
    // Broadcast is wired via recipient_id="*" — the API sentinel.
    expect(
      src.includes('"*"') || src.includes("'*'"),
      "expected a recipient_id='*' broadcast option in Compose",
    ).toBe(true)
    expect(
      src.toLowerCase().includes("broadcast"),
      "expected the Compose recipient dropdown to mention broadcast",
    ).toBe(true)
  })
})

// ---------- Bulk selection toolbar -----------------------------

describe("Bulk selection toolbar", () => {
  it("has a select-all-visible checkbox in the table header", () => {
    const src = readMessagesDashboard()
    // Header checkbox toggles every currently-rendered (filtered) row.
    // We're text-parsing, so look for the structural marker rather than
    // the visual one.
    expect(
      src.includes("selectAllVisible") || src.includes("toggleAllVisible"),
      "expected a select-all-visible handler in the table header",
    ).toBe(true)
  })

  it("tracks per-row checkbox selection", () => {
    const src = readMessagesDashboard()
    expect(
      src.includes("selectedIds"),
      "expected selectedIds state to track per-row checkbox selection",
    ).toBe(true)
  })

  it("shows the bulk actions toolbar when rows are selected", () => {
    const src = readMessagesDashboard()
    // Toolbar buttons: mark read / mark unread / delete.
    expect(src.includes("Mark read"), "missing 'Mark read' bulk action button").toBe(true)
    expect(src.includes("Mark unread"), "missing 'Mark unread' bulk action button").toBe(true)
    expect(src.includes("Delete"), "missing 'Delete' bulk action button").toBe(true)
  })

  it("bulk delete calls the DELETE endpoint", () => {
    const src = readMessagesDashboard()
    // callMessages must support the new DELETE verb.
    expect(
      src.includes('"DELETE"') || src.includes("'DELETE'"),
      "expected the component to issue DELETE /api/messages/<id> for " +
        "bulk + row-level delete",
    ).toBe(true)
  })

  it("has an inline per-row delete button", () => {
    const src = readMessagesDashboard()
    // Lucide Trash2 icon is the convention used elsewhere in the
    // dashboard for delete affordances.
    expect(
      src.includes("Trash2") || src.includes("Trash ") || src.includes('icon="trash"'),
      "expected a Trash2 icon import for the per-row delete button",
    ).toBe(true)
  })
})

// ---------- Filter dropdown participant source ------------------
// Original bug: From/To dropdowns sourced from apiClient.getAgents(),
// which returns EVERY agent including status='terminated'. The
// replacement is a new /api/messages/participants endpoint that returns
// live agents only + DISTINCT tombstone strings (sender_id /
// recipient_id beginning with ``[deleted-``). The Compose recipient
// stays live-only (you cannot message a deleted agent).

describe("Filter dropdown participant source", () => {
  it("calls the /messages/participants endpoint", () => {
    const src = readMessagesDashboard()
    // The filter dropdowns must be populated from /messages/participants
    // (admin-token POST), not from apiClient.getAgents(), so terminated
    // agents are excluded.
    expect(
      src.includes("/messages/participants") || src.includes("/participants"),
      "expected the Messages tab to call the new " +
        "/api/messages/participants endpoint to populate the " +
        "Sender/Recipient filter dropdowns (live agents + tombstones)",
    ).toBe(true)
  })

  it("excludes terminated agents", () => {
    const src = readMessagesDashboard()
    // Defensive client-side filter even if a stale getAgents() call
    // remains; the symbolic check is that the terminated status is
    // explicitly excluded somewhere in the participant pipeline.
    // The new flow drops getAgents() entirely from the *filter* dropdown
    // source, so the only call to apiClient.getAgents() that may remain
    // is the Compose recipient list. Either path must not include
    // terminated agents in the From/To dropdowns.
    if (src.includes("apiClient.getAgents()")) {
      // If getAgents is still used (e.g., to feed the Compose
      // recipient), there must be an explicit terminated filter.
      expect(
        src.includes("terminated"),
        "Compose recipient still uses apiClient.getAgents(); must " +
          "filter status='terminated' out before rendering options",
      ).toBe(true)
    }
  })

  it("renders live agents only via the shared AgentSelect", () => {
    // Post-feat/agent-select-dropdown (2026-06-04): the From/To filter
    // dropdowns are migrated to the shared ``<AgentSelect>``, which
    // sources live agents only via ``data-store::getActiveAgents``.
    //
    // The previous assertion required tombstone rendering so admins
    // could grep history for purged agents — a useful affordance, but
    // the prancy-napping-pie plan's locked design decision was
    // live-only across every <AgentSelect> call site for consistency.
    // If lost-tombstone-search matters in practice, a follow-up PR can
    // either add a parallel "Tombstones" search box or extend
    // <AgentSelect> with an explicit ``extraItems`` prop. For now the
    // invariant is: the filter dropdowns are <AgentSelect> instances
    // and therefore live-only by construction.
    const src = readMessagesDashboard()
    // The filter dropdowns must use AgentSelect rather than the old
    // local Select-over-filterOptions wiring.
    expect(
      src.includes("AgentSelect") && src.includes("agent-select"),
      "expected the From/To filter dropdowns to render via the " +
        "shared <AgentSelect> component (live-only by construction)",
    ).toBe(true)
  })

  it("Compose recipient excludes tombstones", () => {
    const src = readMessagesDashboard()
    // Compose recipient must remain live-only — you cannot message a
    // deleted agent. The simplest evidence is that the compose flow
    // does not iterate `tombstones` into its <SelectItem>s.
    // Find the Recipient label and inspect ~1200 chars after.
    const idx = src.indexOf("Recipient agent_id")
    expect(idx, "Recipient label not found").toBeGreaterThan(0)
    const block = src.slice(idx, idx + 1200)
    expect(
      block.includes("tombstones"),
      "Compose recipient block should not render tombstone agent ids " +
        "(you cannot message a deleted agent); render live agents only",
    ).toBe(false)
  })
})
