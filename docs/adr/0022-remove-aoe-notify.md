# ADR 0022: Remove the `aoe_notify` side-channel and its `config_aoe_*` settings

**Status**: Accepted, 2026-08-10. Completes the retirement promised by
ADR-0021 (delivery transport), whose Verification step required that
`aoe_notify` "is disabled and then removed; no `config_aoe_*` reader
remains."
**Date**: 2026-08-10
**Builds on**: ADR-0021 (delivery transport — the replacement),
ADR-0016 (config in `project_settings`), ADR-0017 (protect by
authorization, not by guessing content), ADR-0018 (settings-schema
registry).

## Context

`features/aoe_notify.py` was the spawn-era side-channel: when
`send_agent_message` stored a message it *also* POSTed an out-of-band
wake-up to a local Agents-of-Empires (AoE) instance
(`POST {aoe}/api/sessions/<id>/send`) so the recipient's tmux pane
noticed the message between polls. It was off by default and configured
by five per-project `config_aoe_*` settings (`notify_enabled`,
`base_url`, `bearer_token`(+`_file`), `notify_template`, `timeout_ms`),
which — because an operator who set `config_aoe_base_url` could aim the
shared host's outbound request at an internal/link-local/metadata
address (SSRF) and exfiltrate the bearer — carried a bespoke
**sysadmin-only** write gate (`_CONFIG_AOE_KEY_RE` /
`_deny_non_sysadmin_aoe_config`, the R8-F1 / R9-F2 hardening) on top of
the operator-writable `config_*` namespace.

Two things made it dead weight:

1. **Its premise died with Wave 7.** agent-mcp no longer owns or spawns
   the recipient's Claude session, so there is no tmux pane for it to
   poke. `send_agent_message` already stores every message for pickup
   via `wait_for_events` / `get_agent_messages`.
2. **ADR-0021 superseded it with the right shape.** The delivery
   transport is a per-worker, bearer-authed SSE channel the runtime
   (the AoE bridge) opens *to* agent-mcp; the dependency points the
   correct way (the runtime reaches into agent-mcp, not the reverse),
   it is skinny-by-default (never ships the body), and it is driven by a
   tunable per-project policy. That channel is deployed and consumed.

## Decision

**Delete `aoe_notify` entirely** and everything that existed only to
serve it:

- `features/aoe_notify.py` (the notifier, the AoE HTTP client, the
  template validator, and `check_health`).
- The `asyncio.create_task(notify_aoe(...))` call site in
  `send_agent_message_tool_impl` (the tool keeps its full authorization
  gate and message store — only the fire-and-forget AoE hop is gone).
- The `GET /api/aoe/health` admin probe and its response sanitiser.
- The five (six incl. `bearer_token_file`) `config_aoe_*` specs in the
  settings-schema registry and the `aoe` settings group.
- The AoE-specific **sysadmin** write gate (`_CONFIG_AOE_KEY_RE` +
  `_deny_non_sysadmin_aoe_config`). With no `config_aoe_*` key left,
  every remaining `config_*` setting is uniformly operator-tier; the
  generic `system.config.write` cap gate is retained.
- The dashboard "AoE integration" group card, the AoE health card, and
  the `aoeHealth()` API method.
- A data migration (`0024_drop_config_aoe_settings`) purges any leftover
  `config_aoe_*` rows from `project_settings` (idempotent; table-absent
  safe).

**Explicitly kept:** the per-agent `agents.aoe_session_id` column and
its edit surface. It is a *different* thing — the delivery-bridge
per-agent binding under ADR-0021 — and is unrelated to the removed
notify feature.

The generic settings-store secret machinery (`_SECRET_SETTING_KEYS`
derived from the schema's `type == "secret"` specs, and
`redact_settings_row`) is retained intact even though `config_aoe_*`
was its only live instance: a future secret-typed setting inherits the
redaction automatically.

## Consequences

**Positive**
- Removes an outbound HTTP dependency and the title→session-id
  heuristic; agent-mcp no longer reaches out to AoE at all.
- **Permanently retires the R8-F1 / R9-F2 webhook-SSRF class**: there is
  no operator-settable outbound URL on the message path any more, so the
  bespoke sysadmin gate that guarded it is no longer needed.
- One notification mechanism (the delivery transport), less coupling and
  less config surface.

**Negative / risks**
- The redaction machinery now has no live secret key (its coverage is
  preserved by tests against a synthetic secret key). Adding a future
  secret setting is the only way it re-acquires a real instance.
- Any deployment that still had `config_aoe_notify_enabled = true`
  loses that push; the delivery transport (ADR-0021) is the supported
  replacement and must be enabled instead.

## Verification

- Grep is clean: no `config_aoe_*`, `aoe_notify`, or `notify_aoe`
  reader remains outside migrations / this ADR / the changelog.
- `send_agent_message` still stores and gates messages (the OBS6 shared
  gate is unaffected) and no longer calls any AoE path.
- The settings schema drops from 32 to 26 specs; `SECRET_SETTING_KEYS`
  is empty; the migration purges pre-existing `config_aoe_*` rows.
- The redaction machinery stays under test via a synthetic secret key.
