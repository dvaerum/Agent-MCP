# Agent lifecycle audit — 2026-06-04

Prompted by Dennis's spec: *"It should verify that it can create an
agent and delete and purge an agent. When an agent is purged, there
should be one less agent than before shown on the agent page."*

Companion to [dashboard-safety-2026-06-04.md](./dashboard-safety-2026-06-04.md)
(the auto-terminate-idle sweep that landed PRs #117 / #118 / #119 /
#120 earlier today). That review covered *implicit* state-mutating
behaviour. This one covers the *explicit* user-triggered lifecycle:
the Create, Terminate, Restore, and Purge buttons on the Agents
page (`agent_mcp/dashboard/components/dashboard/agents-dashboard.tsx`).

## Method

Read-through trace from each row-action button down to its API
client method, REST route, backend handler, MCP tool implementation
(where applicable), and the SQL the row eventually flows through.
Cross-checked against the live production stack
(`https://nixos-developer-system.tailfdae0.ts.net/agent-mcp/api/washing-brothers/...`)
with `curl` for the observable wire shape.

## Behaviour model (post-PR #121)

| Button       | Visible when         | API call                                           | DB effect                                                                                |
| ------------ | -------------------- | -------------------------------------------------- | ---------------------------------------------------------------------------------------- |
| **Deploy**   | always               | `POST /api/agents` (admin token in body)           | INSERT into `agents`; `current_task` NULL when no `task_ids` are bundled.                |
| **View**     | every row            | none (read-only modal)                             | n/a                                                                                      |
| **Edit**     | non-Admin            | `POST /api/agents/<id>/edit`                       | UPDATE editable cols (`capabilities`, `color`, `working_directory`, `aoe_session_id`).    |
| **Terminate**| non-Admin, !terminated | `POST /api/terminate-agent`                        | UPDATE `agents` SET `status='terminated'`. Row stays.                                    |
| **Restore**  | non-Admin, terminated | `POST /api/agents/<id>/restore`                    | UPDATE `agents` SET `status='active'`. Row stays.                                        |
| **Purge**    | non-Admin, terminated | `DELETE /api/agents/<id>?cascade=true`             | Cascade-tombstone references in `agent_messages` / `tasks` / `agent_actions`; then DELETE the `agents` row. The synthetic admin pseudo-row inserts a `tombstone` agent so FK constraints (PR-G1) hold. |

Admin row is special-cased on the frontend — no Edit / Terminate /
Restore / Purge buttons render (`agent.agent_id !== 'Admin'` guard),
so the synthetic-admin pattern from PR #113 is safe.

`refreshData()` fires after Terminate, Restore, and Purge, so the
stats card and table both reflect the new state without a manual
refresh. The Purge confirmation dialog also fires `refreshData()` on
`onConfirmed`. **The "count drops by 1 after purge" assertion
Dennis specified holds.**

## Findings

| # | Severity | Finding | Disposition |
|---|----------|---------|-------------|
| L1 | **HIGH** | Deploy button completely non-functional. Three layered defects: `POST /api/agents` → 405 (route was GET-only); `apiClient.createAgent` omitted the admin token; `create_agent_tool_impl` required a non-empty `task_ids` list the modal cannot collect. All three combined since the dashboard was introduced (July 2025). | Fixed in **PR #121** (v5.0.6). Regression guard: `tests/test_dashboard_create_agent_endpoint.py` (6 assertions). |
| L2 | LOW | Spec/UI naming drift. Dennis's spec uses "delete"; the row-action button is labelled "Terminate" (Trash2 icon). Both refer to the soft-delete (status='terminated'); the hard-delete is "Purge". Tooltip on the Terminate button already says "soft-delete; can be restored or purged after". | Documentation; no code change. |
| L3 | LOW | "Working Directory" field in the Deploy modal is surfaced but post-PR #100 every agent shares the project root via file-level locking (see `agent_mcp/tools/admin_tools.py` line ~218 `# All agents work in the same shared directory`). The field is accepted by the REST shim and stored, but never honoured for non-daemon workers. | Out of scope for this lifecycle audit; flag for a future dashboard cleanup PR. |
| L4 | INFO | Purge dialog correctly fetches preview counts (`getPurgePreview`) and surfaces blast-radius (messages_sent, tasks_created, etc.) before confirming. Transaction is BEGIN/COMMIT-wrapped with `DELETE FROM agents` ordered last so half-purged state is impossible. | No action — model is correct. |
| L5 | INFO | After Purge succeeds, the dialog's `onConfirmed` calls `void refreshData()` and the table re-renders. `stats.total = agents.length` reads from the post-refetch state, so the Total chip drops by one alongside the row disappearance. | No action — model is correct. |

## What pre-existing safety guards already covered

The dashboard-safety review on the same day (PRs #117–#120) had
already removed:
- The auto-terminate-idle-agents `setInterval` loop that would have
  silently purged the e2e-test agent mid-test.
- The `shouldDisplayAgent` age filter that hid newly-created agents
  from the table.
- The dead `getIdleAgentsForCleanup` selector.
- The `request<T>()` 5xx retry on mutating methods (so the dashboard
  doesn't double-fire create/terminate/purge under transient 502s).

Without those guards in place this lifecycle audit would have hit a
much messier set of "phantom state" symptoms. PR #121 is the
positive complement: the lifecycle now *also* moves forward
correctly on explicit user actions.

## What was deliberately out of scope

- Re-litigating the implicit-mutation findings already shipped in
  PRs #117 / #118 / #119.
- The `Working Directory` Deploy field (L3) — cosmetic, doesn't
  affect lifecycle correctness.
- A dedicated `pkgs.nixosTest` VM check for the create→terminate→purge
  flow. The Python harness test in PR #121 exercises the same code
  path (Starlette TestClient against the full app + lifespan) and
  runs in ~20s vs. the 10-15 min a VM boot would cost. If a
  later regression slips past the Python suite (e.g. an aiohttp
  router-layer concern), the existing `vm-multi-tenant` /
  `vm-no-auto-cleanup` checks remain the venue.

## PRs shipped from this audit

- **PR #121** (v5.0.6) — `fix(dashboard): make Agents-page Deploy
  button functional`. RED `e8ded7f` + GREEN `3036dae`. 6 new tests,
  3 file fixes (backend route, frontend api.ts, MCP tool schema).
