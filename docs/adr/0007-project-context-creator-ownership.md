# ADR-0007: `project_context` entries are owned by their creator; `config_*` keys are admin-only regardless

## Status

Accepted. Still the live model for the ORM-era `project_context`
table's write authorization; ADR-0016 (separate project config from
project memory) later split that one table into two, but the
creator-ownership + `config_*`-admin-only-exception rules this ADR
established were preserved across that split, not revisited.

**Provenance**: originally filed in the `home-manager-config` deploy
repo's `common/user/agent-mcp/docs/adr/` (as
`0007-project-context-creator-ownership.md`). Moved here 2026-09-06 —
see ADR-0003's own provenance note for why. The `prancy-napping-pie.md`
plan-file citation below is to a different, now-deleted ephemeral plan
file — see ADR-0005's own note on this.

## Original decision (verbatim, unedited)

Before Phase 7b, `project_context` writes were not admin-only — any
worker bearer token could overwrite any key, including
`config_admin_token` and the ADR-0004 policy toggles. That's a
privilege-escalation path: a worker rewrites `config_admin_token` to a
value it controls, reads it back via the `view_project_context` leak,
then acts as admin. Making all writes admin-only was rejected —
workers genuinely need to contribute shared knowledge (design notes,
findings, references) to `project_context`, which is one of the main
reasons it exists.

Decided in plan `prancy-napping-pie.md` Q7.5. Decision (Phase 7b, on
top of the ORM migration from ADR-0005): each `project_context` row
gets a `created_by` column (worker or admin agent id) and a
`created_at` timestamp; `last_updated` is renamed to `updated_at` for
consistency with `tasks`/`agents`. Authorization: admin can do
anything; a worker can create new entries, and can update or delete
only entries whose `created_by` matches its own agent id. The
`config_*` namespace is a hardcoded exception — admin-only for all
writes regardless of creator, because otherwise a worker that happens
to create `config_admin_token` first would own it forever.

Alembic migration `0002_project_context_ownership.py` backfills
existing rows with `created_by = updated_by`, `created_at = updated_at`
as a best guess — real provenance for pre-existing rows is lost. The
dashboard surfaces `created_by`/`created_at` in the memory detail
modal for audit. Errors distinguish the two failure modes so workers
can self-recover: `"Unauthorized: config_* keys are admin-only"` vs
`"Unauthorized: key 'X' was created by 'Y'"`.

The cost is the schema migration, an ownership check on every write,
and the `config_*` invariant living as a hardcoded prefix rule rather
than a row-level flag — accepted because the prefix rule is short,
obvious, and impossible to bypass by data manipulation.
