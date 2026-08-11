// Agents resource module — types + the agent-scoped client methods.
//
// W6-followup F1: the lean `getAgents()` (GET /api/agents) method was
// REMOVED here. Its sole caller (schedules-dashboard's agent picker)
// only ever read `agent_id`, which the shared `/all-data` envelope
// already provides via `useActiveAgents()` — and the lean endpoint's
// projection lacks `auth_token` / `transport_status` / `profile` / the
// presence fields the `Agent` type promises, so `getAgents(): Agent[]`
// was structurally field-incomplete. The agents LIST everywhere in the
// dashboard renders from `/all-data` (see `lib/queries/all-data.ts`),
// which returns the rich agent shape (auth_token included, from
// `agent_mcp/app/routers/composition.py`). Rather than grow the lean
// endpoint rich columns for a consumer that wanted one string, the
// dead lean path is dropped and schedules reads the shared query.

import type { ApiClient } from './client'
import { type RawTask, type Task, normalizeTask } from './tasks'

export interface Agent {
  agent_id: string
  status: 'pending' | 'running' | 'terminated' | 'failed'
  current_task?: string
  working_directory?: string
  color?: string
  created_at: string
  updated_at: string
  terminated_at?: string | null
  auth_token?: string
  // 16-char lowercase hex string identifying the matching session
  // used by the ADR-0021 delivery bridge to route messages to this
  // agent. Empty/missing = no binding (delivery falls back to
  // title-match resolution).
  aoe_session_id?: string | null
  // Event-coord PR-1: per-agent wake-loop toggle. Default TRUE for
  // every existing row (backfilled via 0010 migration `DEFAULT 1`).
  // When FALSE, the PR-2 `serverInfo.instructions` wake-loop
  // bootstrap is omitted for this agent regardless of the global
  // flag. Greyed out in the edit modal when global is OFF.
  auto_event_loop?: boolean
  // Event-coord PR-1: ISO cursor for `fetch_events_since` (PR-2).
  // NULL until the agent first drains its catch-up window.
  last_event_seen_at?: string | null
  // Event-coord PR-3: TRUE while the agent currently has an
  // in-flight `wait_for_events` long-poll call (i.e. while its
  // `g.lock_for(agent_id)` is held). Drives the "waiting" chip on
  // the Agents table and the "X agents currently in wait" count on
  // the Settings page. Always FALSE for the synthetic Admin row.
  wait_for_events_in_flight?: boolean
  // Phase 2 Wave 2b (plan §2e): role tier. Defaults to 'worker' for
  // any legacy row that pre-dates Wave 1a's migration. 'manager'
  // grants the manager-tier privileges Wave 3 enforces on tool calls.
  agent_role?: 'worker' | 'manager'
  // Wave 7 PR 2 — coordinator transition. Live MCP-session presence
  // derived server-side from `agent_mcp/core/session_registry.py`:
  // - `online: true` iff this agent's bearer is currently subscribed
  //   to a live /mcp stream (runtime queue attached).
  // - `last_mcp_connection`: ISO-UTC `last_seen_at` of the most
  //   recent MCP session this agent opened in the current backend
  //   process (NULL when no stream has ever been opened — the
  //   "Pending — paste snippet into claude .mcp.json" case).
  // Replaces spawn-lifecycle badges in the agents list. Old clients
  // that don't read these fields stay on the legacy `status` field
  // (which is still surfaced, just not the source of the badge).
  online?: boolean
  last_mcp_connection?: string | null
  // Agent self-service profile (migration 0018): the agent's own
  // free-text "what I do / how I work / what to ask me" description,
  // authored via the update_agent_profile MCP tool and curatable by the
  // operator via POST /api/agents/<id>/edit. NULL/'' = never set.
  profile?: string | null
  profile_updated_at?: string | null
  profile_updated_by?: string | null
  profile_reviewed_at?: string | null
  // ADR-0021 delivery transport liveness, reported per-agent by the
  // delivery transport worker (`_mcp_presence_for` in
  // app/routers/agents.py). Distinct from `agentPresence()` (which
  // reflects the live /mcp stream): this is the delivery side-channel's
  // own view of whether the agent is actively working / idle / dormant /
  // dead. NULL or absent when no delivery transport has reported for
  // this agent yet — the UI renders nothing in that case.
  transport_status?: TransportStatus | null
}

/** ADR-0021 delivery transport per-agent liveness. Reported by the
 *  delivery transport worker; separate axis from MCP-stream presence
 *  (`AgentPresence`). */
export type TransportStatus = 'working' | 'idle' | 'dormant' | 'dead'

/** Wave 7 PR 2 — derived presence kind for the agents list / detail
 *  panel. Sourced from the new `online` + `last_mcp_connection`
 *  fields plus the existing `status` column (terminated takes
 *  precedence so a terminated agent never shows "Online" even
 *  during the brief window before its bearer's MCP stream tears
 *  down on the wire).
 *
 *  - `terminated`: row's status column is `terminated`.
 *  - `online`: live MCP stream attached.
 *  - `offline`: registered + previously connected, no live stream.
 *  - `pending`: registered + never connected (the operator hasn't
 *    pasted the snippet into the user's claude yet). */
export type AgentPresence = 'online' | 'offline' | 'pending' | 'terminated'

export function agentPresence(agent: Agent): AgentPresence {
  if (agent.status === 'terminated') return 'terminated'
  if (agent.online) return 'online'
  if (agent.last_mcp_connection) return 'offline'
  return 'pending'
}

export interface AgentDetails {
  agent: Agent
  token?: string
  tasks?: Task[]
  actions?: Array<{
    timestamp: string
    action_type: string
    task_id?: string
    details?: unknown
  }>
}

/**
 * Agent-scoped client methods bound to a shared request core. Assembled
 * onto the composed client by `createApiClient()`.
 */
export function agentsApi(core: ApiClient) {
  const getAgent = (agentId: string): Promise<Agent> =>
    core.request<Agent>(`/agents/${agentId}`)

  return {
    getAgent,

    async getAgentDetails(agentId: string): Promise<AgentDetails> {
      // Get agent basic info
      const agent = await getAgent(agentId)

      // Get tokens
      // Wave 2 (cleanup-wave-2): the Admin pseudo-agent's auth_token used
      // to fall back to ``tokens.admin_token``; Wave 3 will drop that
      // field from the /api/tokens response entirely and Wave 4 deletes
      // the Admin pseudo-agent. The dashboard runs as admin via cookie
      // auth now (ADR-0003), so no admin-side bearer is needed in the UI.
      const tokens = await core.request<{
        agent_tokens: Array<{ agent_id: string; token: string }>
      }>('/tokens')
      const agentToken = tokens.agent_tokens.find(t => t.agent_id === agentId)?.token

      // Get node details which includes actions and related tasks
      const nodeDetails = await core.request<{
        id: string
        type: string
        data: Record<string, unknown>
        actions: Array<Record<string, unknown>>
        related?: Record<string, unknown>
      }>(`/node-details?node_id=${encodeURIComponent(`agent_${agentId}`)}`)

      // TY-4: narrow the untyped node-details record fields instead of a
      // bare trusted cast. `related.assigned_tasks` arrives as raw task
      // rows, so run them through the same TY-2 normalizer the list
      // endpoints use; `actions` is only used array-shaped.
      const rawAssigned = nodeDetails.related?.assigned_tasks
      const tasks = Array.isArray(rawAssigned)
        ? (rawAssigned as RawTask[]).map(normalizeTask)
        : []
      const actions = Array.isArray(nodeDetails.actions)
        ? (nodeDetails.actions as AgentDetails['actions'])
        : []

      return {
        agent: { ...agent, auth_token: agentToken },
        token: agentToken,
        tasks,
        actions,
      }
    },

    // Wave 7 PR 3 (coordinator transition): ``createAgent`` (POST
    // /api/agents — the spawn-via-tmux path) is gone. ``registerAgent``
    // below is the sole agent-creation surface.

    terminateAgent(agentId: string): Promise<{ success: boolean; message: string }> {
      // PR D: cookie auth, no body-token field.
      return core.request('/terminate-agent', {
        method: 'POST',
        body: JSON.stringify({ agent_id: agentId }),
      })
    },

    // Wave 7 coordinator transition. Register an agent identity
    // WITHOUT spawning a claude process: the backend mints a token + a
    // ready-to-paste .mcp.json snippet that the operator hands to the
    // user. The user owns their own claude session.
    registerAgent(data: {
      name: string
      role?: 'worker' | 'manager'
      // The frontend supplies these so the backend's snippet builder
      // doesn't have to derive them from request headers (which the
      // per-project backend gets after the router proxy strips Host).
      project_name?: string | null
      host?: string
      // ADR-0020: the client's external mount prefix (deriveMount()) —
      // "" at a root front door, "/agent-mcp" on the tailnet — so the
      // returned .mcp.json snippet URL matches the front door in use.
      mount_prefix?: string
    }): Promise<{
      success?: boolean
      message: string
      agent_id?: string
      agent_token?: string
      agent_role?: string
      mcp_snippet?: string
      project_name?: string | null
    }> {
      return core.request('/agents/register', {
        method: 'POST',
        body: JSON.stringify(data),
      })
    },

    // Restore + Purge for terminated agents. `restoreAgent` flips the
    // soft-delete back; `getPurgePreview` returns blast-radius counts
    // and samples for the confirmation modal; `purgeAgent` runs the
    // cascade tombstone + DELETE. PR D: cookie auth.
    restoreAgent(
      agentId: string,
    ): Promise<{ success: boolean; agent_id: string; status: string; message: string }> {
      return core.request(`/agents/${encodeURIComponent(agentId)}/restore`, {
        method: 'POST',
        body: JSON.stringify({}),
      })
    },

    // Disconnect / Reconnect — pause or resume an agent's monitoring loop
    // WITHOUT terminating it or revoking its token. Disconnect sets
    // auto_event_loop OFF (its wait_for_events starts returning
    // stop_listening with an operator-facing reason), wakes the parked
    // long-poll to deliver that now, and closes the live push stream so the
    // agent flips offline. Reconnect flips it back ON. The fleet variants
    // toggle the GLOBAL loop — "we're done for now" / "we're back".
    disconnectAgent(
      agentId: string,
    ): Promise<{ success: boolean; agent_id: string; closed_streams: number; message: string }> {
      return core.request(`/agents/${encodeURIComponent(agentId)}/disconnect`, {
        method: 'POST',
        body: JSON.stringify({}),
      })
    },

    reconnectAgent(
      agentId: string,
    ): Promise<{ success: boolean; agent_id: string; message: string }> {
      return core.request(`/agents/${encodeURIComponent(agentId)}/reconnect`, {
        method: 'POST',
        body: JSON.stringify({}),
      })
    },

    disconnectAllAgents(): Promise<{ success: boolean; closed_streams: number; message: string }> {
      return core.request('/agents/disconnect-all', {
        method: 'POST',
        body: JSON.stringify({}),
      })
    },

    reconnectAllAgents(): Promise<{ success: boolean; message: string }> {
      return core.request('/agents/reconnect-all', {
        method: 'POST',
        body: JSON.stringify({}),
      })
    },

    // editAgent updates the editable agent fields (color,
    // working_directory, aoe_session_id). PR D: cookie auth; backed by
    // POST /api/agents/<id>/edit. aoe_session_id is a 16-char lowercase
    // hex string (or empty to clear) used by the ADR-0021 delivery
    // bridge to route messages to this agent.
    editAgent(
      agentId: string,
      updates: {
        color?: string
        working_directory?: string
        aoe_session_id?: string
        // Event-coord PR-1: per-agent wake-loop toggle. Whitelisted on
        // the server side in /api/agents/<id>/edit.
        auto_event_loop?: boolean
        // Phase 2 Wave 2b (plan §2e): promote a worker to manager (or
        // demote). Whitelisted on the server side; the API-boundary
        // check 422s anything outside {'worker', 'manager'}.
        agent_role?: 'worker' | 'manager'
        // Agent self-description curation. Routed server-side through
        // review_profile so the updated_at/by/reviewed_at bookkeeping
        // matches the agent's own MCP self-edit. Empty string clears it.
        profile?: string
      },
    ): Promise<{ success: boolean; agent_id: string; updated: Record<string, unknown>; message: string }> {
      return core.request(
        `/agents/${encodeURIComponent(agentId)}/edit`,
        {
          method: 'POST',
          body: JSON.stringify(updates),
        },
      )
    },

    getPurgePreview(agentId: string): Promise<{
      agent_id: string
      status: string
      tombstone: string
      counts: {
        messages_sent: number
        messages_received: number
        tasks_created: number
        tasks_assigned: number
        agent_actions: number
      }
      samples: {
        messages_sent: Array<{ content: string; timestamp: string }>
        tasks_created: string[]
        tasks_assigned: string[]
      }
    }> {
      // PR D: cookie auth (was ?token=<admin> query param).
      return core.request(
        `/agents/${encodeURIComponent(agentId)}/purge-preview`,
      )
    },

    purgeAgent(agentId: string): Promise<{
      success: boolean
      agent_id: string
      tombstone: string
      counts: Record<string, number>
      message: string
    }> {
      // PR D: cookie auth.
      return core.request(
        `/agents/${encodeURIComponent(agentId)}?cascade=true`,
        {
          method: 'DELETE',
          body: JSON.stringify({}),
        },
      )
    },

    // Ad-hoc poke (operator/admin only) — push a one-shot directive to an
    // agent. Delivered immediately if it's listening, else queued urgent.
    pokeAgent(agentId: string, data: {
      prompt: string
      priority?: string
    }): Promise<{
      success: boolean
      poke_id: string
      agent_id: string
      // TRUE when the agent had a parked wait_for_events waiter and the
      // poke was delivered immediately; FALSE when it was queued as its
      // highest-priority next check-in. Drives the delivered-vs-queued
      // toast copy (see SendDirectiveModal).
      delivered: boolean
      message: string
    }> {
      return core.request(`/agents/${encodeURIComponent(agentId)}/directive`, {
        method: 'POST',
        body: JSON.stringify(data),
      })
    },
  }
}
