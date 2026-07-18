# Proposal: Capability-based authorization

* Status: **Proposed / parked** (foundation partially shipped, remainder deferred)
* Date: 2026-07-19
* Builds on: [ADR-0013](../adr/0013-operator-login.md) (operator login),
  [ADR-0015](../adr/0015-sso-oidc-and-proxy-header.md) (SSO/OIDC + groups)
* Supersedes: nothing (would eventually retire the `has_role` surface)

This document is the canonical home for the capability-based-authz idea. It
replaces the ad-hoc "Wave 9" tracking that lived in a personal plan file — read
this instead. It is a *proposal*, not an accepted decision: the plumbing is in
the tree but inert-safe, and the user-visible feature is unbuilt pending a
concrete need (see [When it's worth finishing](#when-its-worth-finishing)).

## The problem

Authorization today asks *who are you* — a coarse role tier:

```python
if not principal.has_role("admin"):     # or "operator" / "manager" / "system" / "any"
    deny()
```

This surface has accreted and overlaps:

- The role names are semantically muddy — `admin`, `operator`, and `system`
  admit the same callers in practice.
- There are **four** decorators doing the same job: `@requires("admin")`,
  `@requires("any")`, `@requires_role("operator")`, `@requires_role("admin")`.
- Router endpoints use a *separate* gate, `@require_sysadmin`.
- Some handler bodies gate inline with `_viewer_blocked()`.
- Roughly **50 call sites** are spread across those overlapping surfaces.

Consequence: there is no single place that answers "what exactly may a viewer
do?" — you must read all 50 sites. And because roles are all-or-nothing, you
**cannot** grant a partial permission set (e.g. "this SSO group may assign
tasks but not delete agents"). That last limitation is the real motivator.

## The design

Replace role tiers with fine-grained **capabilities** — one string per
protected action, `resource.verb`, AWS-IAM / Keycloak style. A gate asks *what
may you do*:

```python
if not principal.has_capability("agents.register"):
    deny()
```

### The taxonomy (28 capabilities)

Single source of truth in `agent_mcp/core/capabilities.py` (`KNOWN_CAPABILITIES`):

```
mcp.connect
agents.view  agents.register  agents.terminate  agents.use
tasks.view   tasks.create     tasks.update      tasks.delete   tasks.assign
memories.view  memories.create  memories.update  memories.delete
messages.view  messages.send
files.use
coordination.assist  coordination.wait
rag.query  rag.rebuild
system.view  system.config.write  system.users.manage  system.groups.manage
system.groups.capabilities.manage  system.projects.manage  system.sso.configure
```

`has_capability(unknown)` returns `False` (default-deny); dev mode warns when a
queried string isn't in `KNOWN_CAPABILITIES`, catching typos at review time.

### Where a caller's capability set comes from

Resolved **once**, at the auth seam, and attached to the `Principal`:

- **Agents** (`agent_bearer`) → a hardcoded bundle by `agent_role`
  (`AGENT_ROLE_BUNDLES`). `worker` gets `tasks.create`, `rag.query`, … ;
  `manager` additionally gets `tasks.assign`, `memories.update`.
- **Operators** (`operator_session` / `forwarding_header`) → a hardcoded bundle
  by `project_role` (`PROJECT_ROLE_BUNDLES`: `viewer` = the `*.view` caps;
  `operator` = the write caps) **plus** whatever their **SSO/OIDC groups**
  grant, looked up in a new `group_capability(group_id, capability)` table.
- **Sysadmin** → the `*` wildcard (`SYSADMIN_WILDCARD`) — `has_capability`
  returns `True` for anything.

`system.*` capabilities are project-membership-ungated; every other
(resource) capability additionally requires project membership, so holding
`tasks.assign` without being a member of the project still denies.

The **groups → capabilities** mapping is the actual *new feature*. Everything
else is cleanup that preserves today's behaviour. It is what lets a deployment
express "this SSO group may X but not Y" as a per-group checkbox — impossible
under all-or-nothing role tiers.

## Current state in the tree (2026-07-19)

Half-adopted, and inert-safe where adopted:

- **Shipped** (merged to `main`): `core/capabilities.py` (the 28-cap taxonomy,
  the bundles, the wildcard), `Principal.has_capability`, and a **bridge** that
  resolves capabilities out of the role bundles so old and new checks coexist.
- **Already migrated** to `has_capability`: a handful of tools —
  `rag_tools.py`, `task_notes_tools.py`, `agent_communication_tools.py`,
  `file_metadata_tools.py`. These work today *because* the bridge maps their
  caps back onto the role bundles.
- **Not migrated**: ~50 sites still use `has_role` / `@requires` /
  `@requires_role` / `@require_sysadmin` / `_viewer_blocked`.
- **Unbuilt**: the `group_capability` table's migration, its repository, the two
  REST routes (`GET`/`PUT /api/router/groups/<id>/capabilities`), and the
  dashboard UI to manage per-group capabilities — i.e. the user-visible feature.

Three later slices were prototyped on short-lived branches off the foundation
commit `777fae9` — migrating `_check_role_principal`, router-side
`require_capability` replacing `require_sysadmin`, and the group-capability UI +
REST routes — but were not carried forward and have been discarded. The design
below is the reference; re-cut fresh against current `main`.

## Remaining work to finish it

Ship in the usual foundation → parallel-migrations → delete-bridge shape,
**cut fresh against current `main`** (the branches above are reference only):

1. Migrate the remaining `has_role` / decorator / `@require_sysadmin` /
   `_viewer_blocked` sites to `has_capability` / `@requires_capability`.
2. Build the `group_capability` table + repository + the two REST routes +
   the dashboard "Capabilities" checklist (grouped by resource). **This is the
   feature; the rest is cleanup.**
3. Delete the bridge, `has_role`, the deprecated decorators, and
   `_check_role_principal` once zero call sites remain. Pure subtraction; a
   grep for `has_role` returning nothing is the completion proof.

Invariants to preserve: bundles must stay a subset of `KNOWN_CAPABILITIES`;
the operator bundle must cover every action `has_role("operator")` admits today
(a smoke test should assert this); existing deploys keep working with an empty
`group_capability` table because the role bundles are the baseline.

## When it's worth finishing

Finish it **only** when there's a concrete need to grant *different* permissions
to *different* operators or SSO groups — e.g. a read-only auditor login, or a
contractor group that may manage tasks but not agents. That need is what the
groups→capabilities mapping uniquely serves.

If every operator who logs in is effectively the admin, this is pure internal
cleanup with no user-visible payoff — leave it parked. The shipped foundation
is dormant and harmless in the meantime; nothing depends on it being finished.
