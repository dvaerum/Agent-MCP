# ADR-0018: Settings-schema registry as the single source of truth

* Status: Accepted
* Date: 2026-07-15
* Plan: `prancy-napping-pie.md` — "Settings page — best-long-term redesign"
* Supersedes: nothing
* Builds on: ADR-0016 (separate project config from project memory)

## Context

Every per-project `config_*` setting had its schema declared in **two**
places. The **frontend** (`settings-dashboard.tsx`) hardcoded the
`POLICIES` / `AOE_FIELDS` arrays — each entry's default, group, human
title/description, and (implicitly) its tier. The **backend** declared
the same facts, partially and scattered: `tools/access._TOGGLE_DEFAULTS`
(only 4 of the 6 bool keys), a `default=` literal at every
`_get_config_bool` / `_get_config_int` / `@requires_policy` call site,
and `features/aoe_notify.py`'s own `DEFAULT_BASE_URL` / `DEFAULT_TEMPLATE`
/ `DEFAULT_TIMEOUT_MS` constants.

Two owners of the same fact drift. The UI's "(using default: …)" hint
reads its default from the frontend copy, which can silently disagree
with what the backend actually resolves. A concrete symptom of the
missing single source: `config_aoe_notify_enabled` is **sysadmin-only to
write** (matched by `_CONFIG_AOE_KEY_RE` in the settings write gate) yet
the frontend rendered it as a plain operator toggle in the "Worker
permissions" card — so a non-sysadmin operator's toggle silently 403s
with no UI signal (a mis-tier that only a data-driven schema can prevent).

## Decision

A single **backend registry** owns every setting's schema, and the
frontend consumes it.

1. **`agent_mcp/core/settings_schema.py`** — a frozen `SettingSpec`
   (`key`, `type` ∈ `bool|int|string|secret`, `default`, `tier` ∈
   `operator|sysadmin`, `group` ∈
   `worker_permissions|event_loop|retention|aoe`, `title`, `description`,
   `widget`) and `SETTINGS_SCHEMA`, an ordered tuple of the 12 specs.
   Defaults equal today's values **byte-identical** — no behaviour
   change. The human `title`/`description` copy is lifted verbatim from
   the former frontend declarations, so the backend now owns it. The AoE
   `DEFAULT_*` constants move here (their single owner) and
   `features/aoe_notify.py` imports them back.

2. **Every default reader resolves through the registry.**
   `_TOGGLE_DEFAULTS` is derived from the schema's bool specs (now all 6,
   fixing the missing-2 gap); `_get_config_bool` / `_get_config_int` take
   an **optional** `default` and fall back to `default_for(key)`;
   `@requires_policy`'s `default=` is optional with the same fallback.
   The per-call-site `default=` literals are dropped. No reader keeps a
   hardcoded default.

3. **`GET /api/settings-schema`** (on `app/routers/settings.py`) returns
   `{schema: [spec-as-JSON…], caller: {sysadmin, confirmed_operator}}`.
   Gated to a confirmed operator tier (403 otherwise), mirroring the
   `/api/tokens` pattern. The `caller` block lets the frontend disable
   sysadmin-tier widgets for a non-sysadmin operator without a second
   round-trip.

4. **HYBRID tier-enforcement.** The proven `_CONFIG_AOE_KEY_RE` sysadmin
   gate in `tools/project_settings_tools.py` stays the **enforcer** —
   safe-by-default: any future `config_aoe_*` key is sysadmin-gated
   automatically, and the credential-bearing / SSRF-sensitive AoE keys
   keep their live protection. `SettingSpec.tier` drives the **UI** and
   the agreement guarantee; it never relocates the live gate. A CI
   invariant test asserts `(spec.tier == 'sysadmin') == bool(regex.match)`
   for every key, so the two can never silently drift.

The write gate (`_CONFIG_AOE_KEY_RE` + `_deny_non_sysadmin_aoe`) and the
secret classification (`_SECRET_SETTING_KEYS`, now bound to the schema's
`SECRET_SETTING_KEYS`) are otherwise unchanged.

## Consequences

### Positive

* One owner per fact: a new setting is a one-line schema addition; its
  default, group, tier, copy, and widget flow to the UI automatically.
* The FE/BE default-drift class is structurally gone — the frontend can
  no longer disagree with the backend about a default.
* Data-driven grouping + tier-gating fixes the
  `config_aoe_notify_enabled` mis-tier (it renders in the AoE group,
  disabled for non-sysadmin operators — no more silent 403).
* The tier↔gate invariant test converts a latent drift into a build
  failure.

### Negative

* One more module + REST endpoint to maintain, and the frontend gains a
  fetch dependency on it (PR 2).

### Risks

* **A new setting added to a reader but not to the schema** resolves no
  default — `default_for` raises `KeyError` loudly rather than guessing,
  and the `KNOWN_SETTING_KEYS` completeness test catches the omission.
* **Tier drift** between the schema and the live regex — closed by the
  CI invariant test.

## Follow-ups

* **PR 2 (frontend):** consume `GET /api/settings-schema`, render via a
  type→widget registry, group data-driven, disable sysadmin widgets for
  non-sysadmin callers, header/toast parity. Depends on this PR's
  endpoint.

## Verification

* `tests/test_settings_schema.py` — the golden-default table (12 keys
  byte-identical, the no-behaviour-change proof), the tier↔`_CONFIG_AOE_KEY_RE`
  agreement invariant, `KNOWN_SETTING_KEYS` completeness, the
  `SECRET_SETTING_KEYS` derivation, and the `GET /api/settings-schema`
  endpoint (confirmed-operator 200 with 12 rows, non-confirmed 403,
  unauthenticated 401, sysadmin caller block).
* The full existing suite is the repoint's guard: any worker-policy /
  retention / AoE gate that flipped behaviour would fail its existing
  test — none do.
