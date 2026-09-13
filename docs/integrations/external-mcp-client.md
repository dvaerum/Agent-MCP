# Connecting external MCP clients to a project

There is no project-wide admin / system token in any form — no DB
row, no on-disk file, no CLI flag, no global, and no API endpoint
that exposes it (retired in the original Python implementation's
`system_token` retirement, and never reintroduced by the Rust
rewrite — see `~/.claude/plans/prancy-napping-pie.md` for the full
Python→Rust migration this project completed). External MCP clients
(Claude Code, IDE plugins, ad-hoc scripts) authenticate against
`/agent-mcp/mcp/<project>` with **per-agent bearer tokens** — this
document is the setup guide.

This is the out-of-tree counterpart to the in-dashboard auth path
described in [ADR-0013](../adr/0013-operator-login.md) (which
applies to browser-driven, cookie-based dashboard traffic — that
cookie is resolved by `conexus-router`'s `session_gate.rs`, then
forwarded to the per-project backend as a signed
`X-Agent-MCP-Forwarded-Operator` header; see `CONTEXT.md` for the
full identity/authorization vocabulary, including exactly which
capabilities a `worker`- vs `manager`-role bearer carries).

## Concepts

- **Per-agent token** — every row in a project's `agents` table has
  a unique `token` (the primary key). The agent's `agent_role`
  column (`worker` or `manager`) controls which MCP tools the token
  can call — see `CONTEXT.md`'s "Role bundle" section for the exact
  capability sets. This is the only credential the backend's
  `/agent-mcp/mcp/<project>` endpoint accepts for out-of-tree
  clients (verified in `conexus-backend/src/principal_resolve.rs`).
- **Operator cookie** — the dashboard's authentication. The router
  validates the cookie locally (`conexus-router::session_gate`) and
  forwards requests to the backend with a signed
  `X-Agent-MCP-Forwarded-Operator` header. This path is
  browser-friendly but not practical for CLI clients that don't
  manage cookies.
- **Per-agent bearer is the only out-of-tree path.** There is no
  remaining shared, project-wide token. Every external integration
  needs its own agent row.

## Provision a worker agent for an external integration

1. Log in to the dashboard at
   `http://<host>:1337/agent-mcp/login` and open the project.
2. Navigate to the Agents tab and click **Add Agent** (dispatches the
   `register_agent` MCP tool, `Cap(agents.register)` — operator-tier
   only; see `CONTEXT.md`).
3. Pick a meaningful `agent_id` that names the integration, e.g.
   `claude-code-laptop`, `ide-plugin-jetbrains`,
   `ci-runner-github-actions`. Avoid generic names — the `agent_id`
   shows up in audit logs and tool-call attribution.
4. Choose the role:
   - **`worker`** — for read-mostly or scoped integrations. Grants
     `McpConnect`/`AgentsUse`/`TasksView`/`TasksCreate`/`TasksUpdate`/
     `MemoriesView`/`MessagesView`/`MessagesSend`/`FilesUse`/
     `CoordinationAssist`/`CoordinationWait`/`RagQuery` (the full,
     exact set is `agent_role_bundle(Worker)`,
     `conexus-core/src/capability.rs:233`) — plus self-assign/
     self-file task creation, gated by the `config_allow_worker_
     self_assign`/`config_allow_worker_create_unassigned` project
     toggles (dashboard Settings tab).
   - **`manager`** — worker capabilities **plus** `TasksAssign`
     (assign a task to a DIFFERENT agent) and `MemoriesUpdate`.
   - Registering, editing, terminating, or rotating another agent's
     token, and every operator-tier capability (writing `config_*`
     keys, managing users, project administration) are **not**
     available to either bearer role — `AgentsRegister`/
     `AgentsTerminate`/`AgentsRotateToken` are deliberately absent
     from both agent-role bundles (see `CONTEXT.md`'s "Role bundle"
     section). Those flow through operator session cookies (or a
     confirmed-operator-tier bearer presented directly to a REST
     endpoint, `RestPrincipal::OperatorBearer`) only.
5. Submit. The new agent row appears in the table. Use the copy
   button next to the truncated token preview in the Token cell to
   copy the full token to the clipboard.
6. Configure the external MCP client to send the token as a Bearer
   credential:

   ```
   Authorization: Bearer <per-agent-token>
   ```

   For a Claude Code `mcp.json` entry pointed at the project:

   ```json
   {
     "mcpServers": {
       "agent-mcp-<project>": {
         "url": "http://<host>:1337/agent-mcp/mcp/<project>",
         "headers": {
           "Authorization": "Bearer <per-agent-token>"
         }
       }
     }
   }
   ```

## Role permissions at a glance

The role enforcement is the `Requirement::Cap`/`Requirement::Policy`
each `Tool` impl declares (`conexus-auth/src/requirement.rs`), checked
by `dispatch()` against `Principal::has_capability`
(`conexus-core/src/principal.rs:79`) before any tool body runs. See
`CONTEXT.md` for the full vocabulary.

| Tier | How it authenticates | What it can do |
| --- | --- | --- |
| `worker` agent token | `Authorization: Bearer <token>` against `/agent-mcp/mcp/<project>` | `agent_role_bundle(Worker)` (query RAG, view/create/update tasks, send messages, self-assign/self-file per project toggle). Cannot register, edit, terminate, or rotate other agents' tokens, and cannot assign a task to a different agent. |
| `manager` agent token | Same bearer path | Worker capabilities **plus** `TasksAssign` (assign to a different agent) and `MemoriesUpdate`. Still cannot register/edit/terminate/rotate agents — that stays operator-tier only. |
| Operator session (dashboard cookie → router-signed forwarding header → backend) | Cookie via `conexus-router::session_gate`, forwarded as `X-Agent-MCP-Forwarded-Operator` | Everything; including registering/editing/terminating agents, writing `config_*` keys, managing users, project administration. **No bearer equivalent for a `worker`/`manager` agent token** — a confirmed-operator-tier bearer can reach the same REST surface directly via `RestPrincipal::OperatorBearer`, but that requires an agent row with `agent_role == Manager` presented to a REST endpoint, not a `worker` token. |

If an external integration genuinely needs operator-tier capabilities
(modify project settings, manage users, install bootstrap config),
the right answer is to drive the dashboard via cookie auth — not to
provision a privileged bearer token. There is no longer a
shared-secret escape hatch.

## Migrating an existing integration

If your client previously authenticated with the admin / system
token:

1. Provision a per-agent token following the steps above. Match the
   role to the integration's actual needs (default to `worker`).
2. Replace the old admin-token Bearer value in the client's
   configuration with the new per-agent token.
3. Audit what the integration actually does. If any of its calls
   require operator-tier capabilities, those calls will fail with
   `403` even on a `manager` token — surface them as bugs to fix on
   the integration side (move to a dashboard workflow, or split the
   integration so the operator-only steps run from a logged-in
   session).
4. Remove any reference to the legacy `--admin-token-out` /
   `--admin-token-in` / `--admin-token-log` plumbing or the
   `MCP_ADMIN_TOKEN` / `MCP_SYSTEM_TOKEN` env vars. All of these
   were deleted in Wave 3 (PR #210) and silently no-op now.

## What if I lose a per-agent token?

- **Dashboard** — open the agent's row in the Agents tab and click
  the copy button. The token is shown in full to operators
  authenticated for that project.
- **API** — `GET /agent-mcp/api/<project>/tokens` returns the
  current per-agent tokens as
  `{"agent_tokens": [{"agent_id": "...", "token": "..."}, ...]}`.
  This endpoint is gated by operator session cookies; it is not
  reachable with a bearer token.
- **Lost both** — terminate the agent (manager-tier or operator
  action) and provision a fresh agent row. There is no way to
  recover a lost token; the column is the credential itself, not a
  hash.

## Troubleshooting

**`401 invalid or missing agent bearer token` on `/agent-mcp/mcp/<project>`**

- The token doesn't match any row in the project's `agents` table.
  Common causes:
  - Typo in the header (extra whitespace, missing `Bearer ` prefix).
  - Token is for a different project. Each project has its own
    `agents` table; tokens are not portable between projects.
  - Agent was terminated — always an operator-tier action
    (`Cap(agents.terminate)`, absent from both `worker` and `manager`
    agent-role bundles; see `CONTEXT.md`), never something a peer
    agent bearer can do to another.
  - The integration is still sending the old admin / system token,
    which is no longer accepted.

**`403` on a tool call that used to work**

- The integration's per-agent token is for a `worker`-role agent but
  the tool needs `TasksAssign` (assigning a task to a DIFFERENT
  agent, not self-assign) or `MemoriesUpdate` — the two capabilities
  `manager` adds over `worker`. Re-provision the integration with a
  `manager`-role agent, or rework the integration to only self-assign
  (gated by the `config_allow_worker_self_assign` project toggle
  instead, no re-provisioning needed).
- The tool requires operator-tier identity (registering/editing/
  terminating/rotating agents, `config_*` writes, user management).
  There is no `worker`/`manager` bearer-token path for these; drive
  them through the dashboard cookie session, or present a
  `manager`-role bearer directly to the REST surface (which resolves
  to full project-operator capabilities there — see `CONTEXT.md`'s
  `RestPrincipal::OperatorBearer` entry — but this is a REST-only
  door, not available on the MCP bearer path).

**MCP notifications (SSE) on `/agent-mcp/mcp/<project>`**

- The bearer path works the same for SSE as for regular HTTP
  requests — set `Authorization: Bearer <per-agent-token>` on the
  initial GET.
- The dashboard's MCP-notifications provider uses the cookie
  forwarding path instead; out-of-tree clients should not try to
  reproduce that path and should use the bearer.

## See also

- [ADR-0013](../adr/0013-operator-login.md) — operator login on the
  dashboard surface (now the only home of the legacy "admin"
  capabilities).
- `CONTEXT.md` — the full identity/authorization vocabulary
  (`Principal`, `RestPrincipal`, `Capability`, role bundles,
  `is_operator_tier`/`is_confirmed_operator_tier`) with exact source
  locations in the `conexus-*` Rust crates.
- `conexus-backend/src/principal_resolve.rs` — the bearer-vs-
  forwarding-header gate at the HTTP edge for `/agent-mcp/mcp/<project>`.
- `conexus-auth/src/requirement.rs` / `conexus-auth/src/tool.rs` —
  per-tool authorization declarations (`Requirement::Cap`/`Policy`)
  and `dispatch()`, the single point every tool call's gate check
  runs through.
