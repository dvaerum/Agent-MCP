# ADR-0005: Adopt SQLAlchemy 2.0 ORM + Alembic, migrate table-by-table

## Status

**Superseded** — the CoNexus Python→Rust migration (2026-08 onward,
see `docs/proposals/security-authz-architecture-hardening.md` and the
Rust workspace under `rust/`) replaced this entire layer with
`rusqlite` + hand-rolled SQL (no ORM, no Alembic-equivalent — schema
is Rust-source-of-truth in `conexus-db::schema`, with Alembic staying
authoritative only until the Python side is fully decommissioned).
Recorded here as history, not deleted: it explains the raw-`sqlite3`
→ ORM→ Rust lineage the current schema-ownership model descends from.

**Provenance**: originally filed in the `home-manager-config` deploy
repo's `common/user/agent-mcp/docs/adr/` (as
`0005-sqlalchemy-orm-incremental-adoption.md`). Moved here 2026-09-06
— see ADR-0003's own provenance note for why. The `prancy-napping-pie.md`
plan-file citation below is to a DIFFERENT, ephemeral, no-longer-extant
plan file from an earlier planning session (the "Phase 7" work this
ADR and ADR-0006/ADR-0007 describe) — not the actively-tracked
Python→Rust migration plan of the same auto-generated codename this
fork's Rust rewrite has used since 2026-08. Plan-mode codenames are
assigned per session and can repeat; this is a real, confirmed
coincidence, not the same document.

## Original decision (verbatim, unedited)

Upstream agent-mcp uses raw `sqlite3` everywhere (~150
`cursor.execute(...)` sites across tools/, routes/, core/), with no
migration runner or schema version tracking — schema changes are made
by hand-editing `db/schema.py`. Phase 7b needs a real migration
(adding `created_by`/`created_at` to `project_context` for ADR-0007),
making the missing migration story a blocker. We also want the option
to swap SQLite for Postgres/MySQL later without rewriting every query.

Decided in plan `prancy-napping-pie.md` Q7.7. Decision: adopt
SQLAlchemy 2.0 ORM + Alembic as the canonical DB layer, migrating one
table at a time — `project_context` first (Phase 7a), each subsequent
table (agents, tasks, agent_actions, file_metadata, …) getting its own
model class, Alembic migration, and query rewrites. New deps:
`sqlalchemy>=2.0`, `alembic`.

During the transition the codebase mixes ORM and raw SQL — accepted,
since a full-bang rewrite would freeze the rest of Phase 7 work for
weeks. Alembic without an ORM was rejected: it buys migration tracking
but not multi-dialect abstraction. A hand-rolled migration runner over
raw sqlite3 was rejected as reinventing Alembic poorly.

Endpoint: `cursor.execute(...)` should survive only in Alembic
migration files.
