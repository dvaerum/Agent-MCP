# ADR-0004: Per-project policy lives in dashboard Settings tab, backed by `config_*` project_context keys

## Status

Accepted. Still the live pattern — see ADR-0018 (settings-schema
registry as the single source of truth), which builds on this decision
by fixing the frontend/backend schema-declaration split it introduced,
without changing the underlying `config_allow_*`-toggle shape decided
here.

**Provenance**: originally filed in the `home-manager-config` deploy
repo's `common/user/agent-mcp/docs/adr/` (as
`0004-settings-tab-policy-toggles.md`). Moved here 2026-09-06 — see
ADR-0003's own provenance note for why.

## Original decision (verbatim, unedited)

Every new agent capability (worker→worker messaging, worker
self-assignment, worker self-update of task status, worker creation
of unassigned tasks) ships with a per-project admin-toggleable knob
exposed in a new dashboard "Settings" tab and persisted as
`config_allow_*` keys in `project_context`. Defaults are chosen per
capability: deny for collusion-shaped capabilities (worker→worker
messaging), allow for worker-self-scope capabilities (own task status,
self-assign). This avoids the alternative of hardcoded policy (which
would force forking for variation across deployments) and the
alternative of a richer permission model (capability tokens, ACLs —
weeks of work for negligible gain at our scale). Decision is sticky:
it establishes a pattern future capabilities will follow, so adding
new toggles is a 30-line PR each rather than a design exercise.
