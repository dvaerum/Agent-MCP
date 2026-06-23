# Connecting external MCP clients to a project

After the `system_token` retirement (PRs #208, #209, #210), the
project's admin / system token no longer exists in any form — there
is no DB row, no on-disk file, no CLI flag, no global, and no API
endpoint that exposes it. External MCP clients (Claude Code, IDE
plugins, ad-hoc scripts) that previously authenticated against
`/agent-mcp/mcp/<project>` with the admin token are broken until
they are migrated to **per-agent bearer tokens**.

This document is the migration guide for operators. It is the
out-of-tree counterpart to the in-dashboard auth path described in
[ADR-0013](../adr/0013-operator-login.md) (which now applies only to
browser-driven, cookie-based dashboard traffic).

## Concepts

- **Per-agent token** — every row in a project's `agents` table has
  a unique `token` (the primary key). The agent's `agent_role`
  column (`worker` or `manager`) controls which MCP tools the token
  can call. This is now the only credential that the backend's
  `AuthHeaderMiddleware` accepts on `/agent-mcp/mcp/<project>` for
  out-of-tree clients.
- **Operator cookie** — the dashboard's authentication. The router
  validates the cookie locally and forwards requests to the backend
  with a signed `X-Agent-MCP-Forwarded-Operator` header (Wave 2,
  PR #209). This path is browser-friendly but not practical for CLI
  clients that don't manage cookies.
- **Per-agent bearer is the only out-of-tree path.** There is no
  remaining shared, project-wide token. Every external integration
  needs its own agent row.

## Provision a worker agent for an external integration

1. Log in to the dashboard at
   `http://<host>:5454/agent-mcp/login` and open the project.
2. Navigate to the Agents tab and click **Add Agent**.
3. Pick a meaningful `agent_id` that names the integration, e.g.
   `claude-code-laptop`, `ide-plugin-jetbrains`,
   `ci-runner-github-actions`. Avoid generic names — the `agent_id`
   shows up in audit logs and tool-call attribution.
4. Choose the role:
   - **`worker`** — for read-mostly or scoped integrations. Worker
     tokens cannot call manager-tier tools like `create_agent`.
   - **`manager`** — for integrations that must assign tasks, edit
     subordinate agents, or invoke any tool gated by
     `verify_token(token, "manager")` in `agent_mcp/core/auth.py`.
   - Operator-tier capabilities (writing `config_*` keys, managing
     users, project administration) are **not** available to either
     role; those flow through operator session cookies only.
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
         "url": "http://<host>:5454/agent-mcp/mcp/<project>",
         "headers": {
           "Authorization": "Bearer <per-agent-token>"
         }
       }
     }
   }
   ```

## Role permissions at a glance

The role enforcement lives in `agent_mcp/core/auth.py:verify_token`.
The relevant tiers post-`system_token` retirement:

| Tier | How it authenticates | What it can do |
| --- | --- | --- |
| `worker` agent token | `Authorization: Bearer <token>` against `/agent-mcp/mcp/<project>` | Call `agent`-tier tools (query RAG, view tasks, update own task status, send messages). Cannot create or edit other agents. |
| `manager` agent token | Same bearer path | Worker capabilities **plus** manager-tier tools (assign tasks, edit subordinate agents, prompt-builder for spawned agents). |
| Operator session | Dashboard cookie → router-signed forwarding header → backend | Everything; including writing `config_*` keys, managing users, project administration. **No bearer equivalent** — operator capabilities are dashboard-only by design. |

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
  - Agent was terminated by an operator or by a manager-tier agent.
  - The integration is still sending the old admin / system token,
    which is no longer accepted.

**`403` on a tool call that used to work**

- The integration's per-agent token is for a `worker`-role agent
  but the tool requires `manager`-tier (e.g. `create_agent`,
  `assign_task`). Re-provision the integration with a `manager`-role
  agent, or rework the integration so the manager-tier action runs
  from a different surface.
- The tool requires operator-tier identity (`config_*` writes, user
  management). There is no bearer-token path for these; drive them
  through the dashboard cookie session.

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
  capabilities; see the addendum at the bottom of the ADR for what
  changed in the `system_token` retirement).
- `agent_mcp/core/auth.py` — `verify_token` is the source of truth
  for role enforcement.
- `agent_mcp/app/main_app.py` — `AuthHeaderMiddleware` is the
  bearer-vs-forwarding-header gate at the HTTP edge.
