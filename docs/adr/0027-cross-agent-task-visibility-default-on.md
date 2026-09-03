# ADR 0027: Cross-agent task read/comment access defaults ON

**Status**: Accepted, 2026-09-03.
**Date**: 2026-09-03.
**Builds on**: `core/task_ownership.py` (the ownership-predicate
consolidation this feature's toggle lands in), ADR-0018 (settings-schema
registry — where the two new toggles are declared).
**Depends on**: the PF-1 / AZ-R17-1 phantom-404 existence-oracle
control, and the AZ-R19-1 cross-agent stored-injection class, both of
which this feature deliberately relaxes for a narrow slice of surface.

## Context

Before this feature, a worker's task visibility and task-comment
authorship were both scoped to `assigned_to == self` (plus the
unassigned/claimable pool). A task assigned to a DIFFERENT worker was
invisible to `view_tasks` / `search_tasks` / `ask_project_rag`, and
`add_task_comment` on it returned the same phantom `NotFound` a
nonexistent task returns (PF-1 / AZ-R17-1) — an intentional
existence-oracle control from earlier hardening rounds.

The operator asked for the opposite default: workers should be able to
see and discuss each other's tasks, because in practice a small team of
cooperating agents benefits far more from shared visibility (spotting
duplicate work, offering help, understanding what a blocked dependency
is waiting on) than it is harmed by the isolation. This is a deliberate
policy call, not a bug fix — the isolation was working exactly as
designed.

Widening a disclosure boundary by default runs against the general
posture this codebase otherwise holds (see the general precedent named
in ADR-0025: a widening should default OFF and be an explicit,
named opt-in). This ADR documents why THIS widening is the exception,
not a precedent for widening disclosure defaults generally.

## Decision

Two new project-level toggles (`core/settings_schema.py`,
`worker_permissions` group), **both defaulting `true`**:

- `config_allow_worker_view_foreign_tasks` — a worker's `view_tasks` /
  `search_tasks` / `ask_project_rag` calls are no longer scoped to
  `{own tasks} ∪ {unassigned pool}`; they also see tasks assigned to a
  DIFFERENT agent.
- `config_allow_worker_comment_foreign_tasks` — a worker may
  `add_task_comment` on a task assigned to a DIFFERENT agent (implies
  view; comments remain author-attributed and timestamped as normal).

Both toggles are read through `core.task_ownership.can_access_task`'s
new `include_foreign` parameter — the single seam every call site
(`view_tasks`, `search_tasks`, RAG task retrieval, `add_task_comment`)
consults, rather than a class-sweep of independent checks (the exact
problem the ownership-predicate consolidation this ADR builds on was
written to prevent).

**Scope is deliberately narrow** — this decision does NOT widen:

- Who may edit or reassign a task, update its status, attach a
  subtask/dependency under it, or run bulk operations on it. All of
  those write paths keep their existing exact-ownership gate,
  unaffected by either toggle.
- Who may edit or delete a comment once it exists — that stays
  author-only (or manager-tier), regardless of who was allowed to
  create it.
- Visibility of a task's secrets, agent tokens, or any field beyond
  what `view_tasks` already renders for a task the caller CAN see.

## Why this is the exception, not a new default posture

Three properties distinguish this widening from the general case ADR-
0025 argues against defaulting open:

1. **The write half is append-only, not structural.** A comment is an
   immutable, timestamped, author-attributed row. It cannot reassign a
   task, mutate a `child_tasks`/`depends_on_tasks` mirror, or change
   what work exists — the exact mutation shapes the AZ-R19-1 class (the
   subtask-parent-attach primitive) and the general stored-injection
   class both worry about. A malicious comment is visible, attributed,
   and inert with respect to task state.
2. **No named security control is reopened.** AZ-R19-1's actual fix
   (the subtask/dependency-attach ownership gate in `task_tools.py`) is
   untouched — `include_foreign` was never threaded into it. PF-1 /
   AZ-R17-1's phantom-404 mechanism still exists and still fires
   whenever a project opts back into the stricter policy (the toggle
   OFF path); this ADR changes the DEFAULT, not the mechanism.
3. **The read half discloses content the operator already trusts every
   worker to have via other paths.** Task titles/descriptions/status
   are shared project content in the same sense `docs`/`memory`/`code`
   RAG sources already are (ADR-0017) — the isolation being relaxed was
   specifically an authorization boundary between COOPERATING workers on
   the SAME project, not a tenant boundary (that stays fully enforced —
   nothing here touches cross-project isolation) and not a boundary
   against an untrusted party.

## Consequences

### Positive

- Workers can discover and comment on related work without the
  operator hand-coordinating every cross-agent question through itself.
- `can_access_task` gained exactly one new parameter, consulted at
  every call site through the existing consolidation — no repeat of the
  4-implementation drift this feature's own PR 2 (the consolidation)
  closed.
- Both toggles are visible, named, per-project settings (`GET
  /api/settings-schema`, dashboard Settings tab) — an operator who
  wants the old isolation back has a one-flag path, not a code change.

### Negative / trade-offs

- A compromised or misbehaving worker can now read every task in the
  project (previously bounded to its own + the pool) and leave comments
  on any of them. This is the accepted cost: the blast radius of a
  single bad agent widens for READ and for COMMENT-INJECTION, but not
  for task MUTATION.
- The existence-oracle protection (PF-1 / AZ-R17-1) is now effectively
  moot for the default configuration — a foreign task's existence is
  directly observable via `view_tasks`, not just inferable through a
  403-vs-404 side channel. The mechanism is kept (for the toggle-off
  path) rather than deleted, since a project with a real trust boundary
  between its workers still needs it.

## Alternatives considered

- **Default both toggles OFF** (matching the general ADR-0025 posture)
  — rejected per the operator's explicit request; the isolation was
  functioning as designed and the ask was specifically to relax it by
  default for the common cooperating-team case, with the toggle
  existing precisely for operators who want the old behavior.
- **One toggle instead of two** — rejected during design (see the
  session's grill interview): read and write are separable concerns
  with different blast radii (structural-mutation-adjacent write vs.
  pure read), and the operator explicitly wanted them independently
  switchable.
- **Widen task MUTATION (status/reassignment/subtask-creation) under
  the same toggle** — rejected. Those write paths are exactly the
  AZ-R19-1 / R4-F5 class this feature deliberately does not touch;
  bundling them would have been a much larger, differently-shaped
  security decision than "workers can see and discuss each other's
  work."

## Links

- `agent_mcp/core/task_ownership.py` — `can_access_task`'s
  `include_foreign` parameter and `sql_fragment`'s equivalent.
- `agent_mcp/core/settings_schema.py` —
  `config_allow_worker_view_foreign_tasks` /
  `config_allow_worker_comment_foreign_tasks`.
- `tests/test_task_ownership.py` — the `include_foreign` unit + SQL/dict
  equivalence property tests.
- `tests/test_worker_unassigned_visibility.py`,
  `tests/test_sec_comment_ownership_rag_gate.py`,
  `tests/test_sec_r4_f4_rag_task_ownership_scope.py`,
  `tests/test_wave6_pr4_task_tools_e2e.py` — default-on + toggle-off
  parametrized coverage for view/search/RAG/comment.
- [ADR-0025](0025-forwarding-tier-excluded-from-confirmed-operator-tier.md)
  — the general disclosure-widening-defaults-off posture this ADR is a
  deliberate, scoped exception to.
- [ADR-0018](0018-settings-schema-registry.md) — the settings-schema
  registry both new toggles are declared in.
