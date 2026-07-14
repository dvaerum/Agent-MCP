# ADR-0016: Separate project config from project memory

* Status: Accepted
* Date: 2026-07-14
* Plan: `prancy-napping-pie.md` Wave 11
* Supersedes: nothing
* Builds on: ADR-0013 (operator login), ADR-0015 (SSO via OIDC + proxy-header trust)

## Context

The per-project `project_context` table was doing double duty. It held
the **memory** store — agent-authored, RAG-indexed shared *knowledge*
(`view_project_context` / `update_project_context`, the Memories tab,
the RAG corpus) — AND the **operational config** store: the `config_*`
toggles and knobs that control how agent-mcp behaves for the project
(worker-policy toggles, the global event-loop flag, message retention,
the AoE integration keys). The two concerns differ on every axis:

| Axis | Memory (knowledge) | Settings (config) |
|---|---|---|
| Author | agents (+ operators) | operator/sysadmin only |
| Consumer | semantic RAG + humans | exact policy-gate reads |
| Shape | free-form text/JSON | typed toggles/knobs |
| In semantic search | yes | never |

The `config_` prefix was a poor-man's namespace papering over the fact
that these should be separate stores — and the conflation directly
caused **F009** (PR #487). Because credential-bearing config rows
(`config_aoe_bearer_token`) lived in the same table and read path as
agent knowledge, the redaction layer could not tell a boolean policy
toggle from a pasted credential and blanket-redacted the whole
`config_*` namespace ("ANY `config_*` key is secret"). For a cookie
operator — whose tier the per-project backend conservatively treats as
non-confirmed — that turned every Settings toggle into `[redacted]`,
which the dashboard's `coerceBool('[redacted]', default)` collapsed to
the default: a toggle stored `true` rendered OFF. The
`_NON_SECRET_POLICY_KEYS` carve-out in `project_context_tools.py` was
the band-aid on top of that band-aid.

## Decision

Terminology, stated explicitly (this ADR is the canonical home):

* **memory** = agent-authored shared knowledge. Lives in
  `project_context`, is RAG-indexed, surfaces on the Memories tab.
* **settings** = operational config (`config_*` keys). Lives in the new
  `project_settings` table, is NEVER RAG-indexed, surfaces on the
  Settings tab. Operator-only access model.

Concretely:

1. **Dedicated `project_settings` table** with the SAME
   JSON-in-TEXT column shape as `project_context` (`context_key` PK,
   `value` NOT NULL, `description`, `created_at`/`created_by`,
   `updated_at`/`updated_by`), so the liberal-coercion read helpers
   (`tools/access.py::_get_config_bool` / `_get_config_int`,
   `features/aoe_notify.py::_read_ctx`) cut over by changing only the
   table name in their `SELECT`. Typed columns were considered and
   rejected — the loose JSON contract is the tested behaviour and a
   drop-in cutover is lower-risk.
2. **HARD CUTOVER migration**
   (`0016_move_config_to_project_settings`): one transaction copies
   every `config_*` row (`LIKE 'config\_%' ESCAPE '\'`) into
   `project_settings` AND deletes it from `project_context`. No
   dual-store grace period (contrast 0009's leave-in-place): leaving
   copies behind would keep the F009 redaction surface alive and let
   the stores drift.
3. **Operator-only, non-RAG access model.** New small/deep MCP tool
   family (`tools/project_settings_tools.py`:
   `view_project_settings` / `update_project_settings` /
   `delete_project_settings`) gated on the `system.config.write`
   capability; the REST surface (`GET /api/settings-data`,
   `POST/PUT/DELETE /api/settings...`) dispatches those tools — one
   enforcement path, mirroring the memories router. None of the
   context tools' machinery (creator-ownership matrix, backups, bulk
   paths) carries over: settings never needed it.
4. **`config_aoe_*` stays sysadmin-write-gated.** The R8-F1 tier-gate
   (machine-level outbound integration target → SSRF / bearer-exfil
   surface) moves onto the settings write path unchanged.
5. **Secrets are a literal set owned by the settings module.** Exactly
   two keys are genuinely secret — `config_aoe_bearer_token` and
   `config_aoe_bearer_token_file` — declared as a frozenset
   (`_SECRET_SETTING_KEYS`) in `project_settings_tools.py`. The store
   knows its own schema; no more prefix heuristic. They redact to
   `[redacted]` for non-confirmed tiers; every other settings row
   returns its REAL value to any admitted operator (blanket redaction
   of the store is exactly the F009 bug).
6. **The memory write path rejects `config_*` for EVERYONE** — admin
   included (`PermissionDenied: config_* keys moved to the project
   settings store (ADR-0016); use update_project_settings`). Config
   can never be smuggled back into the RAG-indexed store.
7. **Wake parity carries over** (BL-R14-1): settings writes AND deletes
   fire the same post-write wake seam the context tools fired for these
   keys (`config_allow_worker_*` → `tools/list_changed`;
   `config_auto_event_loop_global` → `wake_all_for_flag_recheck`).

## Consequences

### Positive

* The F009 bug class is structurally gone: policy toggles and
  credentials no longer share a store, so the reader never has to guess
  which is which.
* RAG no longer needs a config skip — config rows simply never enter
  the indexed table.
* Settings and Memories UIs are cleanly separated; config rows stop
  polluting the Memories tab automatically.
* The settings tool family is ~1/6 the size of the context tools —
  no ownership matrix, no backups, no bulk machinery to audit.

### Negative

* Forward-only migration: the DELETE is irreversible in production
  (`downgrade()` is a best-effort dev-only copy-back).
* One more table + repository + tool family + REST surface to maintain.

### Risks

* **A missed config reader still pointing at `project_context`
  silently reads defaults.** Guard: the grep sweep in Verification
  (`grep -rn "FROM project_context"`) must show zero config-key
  readers left; the live-gate tests
  (`test_worker_to_worker_gate_reads_new_store_live`,
  `test_get_config_bool_reads_project_settings`) pin the two canonical
  seams against the new table.
* External scripts that seeded config via `/api/memories` break loudly
  (403 with the ADR pointer) — deliberate: a loud category error beats
  a silently ignored row.

## Follow-ups

* **PR 1 (done — pure subtraction):** delete the now-dead config
  branches of `is_secret_key` — `_CONFIG_KEY_RE`'s blanket rule, the
  `_NON_SECRET_POLICY_KEYS` F009 carve-out — and simplify the RAG
  indexing comment; migrate the read-side redaction tests that still
  pin config-key behaviour on legacy seeded rows. The
  secret-suffix-vocabulary redaction for NON-config knowledge keys
  (`openai_api_key`, `db_password`, …) and the
  `_value_has_embedded_secret` backstop stay.
* Consider a typed settings schema (declared key → type/default table)
  once the JSON-in-TEXT contract becomes limiting.

## Verification

* `tests/test_wave11_project_settings_store.py` — the migration
  (hard-cutover, byte-identical copy, escape semantics, no-clobber
  re-run), the repointed read seams, the F009 regression at
  `GET /api/settings-data` (real toggle values for non-confirmed
  operators; the two secret keys masked), the write gates
  (`system.config.write`, AoE sysadmin tier, non-config rejection),
  the everyone-rejection on the context write path, wake parity on
  settings write/delete (MCP + REST), worker `tools/list` hiding, and
  the live worker→worker gate reading the migrated store.
* Retargeted invariant suites:
  `tests/test_arch_r6_p_context_wake_and_authz.py` (wake-parity matrix
  on the settings surfaces), `tests/test_sec_r14_context_notify_parity.py`
  (REST-vs-MCP parity), `tests/test_r8_f1_aoe_config_sysadmin_only.py`
  (AoE tier-gate at its new home),
  `tests/test_sec_composition_policy_readback.py` (settings-data
  readback + config rows absent from the composition reads).
* `nix/tests/event-driven-coord.nix` seeds its toggles into
  `project_settings` (the `vm-event-driven-coord` CI check exercises
  the live gates against the new store end-to-end).
