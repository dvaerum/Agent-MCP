# Dashboard safety review — 2026-06-04

Prompted by the auto-terminate-idle-agents incident
(washing-brothers, 2026-06-04). The dashboard ran a `setInterval`
every 2 minutes that silently terminated any non-Admin agent that
had been alive for more than 10 minutes without a `current_task`.
Symptom: daemon workers (`backend-dev`, `ios-app-dev`) authenticated
fine immediately post-restart, then started returning 401 every
~2 minutes once the agents crossed the 10-minute threshold and the
next dashboard poll fired.

PR #117 (v5.0.3) removed the loop. PR #118 (v5.0.4) removed the
sibling `shouldDisplayAgent` filter that hid the same workers from
the UI and the dead `getIdleAgentsForCleanup` selector.

This review is the broader sweep. The target is any client code
that mutates server state without explicit user intent, OR mutates
server state more times than the user clicked a button.

## Method

Read-through of every file under `agent_mcp/dashboard/` looking
for:

1. `setInterval` / `setTimeout` that calls a state-changing API
   (`terminate`, `delete`, `restart`, `restore`, `purge`, `assign`,
   `unassign`, etc.) without an explicit user click at fire time.
2. `useEffect` that POSTs / DELETEs / PUTs based on derived state —
   i.e. the dashboard "deciding" to clean things up because it
   noticed a condition.
3. `visibilitychange` / `focus` / `blur` handlers that mutate.
4. Polling that mutates (vs. read-only polling, which is fine).
5. Garbage-collection-like behaviour — code that decides to "tidy
   up" server data based on heuristics (age, status, count).
6. Pseudo-RPC from React without user intent — selector-helpers
   (`getXForCleanup`, `pickStaleY`, `findOrphans`) feeding action
   loops.
7. Display predicates that hide live data based on age/status,
   masking that something exists. (Example just fixed in PR #118:
   `shouldDisplayAgent` hiding agents > 10 min old without tasks.)
8. Unbounded retry / restart loops without back-off or surrender.
9. Optimistic mutations without rollback.
10. Shared bearer abuse — anywhere the dashboard's admin bearer
    fires actions attributable in audit logs as "admin" when the
    human didn't initiate them.

## Findings

| # | Severity | Finding | Disposition |
|---|----------|---------|-------------|
| 1 | CRITICAL | `setInterval(handleTerminateAgent, 2min)` over `getIdleAgentsForCleanup()` — silent worker reaper masquerading as "cleanup" | Fixed PR #117 (v5.0.3) — pre-dates this review |
| 2 | HIGH | `shouldDisplayAgent` hid every non-admin agent older than 10 min without `current_task` — companion bug to #1, workers vanished from UI even before being reaped | Fixed PR #118 (v5.0.4) — pre-dates this review |
| 3 | MEDIUM | Dead `getIdleAgentsForCleanup` selector + lying "N pending cleanup" badge — selector still surfaced a count, badge said items were "pending cleanup" but nothing was scheduled to run | Fixed PR #118 (v5.0.4) — pre-dates this review |
| 4 | MEDIUM | `ApiClient.request<T>()` retries on 5xx without gating on HTTP method — universal cold-start absorption double-fires non-idempotent POST/PATCH/DELETE when the backend processed the mutation but disconnected on the response phase | Fixed PR #119 (v5.0.5) — this review |

### 4. Detail — `request<T>()` retries POST/PATCH/DELETE on 5xx

**File**: `agent_mcp/dashboard/lib/api.ts`, around lines 215–230 in
the v5.0.4 source.

**Pattern**:

```ts
for (let attempt = 0; attempt < 3; attempt++) {
  response = await fetch(url, { ...fetchOptions, signal: ... })
  if (response.status >= 500 && response.status < 600 && attempt < 2) {
    await new Promise(res => setTimeout(res, 200 * 2 ** attempt))
    continue
  }
  break
}
```

No reference to `fetchOptions.method`. The loop wraps every API
call, including:

- `createAgent` / `createTask` / `sendMessage` (POST, server-
  generated id)
- `editAgent` / `updateTask` (POST, applies a delta)
- `deleteTask` / `purgeAgent` / `deleteMemory` (DELETE — server-
  side these are idempotent so a retry wastes a round trip but is
  not destructive)

**Bug shape**: backend processes a mutation, commits the side
effect, then crashes or disconnects returning 502/503/504 on the
response phase. The retry re-issues the request.

- `createTask` → two identical tasks. Server generates the
  `task_id`, no uniqueness collision to catch the dup. UI shows
  one, refresh shows two.
- `sendMessage` → recipient receives the message twice, double
  fan-out events.
- `createAgent` → first request creates the agent. Retry returns
  409 "already exists" (4xx, surfaces to user). User assumes
  nothing happened — meanwhile the agent is live.

**Fix shipped in PR #119**: gate the retry on a `method` check.
Only `GET` and `HEAD` are retried; mutations bubble 5xx to the
caller's catch handler.

Cold-start absorption (the original reason for the retry) is
preserved — the first request after a backend restart is almost
always a GET (the data store's `getAllData()` on project load).
Mutations are user-initiated, so the user can see the error and
click again.

## Patterns reviewed and cleared

The following were checked and judged safe:

- **`setInterval` in `lib/stores/data-store.ts:484`**: 60-second
  refresh tick. Calls `store.refreshData()` which is a pure GET.
  Module-level so it never tears down, but no destructive side
  effect.
- **`setInterval` in `components/dashboard/tasks-dashboard.tsx`,
  `vis-graph.tsx`, `vis-network-loader.tsx`**: all background-
  refresh polls. Read-only.
- **`visibilitychange` handler in `lib/mcp-notifications.ts:374`**:
  pauses/resumes the MCP SSE stream when the tab is
  hidden/visible. No mutations.
- **`document.addEventListener` event handlers (sidebar
  keybindings, media-query change)**: cosmetic only.
- **`McpNotificationsProvider` notification dispatch**: notifications
  trigger `refreshData()` / `invalidatePromptsCatalog()` — both
  pure GETs.
- **`settings-dashboard.tsx` optimistic toggle**: flips local state
  before the PUT/POST, reverts on failure. Has proper rollback.
- **`alias-chip-panel.tsx`, `rename-project-modal.tsx`,
  `remove-project-modal.tsx`, `add-project-modal.tsx`**: raw fetch
  to project-management endpoints, all behind a user-clicked
  confirm button.
- **`autoDetectServers`**: only called from a manual "Detect
  Server" button click; the useEffect that used to call it on
  mount is commented out (`components/server/server-connection.tsx:30-34`).
- **MCP SSE reconnect loop**: bounded exponential backoff at 30 s
  max delay. No retry surrender, but bounded delay is enough for
  this use case (the dashboard is best-effort).

## PRs shipped from this review

| PR | Title | Version | Merge SHA |
|----|-------|---------|-----------|
| #119 | `fix(dashboard): gate request<T>() retry to idempotent methods` | v5.0.5 | `421d7f3` |

## Sentinel

This document is the human-readable index. The structural pins
(source-grep regression tests) live in:

- `tests/test_dashboard_no_auto_cleanup.py` — covers findings 1, 2, 3.
- `tests/test_dashboard_api_no_mutation_retry.py` — covers finding 4.

Future contributors who trip these tests should re-read the
relevant section above before deciding whether to "fix" the test
or the implementation.
