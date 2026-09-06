# MCP tool input-schema naming is inconsistent across the task-tool family

Migrated 2026-09-06 from the deploy repo's (`home-manager-config`,
formerly `nixos-developer-system`) `docs/UPSTREAM_ISSUES.md` §G — a
real, still-open naming inconsistency found while building a client
against the live tool catalog. Confirmed still present in the current
codebase before moving (spot-checked `task_tools.py`'s `notes`/`status`
field usage). Not superseded by the Rust migration -- the Rust port
faithfully preserved every tool's existing `inputSchema` shape,
warts included, per this project's own "re-derive documented behavior,
don't smuggle in a fix" discipline, so this inconsistency exists
identically in `conexus-tools`' Rust catalog too.

## The inconsistency

- `assign_task` — single-task mode uses `task_title`/`task_description`;
  bulk mode uses a `tasks` array.
- `bulk_task_operations` — `operations[].type` includes `create`,
  `update_status`, `update_priority`, `add_note`, `reassign`. The
  `add_note` op takes a `notes` field; the `update_status` op takes a
  `status` field (not `new_status`).
- `update_task_status` — takes `task_id` + `status`, and `status` is
  *required* even when the caller only wants to add a note via the
  same tool (no note-only mode).

## Impact

Mixed naming is invisible without reading each tool's JSON schema
directly — any client or integration has to probe each tool
empirically rather than pattern-matching field names across the
family. Not a correctness bug (every field works as documented), a
consistency/discoverability one.

## Fix direction

Align field names across the tool family — standardize on `status`
everywhere or rename consistently to `new_status`; same for
`task_title`/`title` and `task_description`/`description`. Document
the bulk-operation shape explicitly enough in each tool's own
`inputSchema` that introspection alone is sufficient, without needing
this note.
