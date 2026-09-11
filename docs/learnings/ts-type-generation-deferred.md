# TS-type generation from Rust: deferred, not built (Phase F)

The original Rust migration plan's Target Architecture named `specta`/
`ts-rs` as the replacement for the Python pipeline that generated
`agent_mcp/dashboard/lib/api-types.generated.ts` from `agent_mcp/db/
pydantic_mirrors.py` (via `scripts/generate_ts_types.py`, pinned by
`tests/test_orm_is_source_of_truth.py`). All three were deleted in
Phase F's Python-decommission pass rather than ported, after research
found:

- **Nothing in the dashboard actually consumed the generated types.**
  `lib/api/index.ts` re-exported them, but no other file imported any
  of the re-exported names. The dashboard's real types (`Agent`,
  `Task`, `Memory`, ...) are hand-maintained per-resource interfaces
  in `lib/api/{agents,tasks,...}.ts`, independent of the generated
  file.
- **The generated file was already stale, silently.** It still
  referenced `task_notes`/`TaskNoteMirror` months after the real
  rename to `task_comments`/`TaskCommentMirror`
  (`agent_mcp/db/pydantic_mirrors.py`). The invariant test that was
  supposed to catch this only compared two fresh generator runs
  against each other — it never read the committed file, so the drift
  was invisible.
- Generation was never wired into CI, a pre-commit hook, or the Nix
  build — purely "run the script and commit the diff" by convention,
  which is exactly how the drift above went unnoticed.

Given zero real consumers, building the ~6-PR specta pipeline (derive
`specta::Type` on the relevant sea-orm entities/row structs in
`conexus-db`, an exporter binary, a `cargo test` invariant) was judged
not worth doing speculatively — this project's own established
precedent is to build a shared primitive only once a real second
consumer needs it (`StableOrderCache`, Phase B), not ahead of demand.

**If a future dashboard feature needs typed Rust→TS row shapes**: the
research groundwork is already done (which Rust struct maps to which
former Python mirror, the specta-vs-ts-rs tradeoff, the sea-orm-Model-
vs-hand-rolled-DTO split, a full PR breakdown) — recover it from this
session's own agent report rather than re-deriving it, then build the
pipeline for real, with a `cargo test` that reads the *committed* file
(not two fresh runs against each other) as the invariant.
