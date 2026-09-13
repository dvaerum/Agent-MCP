# CONTEXT.md — identity & authorization vocabulary

This is the canonical glossary for "who is calling, and what can they
do" across Agent-MCP. It originally documented the Python
implementation's authorization model as it evolved through Waves 6-9
and several pentest rounds; that Python implementation (and the
`docs/proposals/security-authz-architecture-hardening.md` findings
that hardened it) is now fully deleted (Phase F of the Rust
migration — see `~/.claude/plans/prancy-napping-pie.md`). This
revision describes the **Rust** equivalent (`conexus-core`,
`conexus-auth`, `conexus-tools`, `conexus-backend`, `conexus-router`),
faithfully ported from that Python model — most predicates below are
byte-for-byte behavioral ports, a few are deliberate, documented
improvements the migration made along the way (noted where they
occur).

**Rule going forward: check here before introducing a new term for an
identity/authz concept. If a concept already has a canonical name
below, use it — don't add a synonym.** If you need a genuinely new
concept, add an entry here in the same PR.

This file documents what the code actually does; it does not itself
change any behavior. Where multiple names compete for one concept, one
is picked as canonical here and the others are flagged as deprecated
synonyms.

## Core identity

### Principal

**Canonical term for "the authenticated identity making this call."**
An immutable struct: `rust/conexus-core/src/principal.rs:39`
(`Principal`). Fields: `kind`, `user_id`, `agent_id`, `project_name`,
`project_role`, `agent_role`, `can_wake_loop`, `source_token`,
`capabilities`. Built once at the outermost auth seam — the backend's
`principal_resolve.rs` for the MCP bearer/forwarding-header path, the
router's `session_gate.rs` for the cookie/proxy-header path — and
threaded through every downstream decision point; it is never
re-derived mid-request. Authorization decisions are method calls on
it: `principal.has_capability(cap)`
(`conexus-core/src/principal.rs:79`).

**Real, deliberate divergence from the Python source**: there is no
separate `sysadmin: bool` field. Sysadmin is instead one of the two
variants of `capabilities` itself (`Capabilities::Sysadmin`, see
"Capability vocabulary" below) — a real type-system improvement over
Python's `frozenset({"*"})` sentinel-in-a-set encoding, which needed
its own defense-in-depth filter everywhere a capability set was built
from untrusted data. There is no string a caller could smuggle into a
`HashSet<Capability>` that would ever compare equal to the `Sysadmin`
variant.

**Do not call this**: "auth dict", "caller", "actor", "identity
object". Use `Principal`.

### RestPrincipal

**Canonical term for "which backend-REST door admitted this caller, and
what did that door prove about them?"** An enum:
`rust/conexus-backend/src/rest_principal.rs:53` (`RestPrincipal`), with
exactly **two** variants — `Forwarding { operator_id, project_role }`
and `OperatorBearer { bearer_token }`.

**Real, deliberate scope narrowing from the Python source**: Python's
`RestPrincipal` had a THIRD door, `kind="session"` (an
`agent_mcp_session` cookie resolved via a live `router.db` lookup).
That door is NOT ported here — the operator's own 2026-09-05 decision
(`prancy-napping-pie.md`, Phase E1) keeps the per-project backend
router-DB-blind, exactly like `/mcp` auth. Python's own `deps.py`
docstring called that cookie path "defence in depth for a
misconfiguration that bypassed the router middleware"; this backend is
UDS-only and reachable only through the router's proxy in every real
deployment, so there is no real "bypassed the router" caller to
protect. (The router itself DOES resolve cookie sessions — see
`GateIdentity`/`evaluate_session_gate` below, a genuinely different
type serving a genuinely different door.)

`RestPrincipal` and `Principal` remain **deliberately distinct types**,
same reasoning as the Python source:

* `RestPrincipal` is an **admission record** — which REST door, what
  did it prove.
* `Principal` is an **authorization subject** — a resolved
  `capabilities` set, answers `has_capability`.

There is exactly one conversion between them:
`build_dispatch_principal` (`conexus-backend/src/rest_principal.rs`),
mirroring Python's `_build_route_principal`. Say **"REST principal"**
for the former and **"Principal"** for the latter.

### PrincipalKind — the three authentication MODES

`rust/conexus-core/src/principal.rs:14`: `enum PrincipalKind {
OperatorSession, AgentBearer, ForwardingHeader }` — the three ways a
`Principal` can be constructed; a request is authenticated through
exactly one of them. (Python had a fourth, REST-only mode,
`operator_bearer`; here that concept lives entirely on the separate
`RestPrincipal` type as `RestPrincipal::OperatorBearer`, never as a
`PrincipalKind` variant — see `RestPrincipal` above.)

* **`AgentBearer`** — a per-agent token on `Authorization: Bearer`,
  minted at `register_agent` time and stored in the `agents` table.
  Resolves to a `Worker` or `Manager` `AgentRole`.
* **`OperatorSession`** — the dashboard cookie path (ADR-0013),
  resolved by the router's `session_gate.rs`.
* **`ForwardingHeader`** — the signed
  `X-Agent-MCP-Forwarded-Operator` header the router attaches when
  proxying a cookie-authenticated dashboard request through to a
  per-project backend (ADR-0020: router is mount-agnostic).

**Do not call these** "auth modes" or "login types" interchangeably
with `PrincipalKind` values — use the exact variant name
(`AgentBearer`, `OperatorSession`, `ForwardingHeader`), or the
REST-only discriminator (`RestPrincipal::OperatorBearer`) verbatim.

## Operator-tier vocabulary — three distinct, non-interchangeable concepts

Three predicates answer three different questions; none of them is a
synonym for either of the others, even though all three sometimes
evaluate `true` for the same caller.

### `is_operator_tier` — "does this caller carry the operator write marker?"

`rust/conexus-core/src/principal.rs:122` (`is_operator_tier`). Returns
`true` iff:

* `principal.has_capability(Capability::SystemConfigWrite)` (present
  in `project_role_bundle(Operator)`, short-circuited by the sysadmin
  wildcard), **or**
* `principal.agent_id == Some("admin")` — the legacy pseudo-agent
  label the test harness seeds for a manager-role row named `admin`.
  Production has no such row; this branch collapses to the capability
  check in real deployments.

A coarse-grained, capability-derived predicate. Answers "can this
caller mutate project config", nothing more specific.

### `sysadmin` — "does this caller hold the wildcard capability?"

Not a field — a `capabilities` variant:
`Capabilities::Sysadmin` (`conexus-core/src/capability.rs:194`, see
"Real, deliberate divergence" under `Principal` above).
`Principal::has_capability` (`conexus-core/src/principal.rs:79`)
short-circuits on this variant to admit *any* capability
unconditionally, checked FIRST and unconditionally — before the
project-membership check — a real bug this migration caught and fixed
during the port (Phase A: a sysadmin with no `project_role` set was
being wrongly denied every non-system capability by the original
branch order).

**A sysadmin is always operator-tier**, **but operator-tier does not
imply sysadmin** — a caller with `project_role == Operator` in one
project satisfies `is_operator_tier` without ever holding the wildcard.

### `catalog_role()` — the narrower, MCP-catalog-specific concept

`rust/conexus-core/src/principal.rs:169` (`catalog_role`). Returns
`CatalogRole { Anonymous, Admin, Worker }`
(`conexus-core/src/principal.rs:139`). This is **not** a synonym for
`is_operator_tier` or sysadmin — it is a third, deliberately narrower
vocabulary used for exactly one purpose: deciding what a caller sees
in the three MCP catalog-listing surfaces — `tools/list`
(`conexus-tools::access`), `prompts/list`/`prompts/get`
(`conexus-tools::prompts`), and `resources/list`/`resources/read`
(`conexus-tools::resources`).

Mapping:

* `None` (no authenticated Principal in flight) → `Anonymous`.
* `is_operator_tier(principal)` → `Admin`.
* any other authenticated Principal (agent bearer, or a viewer-tier
  operator/forwarding-header caller) → `Worker` — an authenticated
  non-admin. This deliberately collapses a viewer-tier *operator* and
  a worker-tier *agent* into the same catalog bucket even though they
  are very different Principals for every other authorization
  decision.

Before this function existed in the Python source, the three catalog
surfaces each re-derived "is this caller an admin" independently and
disagreed (a viewer-tier `ForwardingHeader` caller resolved to
`Anonymous` for `tools/list` but `Worker` for prompts) — `catalog_role`
is the single source every catalog surface now calls, closing that
drift class structurally, and is pinned by a dedicated regression test
(`a_viewer_tier_forwarding_header_caller_resolves_to_worker_not_anonymous`).

**Do not use `catalog_role()`'s `Admin` value as a general stand-in for
`is_operator_tier` or sysadmin outside catalog-listing code** — it is
defined only in terms of those two, never the other way around, and it
discards information (it cannot distinguish a viewer from a
worker-role agent) that other authorization decisions need.

### `catalog role "admin"` vs. project-membership role

There is a second, DB-level, unrelated meaning of the string
`"admin"`: `ProjectRole` (`conexus-core/src/capability.rs:223`) has
exactly two variants, `Viewer` and `Operator` — **there is no `Admin`
project-membership role**. So: `CatalogRole::Admin`'s string form and
"sysadmin" are two different things that both happen to use or mean
something adjacent to the word "admin". Neither is a project-membership
role — that vocabulary only has `Viewer`/`Operator`.

### `is_confirmed_operator_tier` — a fourth, narrower, defense-in-depth predicate

Not one of the three above — a separate, stricter check, and (a real,
documented divergence from the Python source's single shared function)
now **two** separate implementations for the **two** identity types
that need it, since `Principal` and `RestPrincipal` can't represent
each other's admission shapes:

* `conexus-core/src/principal.rs:203` — the MCP-side predicate, over
  `Principal`.
* `conexus-backend/src/rest_principal.rs:198` — the REST-side
  predicate, over `RestPrincipal`.

Both answer the same question: "may this caller receive **plaintext
secrets** (agent bearer tokens, project secrets), or must those be
masked?" — a defense-in-depth check sitting *behind* the coarse
capability gate. Confirmed operator tier iff EITHER:

1. the caller authenticated via a **verifiable per-agent operator-tier
   bearer** (`RestPrincipal::OperatorBearer`, or MCP `AgentBearer` with
   `agent_role == Manager`), OR
2. the backend can **see** a resolved operator identity — sysadmin, or
   `project_role == Operator`.

Both implementations preserve ADR-0025's forwarding-tier-exclusion
principle bit-for-bit: a `ForwardingHeader`/`RestPrincipal::Forwarding`
caller is deliberately NEVER given clause-1 treatment even though it
carries a signed role — pinned by
`forwarding_header_never_gets_clause_1_treatment`. Do not reimplement
this check inline anywhere; both surfaces call their own type's one
function.

## Capability vocabulary — three distinct layers

### Capability (the string, now a closed enum)

A single authorization atom, e.g. `Capability::AgentsTerminate`,
`Capability::TasksAssign`, `Capability::SystemConfigWrite`. The
complete vocabulary is the closed `enum Capability`
(`conexus-core/src/capability.rs:43`, `Capability::ALL`).

**Real, deliberate improvement over the Python source**: capability
strings were stringly-typed in Python (a `frozenset[str]` validated
only by a regex convention + a smoke test); here they're a real,
closed `enum` — an unknown/typo'd capability is a `FromStr` parse
error at the boundary (config load, DB row, API body) instead of a
runtime string that silently never matches anything.

`system.*` caps are project-membership-ungated
(`Capability::is_system_tier`, `conexus-core/src/capability.rs:153`,
deployment-wide router-admin verbs); every other cap requires the
caller to have a project membership (`project_role.is_some()`) or be
an `AgentBearer` (`Principal::has_capability`,
`conexus-core/src/principal.rs:79`). Adding/removing a capability is a
design change (Wave 9 grilling, locked 2026-06-30, preserved through
the migration), not a routine PR.

### Role bundle (a set of capabilities granted by a role)

Two functions, both in `conexus-core/src/capability.rs` (functions,
not dicts — a real, deliberate shape change since Rust has no
module-level mutable dict idiom to port; the meaning is identical):

* `project_role_bundle(role: ProjectRole)` (line 261) — caps granted
  to operator-tier callers (`OperatorSession`/`ForwardingHeader`
  Principals) by `project_membership.role`: `Viewer` (read-only) or
  `Operator` (viewer + write surfaces + `SystemConfigWrite` + `rag.*`).
* `agent_role_bundle(role: AgentRole)` (line 233) — caps granted to
  `AgentBearer` Principals by `agents.agent_role`: `Worker` (baseline)
  or `Manager` (worker + `TasksAssign` + `MemoriesUpdate`).
  `AgentsRotateToken` is deliberately in NEITHER bundle — an agent
  must never rotate a peer's, or its own, bearer.

A bundle is a **set of capabilities**, resolved once per request by
`conexus_auth::capabilities::resolve_capabilities` and attached to the
Principal. It is not itself a visibility tier — see below for that
distinct, derived concept.

**Do not call a role bundle a "capability"** (singular) — a bundle is
a set of many; a capability is one enum value.

### `tools/list` visibility tier (access level)

A **third, distinct, and DERIVED** concept: the value used to decide
whether a given tool appears in a catalog listing for a given caller.
Defined and computed in `conexus-tools/src/access.rs:114`
(`access_tier`), feeding the module-level `TIER_OVERRIDES` table.
Values: `enum AccessTier { Operator, Worker, Any,
WorkerIfToggled(&[&str], bool) }` (`conexus-tools/src/access.rs:41`).

**Real, confirmed finding from the migration (not a design change,
a fact about the real Python call graph)**: there is deliberately **no
`Manager` variant**. Python's `is_visible_to_role` carried a full
4-role model including `"manager"`, but `catalog_role()` — the ONLY
function that ever supplies a role to it — only ever returns
`admin`/`worker`/`anonymous`; a manager agent bearer collapses to
`worker` there. A tool whose capability derives to "manager" tier was
therefore already invisible to every role except admin in the code
that actually executed, identical to "operator" tier — confirmed by
grepping every real call site before porting, not assumed.

Derived (not hand-maintained) from whichever of these signals is
present, in priority order:

1. the tool's `Requirement::Cap` → mapped to a tier (cap in the worker
   bundle → `Worker`; cap only in the manager bundle → the
   operator-equivalent tier per the finding above; cap in neither
   agent bundle → `Operator`);
2. `Requirement::Policy` → renders to `WorkerIfToggled(keys, default)`
   — the SAME `default` the call-time gate itself uses (one source,
   can't drift, unlike Python's separate `_TOGGLE_DEFAULTS` table);
3. `TIER_OVERRIDES` (`conexus-tools/src/access.rs:76`) — a small,
   hand-reviewed, tighten-only table, ported ONLY where it changes the
   outcome; every entry traces to a real Python `visibility=` kwarg,
   confirmed by reading the Python source directly before it was
   deleted, not assumed. Two Python kwargs (`update_task`,
   `delete_task`) were confirmed REDUNDANT (merely echoing the
   already-derived tier) and correctly NOT ported.

**Do not confuse this with `catalog_role()`** (above) — `catalog_role`
classifies the *caller*; the `tools/list` tier classifies the *tool*.
They are compared (via `is_visible_to_role`,
`conexus-tools/src/access.rs`) to decide listing membership.

**Real, deliberate simplification over the Python source**: Python's
generic `Registry[T].list_visible()` visibility mechanism (a 2-valued
`"any"`/`"admin"`-or-callable sentinel, used by resources and prompts)
was **not ported at all**. `conexus-tools::resources::resolve_read_scope`
is a purpose-built function for exactly the resources catalogue's two
entries instead — building the generic engine for a two-variant case
would have been over-engineering; the prompts catalogue similarly uses
a plain "any"/"admin" string comparison inline, not a shared generic
type.

## Authentication MODES — how a Principal gets built

See "PrincipalKind" above for the three MCP-side modes. For quick
cross-reference:

* **"agent bearer"** = `PrincipalKind::AgentBearer`: a per-agent MCP
  token on `Authorization: Bearer`, resolved in
  `conexus-backend/src/principal_resolve.rs`.
* **"forwarding header"** = `PrincipalKind::ForwardingHeader`: the
  router's signed `X-Agent-MCP-Forwarded-Operator` header
  (`conexus-auth::forwarding_header`), verified in
  `conexus-backend/src/principal_resolve.rs` and (for REST) in
  `conexus-backend/src/rest_principal.rs`.
* **"cookie session"** = `PrincipalKind::OperatorSession` (MCP side —
  proxied by the router as a `ForwardingHeader` to the backend) /
  `GateIdentity` (`conexus-router/src/session_gate.rs:125`, the
  router's own resolved cookie identity, evaluated by
  `evaluate_session_gate`, `conexus-router/src/session_gate.rs:313`):
  the dashboard's `agent_mcp_session` cookie (ADR-0013).
* **`RestPrincipal::OperatorBearer`** — the REST-only variant (see
  above) — not a `PrincipalKind` value, a per-agent manager-role
  bearer presented straight to a backend REST endpoint.

## Authorization gate vocabulary — `Requirement`

A concept with **no Python analogue to name** — Python's tool
registration stamped a capability/policy/predicate via decorators
whose gate logic was scattered across three separate check functions
(`check_capability_gate`/`check_policy_gate`/`check_predicate_gate`).
The Rust port unifies these into one closed enum,
`conexus-auth/src/requirement.rs:98` (`Requirement`), with one
`Requirement::check()` match:

* `Cap { cap, reason }` — gated on exactly one capability.
* `Policy { keys, default }` — the worker-toggle policy: an
  agent-bearer caller passes iff at least one of `keys` resolves
  truthy (an explicit per-project override, via a `PolicySource`, else
  `default`); operator-tier callers always bypass this gate.
* `Predicate { check, reason }` — an arbitrary boolean predicate over
  the (possibly absent) Principal, with a mandatory human-readable
  denial reason.
* `Public` — no gate at all. The ONLY way to declare an ungated tool,
  deliberately named so it greps, and cross-checked against a
  hand-reviewed allowlist (`conexus-tools::registry`'s
  `PUBLIC_TOOL_ALLOWLIST` arch test) wherever `all_tools()` is walked.

Every `Tool` impl (`conexus-auth/src/tool.rs:173`) declares exactly
one `Requirement` as an associated const — the single declaration
site; `dispatch()` (`conexus-auth/src/tool.rs:285`) checks it before
ever calling the tool body. This closes a real Python-source
cross-check mechanism (`ToolRequirement.verify(impl)`, needed there
because a tool's authorization lived in TWO disconnected places — the
decorator's stamp and the registration site's `requires=` kwarg — that
could drift) that has no Rust equivalent to port: with one declaration
site, there is nothing left to cross-check.

## Summary table

| Term | What it answers | Type | Defined at |
|---|---|---|---|
| `Principal` | who is calling (MCP side) | struct | `conexus-core/src/principal.rs:39` |
| `RestPrincipal` | which REST door admitted the caller | enum (2 variants) | `conexus-backend/src/rest_principal.rs:53` |
| `PrincipalKind` | which auth mode built this Principal | enum (3 variants) | `conexus-core/src/principal.rs:14` |
| `Capability` | one authorization atom | closed `enum` | `conexus-core/src/capability.rs:43` |
| `Capabilities` | a Principal's resolved cap set (or the sysadmin wildcard) | `enum { Sysadmin, Set(...) }` | `conexus-core/src/capability.rs:194` |
| role bundle | caps granted by a role | `fn -> HashSet<Capability>` | `conexus-core/src/capability.rs:233,261` |
| `AccessTier` | catalog-listing visibility for a *tool* | enum (4 shapes, no `Manager`) | `conexus-tools/src/access.rs:41` |
| `CatalogRole` | catalog-listing bucket for a *caller* | enum (3 variants) | `conexus-core/src/principal.rs:139` |
| `is_operator_tier` | can this caller write project config | `fn -> bool` | `conexus-core/src/principal.rs:122` |
| sysadmin | does this caller hold the wildcard cap | `Capabilities::Sysadmin` variant | `conexus-core/src/capability.rs:194` |
| `is_confirmed_operator_tier` | may this caller see plaintext secrets | `fn -> bool` (×2: MCP + REST) | `conexus-core/src/principal.rs:203`, `conexus-backend/src/rest_principal.rs:198` |
| `Requirement` | what a tool demands of the caller | enum (4 shapes) | `conexus-auth/src/requirement.rs:98` |
| `GateIdentity` | the router's own resolved cookie identity | struct | `conexus-router/src/session_gate.rs:125` |
