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

**One base, everything derived.** There is exactly one agent-mcp URL to
configure — `agent_mcp_base`, the **bare** router address you reach it at (on the
same host `http://127.0.0.1:1337`, no `/agent-mcp` — that prefix is a
reverse-proxy concern, per ADR-0020, not part of this base). Every per-session
URL is derived from it plus the row's `project`:

```text
delivery = <agent_mcp_base>/api/<project>   (SSE /delivery/stream + status POST)
mcp      = <agent_mcp_base>/mcp/<project>   (injected per-session MCP server)
```

So a covered-session row carries **only identity** — never a URL. The **one
token drives both surfaces**: an agent-mcp agent token (minted via
`register_agent`) authenticates the delivery stream AND the MCP transport. The
bridge wires delivery (always) and MCP (when `expose_mcp`, over
`session.mcp.set`) — no separate provisioning step.

The AoE plugin host gives a worker **no** way to read another component's
per-session MCP config, so the mapping is sourced from **this plugin's own
settings**, read over the `config.get` host RPC (which only ever returns this
plugin's own table). The `sessions` object-list holds one row per covered
session:

| Field | Meaning |
|---|---|
| `session_id` | **(required)** AoE session id (matches `sessions.list[].id`; stable across respawn). |
| `token` | **(required)** The session's agent-mcp bearer. Authenticates both the delivery stream and the injected MCP server. |
| `project` | **(required)** The agent-mcp project this session acts as. Appended to `agent_mcp_base` for both its `/api/<project>` (delivery) and `/mcp/<project>` (MCP) URLs. |
| `expose_mcp` | Also inject agent-mcp's tools into this session (default `true`). Delivery fires regardless; this gates only the MCP-tools half. First enable respawns the session once (transcript-preserving) to load the set. |
| `mode` | `auto` \| `terminal` \| `structured`. |

Global settings: `agent_mcp_base` (the one above), `aoe_base` (the **AoE-side**
REST the bridge injects into — a *different* service), `aoe_token` (only if AoE
runs with auth), `status_interval_secs`, `enabled`. See `aoe-plugin.toml`.

The bridge re-asserts each covered session's MCP layer every reconcile; the AoE
host makes an unchanged set a no-op (no respawn), so re-assertion — and a bridge
restart — is free. A session is interrupted at most once, the first time it is
provisioned.

### Assumptions

- **`session_id` == `sessions.list[].id`.** The bridge only opens a stream for a
  configured session while it is present in `sessions.list`; a configured
  session that has left the list is reported `dead` and its stream dropped.
- **`agent_mcp_base` is the bare router address** (no reverse-proxy prefix). The
  bridge appends `/api/<project>` (+ `/delivery/stream|status`) and
  `/mcp/<project>`.
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

- initiates host RPCs (`config.get`, `session.mcp.set`, `ui.state.set`,
  `ui.notify`) and reads their responses, and
- answers host-initiated notifications: `plugin.settings.changed` (triggers an
  immediate re-reconcile) and `plugin.command.invoke` (a `[[commands]]` entry).

**`plugin.command.invoke` carries the command in `params.command`, not in the
method name.** The host names every command with the same method and answers the
operator `202 {"ok": true}` before forwarding a reply-less notification, so a
command can never return a value — it acts, then re-pushes UI state.

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
| `observe.rs` | leveled + redacted diagnostics, durable log file, rotation | ✅ |
| `status.rs` | observable state, notify gating, settings-page renderer | ✅ |
| `bridge.rs` | async orchestrator: reconcile, SSE client, status reporter, injector, publisher | — |
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

Run the unit tests:

```sh
cargo test
```

## Observability

Two surfaces, because the host gives a runtime worker exactly two channels.

### The status page (live state)

The manifest declares the global `settings-page` UI slot, so the AoE dashboard
mounts **Settings → agent-mcp Delivery Bridge**. The worker re-pushes it after
every reconcile, on every stream/inject state change, and at least every 30s.
It answers, per covered session:

| Question | Where |
|---|---|
| is this session covered, and live? | the session's `live` / `transport-status` rows |
| is the delivery stream up? | `stream`: connected / reconnecting (attempt N) / stopped |
| did a frame arrive? | `frames received`: count, age, and the frame's `reason` |
| did the inject work? | `injects`: ok/failed counts, and `last inject` with the HTTP status |
| **why did it fail?** | `last inject` carries AoE's error body, e.g. `HTTP 400: acp_mode_unsupported` |
| is agent-mcp's MCP wired in? | `agent-mcp tools`: injected / pending |

Failing sessions sort first and render expanded. The overall verdict
distinguishes *disabled*, *not configured*, *no live sessions*, *healthy* and
*degraded* — an empty page always says which.

The `agent-mcp Delivery: status` command forces an immediate repaint and toasts
a one-line summary. It cannot return anything (see *Protocol model*), so the
toast is the answer.

Injection failures also raise a **danger toast**, rate-limited to one per session
per 15 minutes while it keeps failing, plus one success toast on recovery.

### The log file (history)

The page shows *state*; the log holds *history*. The host redirects worker stderr
into `<app_dir>/plugin-workers/<random-uuid>.log`, a file with a per-spawn
unguessable name that nothing reads and that never reaches the journal — so
`journalctl --user -u aoe-web.service | grep aoe-bridge` returns nothing, and
never could. The bridge therefore writes its own log at a fixed path:

```sh
tail -f ~/.local/state/aoe-bridge/worker.log     # or $XDG_STATE_HOME/aoe-bridge/
```

The exact path in force is shown under *Diagnostics* on the status page.

| Env | Default | Meaning |
|---|---|---|
| `AOE_BRIDGE_LOG_LEVEL` | `info` | `error` / `warn` / `info` / `debug` |
| `AOE_BRIDGE_LOG_FILE` | `$XDG_STATE_HOME/aoe-bridge/worker.log` | explicit path; set **empty** to disable file logging |

Rotates at 2 MiB keeping one `.1` generation. Levels are chosen for volume:
per-frame and per-inject traffic is `debug`, state transitions (coverage gained
or lost, stream connected, MCP injected) are `info`, retrying failures are
`warn`, and a refused injection is `error`.

**Nothing logged or displayed carries a token or a message body.** Every line
passes through a redactor that scrubs `Bearer …` and `token=…`-shaped values,
and the observable state model has no field a message subject, sender or task
title could occupy — only ids, counts, timestamps, HTTP codes and the frame's
`reason` enum.

## Notes

- **stdout is the protocol channel** — a non-JSON line there makes the host stop
  reading the worker. All diagnostics go through `observe.rs`.
- TLS uses `rustls` (no OpenSSL system dependency). Swap the reqwest feature to
  `native-tls` in `Cargo.toml` if the host toolchain prefers it.
- A transient SSE drop is treated as *temporarily gone*, not a session end: the
  stream reconnects with capped backoff and the policy re-fires on reconnect
  (self-healing, ADR-0021). No delivered-state, no ack — the condition on the
  agent-mcp side is the source of truth.
