# agent-mcp Delivery Bridge (AoE plugin worker)

A native Rust [Agent of Empires](https://github.com/agent-of-empires) plugin
worker that implements the **runtime side** of agent-mcp's per-worker
*delivery* fallback channel ([ADR-0021](../docs/adr/0021-delivery-transport.md)).

agent-mcp owns the **policy** — when to nudge a session that has stopped calling
`wait_for_events`. This bridge owns **delivery**: it reaches *out* to agent-mcp,
subscribes to each covered session's delivery SSE stream, reports that session's
`transport-status` back up, and injects the skinny frames it receives into the
AoE-run Claude session through AoE's localhost REST. agent-mcp needs no
knowledge of AoE; the dependency points from the runtime into agent-mcp.

## What it does, per covered session

1. **SSE subscribe (down).** `GET {endpoint}/delivery/stream` with
   `Authorization: Bearer <session token>`, `Accept: text/event-stream`. Frames
   arrive as `data: <json>` lines:

   ```json
   {"type":"delivery","reason":"unread_messages|unfinished_tasks|unassigned_tasks",
    "unread_count":N,"task_count":N,
    "unread_messages":[{"message_id","sender_id","subject"}...],
    "open_tasks":[{"task_id","title","status"}...],
    "unassigned_count":N?}
   ```

2. **Status report (up).** `POST {endpoint}/delivery/status` with
   `{"status":"working"|"idle"|"dormant"|"dead"}`, same bearer, on the
   reconcile cadence. The status is derived (best-effort) from the live
   `sessions.list` record's status.

3. **Inject (skinny).** Each frame is rendered to **ids/subjects/status only —
   never a message body** (bodies/secrets stay out of the pane) and pointed at
   `get_agent_messages` / `view_tasks`. The rendered text is injected via AoE's
   REST, mode-aware:
   - **terminal** → `POST {aoe_base}/api/sessions/<id>/send`
     `{"message": <text>, "revive": true}`
   - **structured** → `POST {aoe_base}/api/sessions/<id>/acp/prompt`
     `{"text": <text>}`

   > Note: the ACP prompt route's real request field is `text` (verified against
   > `src/acp/protocol.rs::PromptRequest`), not `prompt`. This bridge sends
   > `text`.

## The session → route mapping

Per covered session the bridge needs an agent-mcp **delivery endpoint** and a
per-session **bearer token**; optionally it also injects agent-mcp's **tools**
into the session as a per-session MCP server. The **one token drives both
surfaces** — an agent-mcp agent token authenticates the delivery stream
(`/delivery/stream`) AND the MCP transport (`/mcp/<project>`) — so the operator
supplies it once (minted via agent-mcp's `register_agent`), and the bridge wires
delivery (always) and MCP (when `expose_mcp`, over `session.mcp.set`).

The AoE plugin host gives a worker **no** way to read another component's
per-session MCP config, so the mapping is sourced from **this plugin's own
settings**, read over the `config.get` host RPC (which only ever returns this
plugin's own table). The `sessions` object-list setting holds one row per
covered session:

| Field | Meaning |
|---|---|
| `session_id` | AoE session id (matches `sessions.list[].id`; stable across respawn). |
| `token` | The session's agent-mcp bearer. Authenticates the delivery stream AND the injected MCP server. Empty ⇒ target runs without auth (no `Authorization` header sent). |
| `endpoint` | agent-mcp delivery mount base, e.g. `https://host/api/<project>`. Blank ⇒ `default_endpoint`. `/delivery/stream|status` appended. |
| `project` | agent-mcp project; appended to the global `mcp_base` → `<mcp_base>/<project>` for the injected MCP url. Blank ⇒ trailing path segment of the resolved `endpoint`. |
| `expose_mcp` | Also inject agent-mcp's tools into this session (default `true`). Delivery fires regardless; this gates only the MCP-tools half. First enable respawns the session once (transcript-preserving) to load the set. |
| `mode` | `auto` \| `terminal` \| `structured`. |

Global settings: `enabled`, `aoe_base`, `aoe_token`, `default_endpoint`,
`mcp_base` (shared MCP-transport mount, e.g. `https://host/agent-mcp/mcp`),
`status_interval_secs`. See `aoe-plugin.toml`.

The bridge re-asserts each covered session's MCP layer every reconcile; the AoE
host makes an unchanged set a no-op (no respawn), so re-assertion — and a bridge
restart — is free. A session is interrupted at most once, the first time it is
provisioned.

### Assumptions

- **`session_id` == `sessions.list[].id`.** The bridge only opens a stream for a
  configured session while it is present in `sessions.list`; a configured
  session that has left the list is reported `dead` and its stream dropped.
- **`endpoint` is the project mount base.** `/delivery/stream` and
  `/delivery/status` are appended.
- **`token`** authenticates both the SSE subscribe and the status POST; it is
  the session's agent-mcp worker bearer.
- **Mode `auto` is best-effort.** `sessions.list` exposes no definitive
  terminal-vs-structured flag today (only `tool` + a debug-formatted `status`),
  so `auto` infers from those strings via the rule *contains
  structured/acp/composer/cityhall ⇒ structured, else terminal*. **Set `mode`
  explicitly to `structured`** for ACP / CityHall / composer sessions to be
  safe. If `sessions.list` later exposes a real mode field, wire it into
  `resolve_routes` and the inference tightens automatically.
- **`transport-status` mapping** from AoE's session status is likewise a
  keyword match (`map_transport_status`); it only *gates nudge timing*, never
  authorizes anything, so an occasional misclassification is low-blast-radius.
- **The operator populates `sessions`.** Each row's `token` is minted via
  agent-mcp's `register_agent`; the bridge then wires both delivery and (if
  `expose_mcp`) the per-session MCP itself — no separate provisioning step. On a
  nix deploy where config.toml is writable (e.g. via `nix-it-in`), rows are added
  at runtime so tokens stay out of git.

## Protocol model

The worker is a JSON-RPC 2.0 **peer** over newline-delimited JSON on stdio.
Over the one pipe it both:

- initiates host RPCs (`sessions.list`, `config.get`, `ui.notify`) and reads
  their responses, and
- answers host-initiated requests (a `status` command) and notifications
  (`plugin.settings.changed`, which triggers an immediate re-reconcile).

A single stdout writer task serialises all outgoing lines. The reader demuxes on
the presence of a `method` field: `method` present ⇒ host request/notification;
absent ⇒ a response routed back to the parked caller by id. The worker exits on
stdin EOF (host closed the pipe).

## Module layout

| Module | Responsibility | Pure/tested |
|---|---|---|
| `protocol.rs` | JSON-RPC wire types, parse, line building | ✅ |
| `plugin.rs` | stdio loop: writer, host-RPC client, reader/demux | command dispatch tested |
| `config.rs` | settings + `session → route` resolver (`resolve_routes`, `parse_session_entries`) | ✅ |
| `mode.rs` | `normalize_mode`, `map_transport_status` | ✅ |
| `render.rs` | skinny frame renderer (`render_skinny`) | ✅ |
| `inject.rs` | REST request building, SSE data extraction | ✅ |
| `bridge.rs` | async orchestrator: reconcile, SSE client, status reporter, injector | — |
| `main.rs` | wiring | — |

## Build & install

The plugin builds from source at install; `cargo` must be on the interactive
shell PATH.

```sh
aoe plugin install ./aoe-bridge
aoe plugin list
```

The build step compiles into `.aoe-build/target/` (excluded from the plugin
integrity hash) and the host launches the plugin-relative binary
`.aoe-build/target/release/aoe-bridge-worker`.

Drive the worker by hand to smoke-test the protocol:

```sh
echo '{"jsonrpc":"2.0","id":1,"method":"plugin.dev.dvaerum.agent-mcp-delivery.status","params":{}}' \
  | .aoe-build/target/release/aoe-bridge-worker
# => {"jsonrpc":"2.0","id":1,"result":{"ok":true,"message":"agent-mcp delivery bridge running"}}
```

Run the unit tests:

```sh
cargo test
```

## Notes

- **stdout is the protocol channel** — all diagnostics go to stderr (the host
  drains a worker's stderr to its worker log).
- TLS uses `rustls` (no OpenSSL system dependency). Swap the reqwest feature to
  `native-tls` in `Cargo.toml` if the host toolchain prefers it.
- A transient SSE drop is treated as *temporarily gone*, not a session end: the
  stream reconnects with capped backoff and the policy re-fires on reconnect
  (self-healing, ADR-0021). No delivered-state, no ack — the condition on the
  agent-mcp side is the source of truth.
