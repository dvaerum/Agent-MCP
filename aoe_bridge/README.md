# agent-mcp delivery bridge (AoE plugin)

The runtime side of [ADR-0021](../docs/adr/0021-delivery-transport.md). An
[Agent of Empires](https://github.com/agent-of-empires/agent-of-empires)
plugin worker that reverses the old `agent-mcp → AoE` push: **the bridge
reaches out to agent-mcp** and delivers into the session.

It lives in the Agent-MCP repo (deliberately — it's tightly coupled to
agent-mcp's `/delivery` API) but ships as an AoE plugin: the
`aoe-plugin.toml` manifest + this Python worker.

## What it does (per covered session)

1. **Subscribe** — holds an SSE connection to
   `…/api/<project>/delivery/stream` authed with **that session's own
   agent-mcp token** (the same token wired into the session's per-session
   MCP config — one token links tools + fallback).
2. **Report status up** — `POST …/delivery/status` with the session's
   `transport-status` (`working` / `idle` / `dormant` / `dead`), derived
   from AoE's `sessions.list`.
3. **Inject down** — on a delivery frame, render **skinny** nudge text
   (ids/subjects/status, never bodies) and inject it into the session via
   AoE's REST:
   - terminal (tmux) → `POST /api/sessions/<id>/send` `{message, revive}`
   - structured (ACP) → `POST /api/sessions/<id>/acp/prompt` `{prompt}`

   Both routes reach an arbitrary session the plugin didn't create — the
   only path that works for dashboard-started sessions in **both** modes.

The frame never marks anything read/done; agent-mcp's tunable per-project
policy decides *when* to fire and re-fires until the agent acts (ADR-0021).

## Layout

| File | Role | Tested |
|---|---|---|
| `render.py` | delivery frame → skinny nudge text | ✅ `tests/test_aoe_bridge.py` |
| `inject.py` | mode-aware AoE REST injection (both modes) | ✅ |
| `client.py` | agent-mcp `/delivery` SSE consumer + status POST | _next_ |
| `worker.py` | AoE plugin JSON-RPC stdio glue + per-session loops | _next_ |

## Configuration

Per session the bridge needs `(agent-mcp delivery endpoint, token)` — the
same pair wired into that session's per-session MCP entry (so a session can
even fall back to a *different* agent-mcp server). Read via the plugin's
`config.get`; the AoE serve token (for the localhost REST injection) comes
from the runtime environment.

## Status

Cores (`render`, `inject`) are implemented + unit-tested. The SSE client
and the AoE plugin JSON-RPC worker glue are the remaining integration
layer. The `aoe-plugin.toml` manifest is a **draft** — reconcile its
`[runtime]`/`capabilities` shape against the target AoE `aoe-plugin-api`
version before loading. Requires the **per-session MCP** AoE patch
(`feat/per-session-mcp` on the fork) for each session to authenticate as
its own agent-mcp identity.
