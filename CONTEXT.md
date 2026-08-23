# CONTEXT.md — identity & authorization vocabulary

This is the canonical glossary for "who is calling, and what can they
do" across Agent-MCP. It exists because the same handful of concepts
have accumulated 3-5 competing names across files as the authorization
model evolved through Waves 6-9 and several pentest rounds (see
`docs/proposals/security-authz-architecture-hardening.md`, Finding H).

**Rule going forward: check here before introducing a new term for an
identity/authz concept. If a concept already has a canonical name
below, use it — don't add a synonym.** If you need a genuinely new
concept, add an entry here in the same PR.

This file documents what the code actually does; it does not itself
change any behavior. Where multiple names compete for one concept, one
is picked as canonical here and the others are flagged as deprecated
synonyms — grep-replacing them over time is left to future PRs, not
done by this one.

## Core identity

### Principal

**Canonical term for "the authenticated identity making this call."**
An immutable dataclass: `agent_mcp/core/principal.py:58` (`Principal`).
Fields: `kind`, `user_id`, `agent_id`, `sysadmin`, `project_name`,
`project_role`, `agent_role`, `can_wake_loop`, `source_token`,
`capabilities`. Built once at the outermost auth seam (router
`auth_middleware.py` for the cookie/forwarding path, `app/main_app.py`
for the MCP bearer path) and threaded through every downstream
decision point — it is never re-derived mid-request. Authorization
decisions are method calls on it: `principal.has_capability(cap)`
(`agent_mcp/core/principal.py:128`).

**Do not call this**: "auth dict", "caller", "actor", "identity
object" — all four names appear informally in comments/docstrings
across the codebase referring to this same concept on the MCP side.
Use `Principal`.

**Known deviation — the REST "auth dict" is NOT a Principal.**
`agent_mcp/app/deps.py:310` (`require_operator_session`) returns a
plain `dict[str, Any]` with one of three shapes
(`{"kind": "session", "user": ..., "project_role": ..., "sysadmin":
...}`, `{"kind": "forwarding", "operator_id": ...}`, `{"kind":
"operator_bearer", "user": None}`) — not a `Principal`. This is a
real, currently-shipping type split between the MCP surface
(`Principal`) and the backend REST surface (this untyped dict), not a
naming inconsistency to fix here. It is tracked as Finding D in
`docs/proposals/security-authz-architecture-hardening.md` (typed
`Principal` for backend REST, Phase 5 — the highest-effort phase,
blocked on updating a test that pins the dict shape verbatim). Do not
attempt to unify it in this phase; do use the term **"REST auth
dict"** (not "Principal") when referring to this shape, so the two are
never conflated in conversation or comments.

### PrincipalKind — the four authentication MODES

`agent_mcp/core/principal.py:54`: `Literal["operator_session",
"agent_bearer", "forwarding_header"]` — three MCP-side values, plus a
fourth REST-only mode below. These are the distinct ways a `Principal`
(or the REST auth dict) can be constructed; a request is authenticated
through exactly one of them.

* **`agent_bearer`** — a per-agent token on `Authorization: Bearer`,
  minted at `register_agent` time and stored in the `agents` table.
  Resolves to a `worker` or `manager` `agent_role`. Built by
  `agent_mcp/core/principal_builder.py:54`
  (`build_agent_bearer_principal`).
* **`operator_session`** — the dashboard cookie path (ADR-0013). A
  human operator logged in via `POST /agent-mcp/login`; the session
  cookie resolves to a `users` row + `project_membership.role`. Built
  by `agent_mcp/core/principal_builder.py:110`
  (`build_operator_principal`).
* **`forwarding_header`** — the signed `X-Agent-MCP-Forwarded-Operator`
  header the router attaches when proxying a cookie-authenticated
  dashboard request through to a per-project backend (ADR-0020: router
  is mount-agnostic, so the backend can't see the original cookie).
  Also built by `build_operator_principal`, with `kind="forwarding_header"`.
* **`operator_bearer`** (REST-only, not a `PrincipalKind` value — a
  REST auth-dict `"kind"` discriminator) — a per-agent
  manager/admin-role bearer token presented directly to a backend REST
  endpoint instead of a cookie. See
  `agent_mcp/app/deps.py:310-436`. Named `"admin_token"` before
  retire-system-token Wave 5; renamed because it never carried a
  god-key admin token post-Wave-1, only per-agent manager-tier tokens.
  Do not confuse this REST-only discriminator with the MCP
  `agent_bearer` `PrincipalKind` — they overlap in meaning (a bearer
  token identifying an agent) but live in different type systems (REST
  dict vs. typed `Principal`) and currently have different name
  spellings for historical reasons.

**Do not call these** "auth modes", "login types", or "identity
sources" interchangeably with `PrincipalKind` values — use the exact
literal (`agent_bearer`, `operator_session`, `forwarding_header`) or
the REST discriminator (`operator_bearer`) verbatim.

## Operator-tier vocabulary — three distinct, non-interchangeable concepts

This is the area with the most historical drift. Three predicates
answer three different questions; none of them is a synonym for
either of the others, even though all three sometimes evaluate `True`
for the same caller.

### `is_operator_tier` — "does this caller carry the operator write marker?"

`agent_mcp/core/principal_builder.py:162` (`is_operator_tier`). Single
definition (collapsed from two copies that had drifted — see the
function's own docstring). Returns `True` iff:

* `principal.has_capability("system.config.write")` (present in
  `PROJECT_ROLE_BUNDLES["operator"]`, short-circuited by the sysadmin
  wildcard), **or**
* `principal.agent_id == "admin"` — the legacy pseudo-agent label the
  test harness seeds for a manager-role row named `admin`. Production
  post-Wave-4 has no such row; this branch collapses to the capability
  check in real deployments.

This is a coarse-grained, capability-derived predicate. It answers
"can this caller mutate project config", nothing more specific.

### `is_sysadmin` — "does this caller hold the wildcard capability?"

Not a function — a field: `principal.sysadmin: bool`
(`agent_mcp/core/principal.py:112`), and the resolution rule in
`agent_mcp/core/capabilities.py:261` (`resolve_capabilities`): when
`sysadmin=True` is passed in, the Principal's `capabilities` becomes
exactly `frozenset({SYSADMIN_WILDCARD})` (`"*"`,
`agent_mcp/core/capabilities.py:205`), which
`Principal.has_capability` (`agent_mcp/core/principal.py:154`)
short-circuits to admit *any* capability string unconditionally. This
is deployment-wide: a sysadmin is not scoped to one project the way an
`operator` project-role is.

**A sysadmin is always operator-tier** (the wildcard trivially
satisfies `is_operator_tier`'s capability check), **but operator-tier
does not imply sysadmin** — a caller with `project_role == "operator"`
in one project satisfies `is_operator_tier` without ever setting
`sysadmin=True`.

### `catalog_role()` — the narrower, MCP-catalog-specific concept

`agent_mcp/core/principal_builder.py:183` (`catalog_role`). Returns
`CatalogRole = Literal["admin", "worker", "anonymous"]`
(`agent_mcp/core/principal_builder.py:39`). This is **not** a synonym
for `is_operator_tier` or `sysadmin` — it is a third, deliberately
narrower vocabulary used for exactly one purpose: deciding what a
caller sees in the three MCP catalog-listing surfaces —
`tools/list` (`agent_mcp/tools/registry.py`'s
`list_available_tools`), `prompts/list`/`prompts/get`, and
`resources/list`/`resources/read` (`agent_mcp/resources/__init__.py`).

Mapping (`agent_mcp/core/principal_builder.py:196-212`):

* `None` (no authenticated Principal in flight) → `"anonymous"`.
* `is_operator_tier(principal)` → `"admin"`.
* any other authenticated Principal (agent bearer, or a viewer-tier
  operator/forwarding-header caller) → `"worker"` — an authenticated
  non-admin. Note this collapses a viewer-tier *operator* and a
  worker-tier *agent* into the same catalog bucket; that is
  intentional for catalog visibility (both should see the
  non-admin-only surface) even though they are very different
  Principals for every other authorization decision in the system.

Before arch-r3 #1+5 PR-B, the three catalog surfaces each re-derived
this independently and disagreed (a viewer-tier `forwarding_header`
caller resolved to `"anonymous"` for `tools/list`, `"worker"` for
prompts, and an `agent_id` string-match for resources). `catalog_role`
is now the single source every catalog surface calls — see
`agent_mcp/resources/__init__.py:206` for one live call site.

**Do not use `catalog_role()`'s `"admin"` value as a general
stand-in for `is_operator_tier` or `sysadmin` outside catalog-listing
code** — it is defined only in terms of those two, never the other way
around, and it discards information (it cannot distinguish a viewer
from a worker-role agent) that other authorization decisions need.

### `catalog role "admin"` vs. project-membership `role = "admin"`

There is a fourth, DB-level, unrelated meaning of the string
`"admin"`: `PROJECT_ROLE_BUNDLES`
(`agent_mcp/core/capabilities.py:127`) recognizes exactly two
project-membership roles, `"viewer"` and `"operator"` — **there is no
`"admin"` project-membership role**. Where `"admin"` appears as a
`visibility=` kwarg value in tool registration
(`agent_mcp/tools/access.py:96-97`, `:206-209`), it is documented
explicitly as **"a legacy synonym for `operator`"** — both collapse to
the same `"operator"` tools/list tier. So: `catalog_role()`'s
`"admin"` return value, the legacy `visibility="admin"` tools/list
synonym for `"operator"`, and "sysadmin" are three different things
that all happen to use or mean something adjacent to the word
"admin". None of them is a project-membership role — that vocabulary
only has `viewer` / `operator`.

### `is_confirmed_operator_tier` — a fourth, narrower, defense-in-depth predicate

Not one of the three above — a separate, stricter check with its own
module: `agent_mcp/core/operator_tier.py:71`
(`is_confirmed_operator_tier`). Answers a different question than
`is_operator_tier`: "may this caller receive **plaintext secrets**
(agent bearer tokens, project secrets), or must those be masked?" It
sits *behind* the coarse capability gate as defense-in-depth — a
caller who already passed `is_operator_tier` (or the cap gate
directly) can still fail this stricter check and get secrets
redacted.

Confirmed operator tier iff EITHER:

1. the caller authenticated via a **verifiable per-agent operator-tier
   bearer** (REST `operator_bearer` kind, or MCP `agent_bearer` with
   `agent_role in {"manager", "admin"}`), OR
2. the backend can **see** a resolved operator identity — `sysadmin`,
   or `project_role == "operator"`.

This module exists because the policy was implemented twice
(`app/routers/composition.py`'s REST copy and
`tools/admin_tools.py`'s MCP copy) and the two DRIFTED — see the
module docstring in `agent_mcp/core/operator_tier.py:10-25` for the
exact before/after disagreement. Do not reimplement this check inline
anywhere; both surfaces now call the one function, adapting their
native identity representation (REST auth dict vs. `Principal`) into
its keyword arguments.

## Capability vocabulary — three distinct layers

### Capability (the string)

A single authorization atom, e.g. `"agents.terminate"`,
`"tasks.assign"`, `"system.config.write"`. The complete vocabulary is
the frozen 28-element set `KNOWN_CAPABILITIES`
(`agent_mcp/core/capabilities.py:81`). Format: AWS-IAM-style,
per-resource × verb, matching `^[a-z]+(\.[a-z_]+)+$`. `system.*` caps
are project-membership-ungated (deployment-wide router-admin verbs);
every other cap requires the caller to have a project membership
(`project_role is not None`) or be an `agent_bearer`
(`Principal.has_capability`, `agent_mcp/core/principal.py:128`).
Adding/removing a capability is a design change (Wave 9 grilling,
locked 2026-06-30), not a routine PR.

### Role bundle (a set of capabilities granted by a role)

Two dicts, both in `agent_mcp/core/capabilities.py`:

* `PROJECT_ROLE_BUNDLES` (line 127) — caps granted to
  operator-tier callers (`operator_session` / `forwarding_header`
  Principals) by `project_membership.role`: `"viewer"` (read-only) or
  `"operator"` (viewer + write surfaces + `system.config.write` +
  `rag.*`).
* `AGENT_ROLE_BUNDLES` (line 165) — caps granted to `agent_bearer`
  Principals by `agents.agent_role`: `"worker"` (baseline) or
  `"manager"` (worker + `tasks.assign` + `memories.update`).

A bundle is a **set of capability strings**, resolved once per request
by `resolve_capabilities()` (`agent_mcp/core/capabilities.py:211`) and
attached to the Principal. It is not itself a visibility tier — see
below for that distinct, derived concept.

**Do not call a role bundle a "capability"** (singular) — a bundle is
a set of many; a capability is one string. Do not call it a "role"
either without qualifying which vocabulary — see the next section for
why "role" alone is ambiguous in this codebase.

### `tools/list` visibility tier (access level)

A **third, distinct, and DERIVED** concept: the string used to decide
whether a given tool/resource/prompt appears in a catalog listing for
a given caller. Defined and computed in
`agent_mcp/tools/access.py:141` (`_derive_access_level`), feeding the
module-level `TOOL_ACCESS` map. Values, from most to least
restrictive: `"operator"` (== legacy synonym `"admin"`), `"manager"`,
`"worker"`, `"any"`, or the parametrized `"worker-if-toggled:<config_key>[,<config_key>...]"`.

This is derived (not hand-maintained) from whichever of these three
signals is present, in priority order:

1. the tool impl's `_required_capability` (stamped by
   `@requires_capability`, `agent_mcp/core/authorize.py:166`) → mapped
   to a tier via `_visibility_for_capability`
   (`agent_mcp/tools/access.py:104`: cap in the worker bundle →
   `"worker"`; cap only in the manager bundle → `"manager"`; cap in
   neither agent bundle → `"operator"`);
2. the impl's `_required_policy_keys` (stamped by `@requires_policy`)
   → renders to `"worker-if-toggled:<keys>"`;
3. the registry entry's `visibility=` kwarg (`declared_visibility`) —
   the only signal for tools whose cap check is in-body rather than
   decorator-stamped, and otherwise usable only to *tighten* (never
   loosen) the derived tier.

**Do not confuse this with `catalog_role()`** (above) — `catalog_role`
classifies the *caller* into `admin`/`worker`/`anonymous`; the
`tools/list` visibility tier classifies the *tool* into
`operator`/`manager`/`worker`/`any`/`worker-if-toggled:...`. A tool's
tier and a caller's catalog role are compared (via
`is_visible_to_role`, `agent_mcp/tools/access.py:409`) to decide
listing membership — they are two different vocabularies that happen
to share the substring "admin"/"operator" in places, which is exactly
the kind of collision this document exists to prevent.

The generic `Registry[T].list_visible()` visibility sentinel
(`agent_mcp/core/registry.py:76`: `Literal["any", "admin"]` or a
callable) is a related but *coarser* mechanism used by resources and
prompts (which have no capability-driven derivation) — a 2-valued
subset of the same idea, not identical to the 5-shape `tools/list`
tier string above. When writing new code against the shared
`Registry`, use its own `"any"`/`"admin"`/callable vocabulary as
documented in `agent_mcp/core/registry.py`; do not assume it accepts
the fuller `tools/access.py` tier strings.

## Authentication MODES — how a Principal gets built

See "PrincipalKind" above for the four modes by name. For quick
cross-reference, the phrase-level vocabulary used in prose/comments
maps onto them as:

* **"agent bearer"** = `PrincipalKind == "agent_bearer"`: a per-agent
  MCP token on `Authorization: Bearer`.
* **"forwarding header"** = `PrincipalKind == "forwarding_header"`:
  the router's signed `X-Agent-MCP-Forwarded-Operator` header, built
  by `app/main_app.py`'s `AuthHeaderMiddleware`
  and consulted by `app/deps.py:394-417`.
* **"cookie session"** = `PrincipalKind == "operator_session"` (MCP
  side) / REST auth-dict `kind == "session"` (`app/deps.py:370-375`):
  the dashboard's `agent_mcp_session` cookie (ADR-0013).
* **"operator_bearer"** = the REST-only auth-dict discriminator (see
  above) — not a `PrincipalKind` value, a per-agent manager/admin
  bearer presented straight to a REST endpoint.

## Summary table

| Term | What it answers | Type | Defined at |
|---|---|---|---|
| `Principal` | who is calling (MCP side) | frozen dataclass | `core/principal.py:58` |
| REST auth dict | who is calling (backend REST side) | untyped `dict` (3 shapes) | `app/deps.py:310` |
| `PrincipalKind` | which auth mode built this Principal | `Literal[...]` | `core/principal.py:54` |
| capability | one authorization atom | `str`, member of `KNOWN_CAPABILITIES` | `core/capabilities.py:81` |
| role bundle | caps granted by a role | `frozenset[str]` | `core/capabilities.py:127,165` |
| `tools/list` tier | catalog-listing visibility for a *tool* | `str` (5 shapes) | `tools/access.py:141` |
| `catalog_role()` | catalog-listing bucket for a *caller* | `Literal["admin","worker","anonymous"]` | `core/principal_builder.py:183` |
| `is_operator_tier` | can this caller write project config | `bool` | `core/principal_builder.py:162` |
| `sysadmin` | does this caller hold the wildcard cap | `bool` field | `core/principal.py:112` |
| `is_confirmed_operator_tier` | may this caller see plaintext secrets | `bool` | `core/operator_tier.py:71` |
