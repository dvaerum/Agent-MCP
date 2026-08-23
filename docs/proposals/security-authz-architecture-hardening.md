# Proposal: Security-architecture hardening (authorization seam + SSO identity type)

* Status: **Proposed** (Phase 0 — Findings H, G — landed: PRs #717, #718.
  Step 0's 3 fix-now bugs — landed: PR #721. Phase 1 — Findings B, F, N3
  Tier 1 — in flight.)
* Date: 2026-08-23
* Source: two security-focused `/improve-codebase-architecture` passes.
  Pass 1 ran parallel with pentest-all round 21 (see
  `~/.claude/plans/pentest-all-Agent-MCP.md` for the full pentest ledger it
  draws on) and produced Findings A–H below. Pass 2 (follow-up, after
  Phase 0 landed) sanity-checked A–F's sequencing and surfaced Findings
  N1–N6. Pass 2's own HTML report is an ephemeral `/tmp` artifact, not
  committed (see `docs/learnings/plan-file-citations.md` for why that's a
  deliberate non-problem here) — its file:line evidence is folded into
  each N-finding below verbatim. The full execution plan (this doc's
  ordering plus the exact TDD/delivery steps per item) lives in
  `~/.claude/plans/security-arch-hardening-consolidated.md`.
* Related but distinct: [capability-based-authz.md](capability-based-authz.md)
  — that proposal is about *replacing role tiers with capabilities*
  (`has_role` → `has_capability`), and per its own status note is already
  mostly shipped in the tree. This proposal assumes capabilities already exist
  and is about *how consistently and structurally they're enforced* — the
  mechanism, not the taxonomy. The two don't conflict; finishing that
  proposal's remaining `has_role` migration (if ever needed) is orthogonal to
  everything below.
* Builds on: ADR-0011 (event-driven coordination), ADR-0013 (operator login),
  ADR-0015 (SSO/OIDC), ADR-0020 (router is mount-agnostic)
* Touches: ADR-0015 (see Phase 4 — proposes ADR-0024 to supersede its matching
  algorithm section, which has drifted from the code for 6 rounds)

## Why this exists

Agent-MCP has run 21 rounds of `/pentest-all` since the 2026-08-19 ledger
reset. Severity has trended down (HIGH/MEDIUM → LOW), but the same *shape* of
bug keeps recurring in two lineages:

1. **"Opt-in-and-forget" authorization** (first named OBS-R11-1, round 11) —
   a handler author must remember to call the right helper, in the right
   place, with the right parameters. It's been independently rediscovered
   15+ times across rounds 6–21, most recently R21-F1 (3 more instances found
   by 3 separate pentest lanes in the same round, on top of R20-F4's fix for
   the first batch).
2. **The SSO subject-key lineage** — 5 consecutive rounds (R16→R20) where
   fixing the previous round's finding in `agent_mcp/router/sso.py` seeded
   the next round's finding, in the same handful of functions.

Both are architectural, not per-instance. Continuing to fix instances
one-by-one is what the pentest loop has been doing for 21 rounds; this
proposal is the structural fix that ends the recurrence, per the review's
deletion-test reasoning (see the HTML report,
`architecture-review-agent-mcp-20260823-080634.html`, for full before/after
diagrams and file:line evidence — this doc is the actionable sequencing on
top of it).

## Findings covered

Pass 1 (Findings A–H):

| ID | Finding | Files | Strength | Effort |
|---|---|---|---|---|
| A | Capability is a decorator, not a registration argument — 20/49 MCP tools bypass the pre-schema authz gate (re-verified counts, pass 2; was stale 37/53 pre-R21-F1) — **done**, see Phase 2 | `tools/registry.py`, `tools/access.py`, all `tools/*.py` | Strong | Medium |
| B | `_build_route_principal` hardcodes `project_name=None` → one route duplicates identity construction | `app/_dispatch_helpers.py`, `app/routers/agents.py` | Strong | Trivial |
| C | SSO subject key is an unescaped f-string carrying 4 responsibilities | `router/sso.py` | Strong | Medium |
| D | Backend REST identity is an untyped 3-shape dict + a ContextVar side-channel | `app/deps.py`, `core/operator_tier.py` | Worth exploring | High |
| E | MCP Resources authz is a disconnected 4th mechanism, no capability consulted | `resources/__init__.py`, `core/auth.py` | Worth exploring | Medium |
| F | Agent liveness checked 6 ways, one hand-duplicated constant | `repositories/agent_repository.py`, `tools/scheduled_directive_tools.py` | Speculative | Trivial |
| G | Arch-enforcement test scans a hardcoded 2-module allowlist | `tests/router/test_arch_enforced_revalidation.py` | Speculative | Trivial |
| H | No `CONTEXT.md` — the same identity concept has 3–5 names across modules | repo root | Speculative | Trivial |

Pass 2 (Findings N1–N6, follow-up review, informed by the same pentest
ledger — see `~/.claude/plans/security-arch-hardening-consolidated.md`
for full file:line detail on each):

| ID | Finding | Strength | Effort |
|---|---|---|---|
| N1 | Sanitization is a helper you must remember to call, not a seam — 11 ledger findings across 9 rounds, 5 live bypasses | Strong | Medium |
| N2 | The one structurally-enforced revalidation invariant covers 1 of 3 request surfaces (router admin; backend REST relies on an undocumented proxy-buffering side effect, FLAG-R7-1) — **done**: both remaining surfaces investigated and their real invariants pinned instead of adapters built, see N2 below | Strong | Medium |
| N3 | "What kind of request is this?" answered 11 times by 5 modules — Tier 1 (pure-copy subtractions) done in Phase 1; the SSO-vs-rate_limit trusted-proxy disagreement was investigated and deliberately NOT force-fit (see Phase 1 below); Tier 2 (derived classification) deferred | Strong | Medium |
| N4 | `Registry.visibility` is a listing filter wearing an authorization name for 2 of 3 catalogs — informational input to Phase 2 and Phase 4, no PR of its own | Strong | Low to surface |
| N5 | Long-lived stream re-validation is a convention (4 copies, 1 pattern, 0 seams), not a seam — nothing broken today, pure future-proofing | Worth exploring | Low-medium |
| N6 | Credential lifecycle has no owning module — mint/compare consolidated, redact/rotate scattered; includes 2 fix-now items (Step 0) plus a structural half deferred to Phase 5 | Worth exploring | Medium |

## Sequencing

Two hard constraints shape the order:

1. **File overlap with the active pentest-all round.** Round 21's in-flight
   fixes (R21-F1 through R21-F4) touch `tools/registry.py`,
   `tools/admin_tools.py`, `tools/agent_communication_tools.py`,
   `tools/project_context_tools.py`, `tools/project_settings_tools.py`,
   `tools/task_tools.py`, `features/task_queries.py`,
   `repositories/agent_repository.py`, `repositories/message_repository.py`,
   and `resources/__init__.py` — nearly the entire footprint of Findings A, E,
   and F. Starting those before round 21's fixes merge means rebasing through
   a moving target. **Findings A, E, and F wait until round 21 fully merges**
   (the operator has already set `pause_after_round: 21` in the pentest-all
   config for exactly this reason).
2. **Vocabulary before structure.** Finding H (CONTEXT.md) costs nothing and
   pins the naming every other finding's code will use (`Principal`,
   `capability`, `operator-tier`, `catalog role` — pick one term per concept
   now, not mid-refactor).

### Phase 0 — **done** (PRs #717, #718)

- **H — CONTEXT.md.** Landed. Identity/authorization glossary pins
  `Principal`, `capability`, `operator-tier` vs `sysadmin` vs `catalog role
  "admin"`, `agent bearer`, `forwarding header`.
- **G — dynamic module discovery.** Landed. `test_arch_enforced_revalidation.py`
  now discovers targets by AST-parsing for `perm_gates.require_capability`
  imports instead of a hardcoded 2-module list — immediately found a third
  module (`admin_sso_api.py`) the old list silently skipped.

### Step 0 — **done** (PR #721): 3 fix-now bugs, no architectural dependency

Surfaced by pass 2, none blocking or blocked by anything else: a plaintext
bearer token logged at WARNING (`admin_tools.py`), password-strength policy
skipped at 2 of 4 mint sites (env bootstrap + CLI `create-operator`), and an
SSO fresh-install lockout (`setup_wizard._REDIRECT_EXEMPT_PREFIXES` missing
the SSO callback path).

### Phase 1 — quick wins (in flight)

- **B — thread `project_name` through `_build_route_principal`.** One
  optional parameter + delete the 20-line inline duplicate in
  `app/routers/agents.py`. TDD: RED test asserts `_build_route_principal`
  accepts and threads `project_name` (fails with `TypeError` pre-fix); the
  existing AZ-R14-1 regression suite (forwarding-VIEWER → viewer-role
  Principal, not full operator) guards the refactor doesn't reintroduce
  the original bug.
- **F — dedupe the liveness constant.** `scheduled_directive_tools.py:67`
  imports `TERMINAL_AGENT_STATUSES` from `agent_repository.py` instead of
  redeclaring it. RED test: identity (`is`) assertion between the two names.
- **N3 Tier 1 (subtraction only).** `app/deps.py`'s verbatim
  `_MUTATION_METHODS` copy → import from `auth_middleware`. **The
  `sso.is_trusted_proxy_source` vs `rate_limit` disagreement was
  investigated and deliberately NOT fixed here**: `rate_limit`'s default
  trusted-proxy set includes loopback (`127.0.0.1,::1`) unconditionally,
  while `sso.is_trusted_proxy_source` was deliberately built with *no*
  implicit trust — only the operator-configured
  `AGENT_MCP_SSO_PROXY_TRUSTED_IPS` allowlist. Delegating to the
  "canonical" `rate_limit` helper (pass 2's literal recommendation) would
  have widened SSO's proxy-header trust to implicitly include loopback,
  a real regression caught by the existing
  `test_trusted_header_from_untrusted_source_rejected` test. This is a
  genuine architectural question — should SSO's trust model gain an
  explicit, narrowly-scoped UDS-fronted carve-out, and on whose terms —
  not a mechanical dedup. Surfaced to the operator rather than force-fit;
  N3 Tier 2 (below) is where this should be revisited with a real design,
  not a copy-paste delegation.

### N1 — parallel with Phase 2 (pass 2's top recommendation)

**Sanitization is a helper you must remember to call, not a seam.** File-
disjoint from Phase 2 (`tools/` vs `router/` + `app/main_app.py`), and the
enforcement mechanism (an AST discovery test) is the same idiom Phase 0's
Finding G just proved works. Collapse the three existing wrappers
(`get_sanitized_json_body`, `admin_users_api._json_body`,
`router/app.py:2223`'s `_parse_json_body`) into one entry point, fix the 5
live bypasses (project create/rename, `identity.create_user`'s `username`,
`main_app.py` clientInfo, `sso.py`'s flow cookie, form-encoded credential
paths), and add `test_arch_enforced_sanitization.py`.

**Delivered** as `json_utils.decode_untrusted_body` plus the AST
discovery test. Scope moved in two places, both recorded where a reader
will hit them rather than restated here:

- `sso._decode_flow_cookie` is **deferred to Phase 3**, not fixed — it
  lives in the file Finding C is reworking, so fixing it here would
  collide. It carries a declared exemption in
  `tests/router/test_arch_enforced_sanitization.py` naming Phase 3 as
  its owner; Phase 3 must route it through the seam and delete that
  entry.
- the form-encoded credential paths took the **declared-exemption**
  branch of the "join the seam or declare out of scope" question.
  Identity fields are sanitized at the write instead
  (`identity.create_user`, which now strips `username` as well as
  `email`); the reasoning and its tests are in
  `tests/router/test_arch_n1_form_credentials.py`.

### Phase 2 — the structural lever — **done** (PRs #723, #726, #728, #729, this PR)

- **A — capability as a registration argument.** `register_tool(...,
  requires=Cap("agents.terminate"))` is now a REQUIRED keyword argument
  (no default), and it is **verified** against what the implementation
  actually enforces: a declaration that contradicts the impl's
  `@requires_*` stamp — or a `PUBLIC` declaration on a gated impl — is a
  `ValueError` at import time, not a silent lie.
  - **Enforcement deliberately stayed on the decorator.** The obvious
    reading of "capability as a registration argument" is to have
    `register_tool` apply the gate. That would have been a real
    regression: five call sites invoke a tool impl DIRECTLY, in-process
    (`app/routers/agents.py`, `app/routers/schedules.py` ×3,
    `tools/task_tools.request_assistance`,
    `agent_communication_tools.broadcast_admin_message`'s fan-out,
    `features/task_placement/validator.py`), and a gate applied at
    registration does not travel to any of them. The declaration lives
    at the catalogue; enforcement lives on the function object; import
    time proves they agree.
  - Vocabulary: `Cap(cap)`, `Policy(*keys, default=)`,
    `Predicate(reason)`, `PUBLIC` — all in `core/authorize.py`,
    re-exported from `tools/registry.py`.
  - Final counts: **49 tools, 48 gated, 1 `PUBLIC`** (`test`, the
    fixed-string MCP connectivity probe). The 19 previously in-body-only
    tools were migrated file-by-file across four PRs.
  - RED: `tests/test_arch_enforced_tool_capability_registration.py` —
    a self-discovering sweep over the LIVE registry (no hand-maintained
    tool list) plus a frozen `tools/list`-tier snapshot for all 49, so no
    migration step could change who sees what by accident. It failed for
    19 tools at the start.
  - **One deliberate tier change in the whole migration**:
    `view_project_context` derives `"worker"` instead of `"any"` now that
    the derivation can see its `memories.view` cap. Not a policy change —
    the cap gate already rejected anonymous callers; the tool just stops
    being advertised to a caller that could never invoke it.
  - Predicate vs capability was decided per tool by reading the check
    being replaced. Compound rules (`kind == "agent_bearer" AND cap`,
    `cap_a OR cap_b`, `authenticated AND NOT viewer-tier`) became
    `@requires_predicate`; flattening any of them into a capability
    string would have widened or narrowed the admitted set.
  - `requires_capability(cap, reason=...)` was added so a single-cap gate
    can keep a hand-written, worker-actionable denial message
    (`update_file_metadata`'s, pinned by
    `tests/test_worker_msg_file_tools_clarity.py`) instead of being
    downgraded to the generic text or misusing `@requires_predicate` to
    keep it.
  - **`visibility=` shrank** (per N4, not deleted): 19 kwargs that merely
    echoed a derivable tier are gone; **6 remain**, each doing real work
    — 3 predicate-gated tools whose tier cannot be derived
    (`view_agents`, `send_agent_message`, `broadcast_admin_message`) and
    3 deliberate tightens (`create_task`, `bulk_task_operations`,
    `update_task`). A new invariant test fails if a redundant kwarg
    creeps back.
  - The four in-body denial helpers the original plan named:
    `project_settings_tools._deny_without_config_write_cap` no longer
    existed (that module had already migrated to
    `@requires_capability`); the two `file_management_tools` /
    `file_metadata_tools` compounds became the shared
    `core/authorize.agent_bearer_with_capability(cap)` predicate factory
    (one definition, four call sites); `admin_tools._require_capability`
    **stays** — it still has four live callers
    (`disconnect_agent` / `reconnect_agent` / the two fleet-wide
    variants), which are REST-only impls, not registered MCP tools.
  - **Bug found and class-swept while migrating** (PR #726): a REST
    adapter that dispatches a tool without an `except AuthRejected` arm
    reports a routine 403 denial as a **500**. Two per-site fixes
    (AC-R5-1, R21-F1) had not converged the class; 10 unguarded sites
    remained, 6 of them live. Fixed at all 10 with a self-discovering AST
    backstop (`tests/test_arch_enforced_authrejected_403.py`).

### N4 — read before scoping Phase 2's last step and Phase 4 (no PR of its own)

`Registry.visibility` is authoritative for LIST on all three catalogs
(Prompts/Resources/Tools) but only re-checked at verb time for Prompts —
Resources' read path and Tools' dispatch path each use a different,
parallel mechanism. Two consequences already folded into Phase 2 (above)
and Phase 4 (below); this entry exists so a future reader doesn't
rediscover the asymmetry mid-implementation.

### Phase 3 — parallel with Phase 2, independent files

- **C — `SsoSubject` value type.** Self-contained to `router/sso.py` (plus
  its own new test file); doesn't touch any file Phase 2 touches, so this can
  run in a parallel worktree.
  - TDD: RED tests are (1) a property test — `decode(encode(x)) == x` for a
    fuzzed range of `(iss, sub)` pairs including the round 18-20 collision
    cases — and (2) the exact R18-F1/R19-F1/R20-F1 live repros from the
    ledger, now passing through the typed encode/decode instead of the old
    f-string.
  - Bundle the ADR-0024 write (superseding ADR-0015's matching-algorithm
    section) into this PR — the code change and the doc catching up to it
    belong together.
  - Given the ledger's own finding that this whole path is dead code on the
    live pentest target (BUILTIN mode, not PROXY_HEADER), this phase is safe
    to land without needing a live OIDC IdP to test against — the unit/
    property tests are the real gate here, not an end-to-end SSO login.
  - **Widened per N3**: fold in `sso._cookie_secure_flag` (`:1375-1404`, an
    admitted duplicate of `login.cookie_secure_flag` that already produced
    R6-F3) — same file, already checked out, cheap to add.

### N2 — after Phase 2 — **done**

**Widen the arch-enforcement invariant to all 3 request surfaces.**
`test_arch_enforced_revalidation.py` (post-Phase-0) discovers targets
dynamically but only globs `agent_mcp/router/*.py` — router admin is
enforced, backend REST (40+ handlers) relies on `_proxy_to_backend`'s
buffer-then-forward as an *undocumented* security property (FLAG-R7-1),
MCP tools have 2 ad-hoc re-checks. Sequenced after Phase 2 because its
first question — does backend REST need its own adapter, or is it
genuinely immune because the proxy buffers? — is easier to answer once
Phase 2 has established what a per-surface authorization adapter looks
like. An acceptable outcome is documenting + testing the buffering
invariant explicitly rather than building a redundant adapter.

**Outcome (that acceptable one, for both remaining surfaces).** The
investigation found no adapter is warranted today, and the two new test
files carry the full reasoning rather than repeating it here:

- **Backend REST** — genuinely immune *as deployed*, and the reason is
  a single line: `_proxy_to_backend` materialises the whole client body
  before it opens the backend socket, and both proxy entry points
  (`backend_api_handler` for the entire `/api/<project>/…` surface,
  `backend_mcp_handler` for `/mcp`) route through it, so the
  caller-paced window is spent at the router. Pinned behaviourally and
  by AST in
  `tests/router/test_arch_n2_proxy_buffers_before_backend.py`, with the
  security rationale added to the comment at the body-materialisation
  step in `router/app.py` (it previously gave only the
  proxying-correctness reason). **Scope caveat, deliberately not closed
  here:** a backend reached *directly* (the misconfiguration posture
  `app/deps.py::_backend_project_name` already hardens against) has no
  buffering in front of it and the class is open there. Closing that
  means a re-validation adapter across 40+ FastAPI handlers, which
  wants Finding D's typed `Principal` first — Phase 5, not N2.
- **MCP tools** — the plan's framing of "2 ad-hoc `_agent_assignable`
  re-checks" does not survive contact: `_agent_assignable` validates the
  assignment *target*'s liveness inside the write transaction, not the
  *caller*'s authority, so it is not a re-validation analogue at all and
  extending it would have enforced the wrong thing. What the surface
  actually rests on is checked instead, per registered tool (49
  parametrized cases, discovered from the live registry) in
  `tests/test_arch_n2_tool_surface_yield_points.py`: the tool layer holds
  no transport/stream handle, so no caller-paced yield point can exist
  in it, and `dispatch_tool_call` has zero yield points before *or*
  between its authorization gate and the tool invocation — `perm_gates`'
  fusion property, arrived at by construction and therefore exactly as
  easy to break with one added `await`. `wait_for_events`' indefinite
  hold is a stream lifetime, which is **N5**'s subject, not this one's.

### Phase 4 — **done** (this PR): E, Resources only

- **E — shared `decide()` seam.** Delivered as `core/access.py`'s
  `decide(Request) -> Decision`; the module docstring carries the design
  (why the role always comes from `catalog_role`, why denials are
  *classified* rather than phrased, and the shape a later Prompts / Tools
  migration would use). `resources/__init__.py::resolve_agent_id_for_uri`
  is wired through it and keeps its signature + `ResourceReadError`
  contract — only *where* the decision is made moved.
  - **Per N4, the gap actually closed**: the read path now re-checks
    `entry.visibility` (the declaration `resources/list` filters on), so an
    `"admin"`-visibility resource can no longer be hidden from a worker's
    `resources/list` and then served to that same worker by
    `resources/read` if they guess the URI. Both shipped resources are
    `visibility="any"`, so the RED test registers a synthetic admin-only
    one — see `tests/test_phase4_decide_seam.py`.
  - **No policy change**: `catalog_role` and `resolve_visibility` needed no
    reconciling because `decide()` *delegates* to the former and feeds its
    result to the latter, rather than re-deriving admin-ness. That
    equivalence (LIST-visibility == READ-visibility, per Principal shape) is
    pinned parametrically over every shape reachable in production.
  - MCP resource *subscriptions* stay out of scope — `main_app.py` never
    registers them, so the vendored SDK never advertises the capability.
- **Follow-up, deliberately NOT in this pass** (the scope cut, restated so
  it doesn't read as an omission): Prompts and Router admin are not
  migrated onto `decide()`. Prompts already re-checks `visibility` at verb
  time (`PromptRegistry.render`), so its migration is a consolidation, not
  a fix; Router admin needs Phase 5's typed `Principal` first.

### N3 Tier 2 + N5 — after Phase 4, or explicitly deferred

Lower urgency, both "generalize an idiom that already works," nothing
broken today. **N3 Tier 2**: derive request classification (public-path?
delivery route? which project?) from route-registration metadata instead
of hand-maintained literal tuples, mirroring `app.py`'s existing
`_add_admin_trailing_slash_aliases` idiom — this is also where the
Phase-1-deferred SSO-trusted-proxy question belongs, designed properly
rather than delegated wholesale. **N5**: fuse the four independently-
implemented long-lived-stream re-validation loops (`events.py`,
`delivery.py`, `main_app.py`'s SSE pump, `wait_for_events`) behind one
seam, mirroring `perm_gates`' fusion idiom applied to the streaming
lifecycle. Fine to defer past this plan's initial pass if time-boxed —
flag explicitly rather than silently dropping.

### Phase 5 — largest effort, do last

- **D — typed `Principal` for backend REST, replacing the 3-shape dict.**
  Blocked today by `tests/test_sec_r4_operator_identity_race.py` pinning the
  dict literal verbatim. This phase's first step is updating that test to
  assert on `Principal` fields instead of dict shape (a deliberate,
  reviewed test change — not a silent loosening: the pinned invariant
  itself must survive, just expressed against the new type).
  - TDD: keep the race-condition property that test protects; add a RED test
    proving the ContextVar (`_forwarding_route_role`) is no longer read
    anywhere once the dict is gone.
  - This is the highest-effort phase and the one most likely to surface
    unexpected callers relying on the dict shape (40+ handlers) — budget the
    most review time here, and consider doing it as several smaller PRs
    (one subsystem of routers at a time) rather than one big-bang migration.
- **N6 structural half — do alongside D** (every response builder gets
  touched anyway): consolidate the four independent agent-bearer redaction
  mechanics (`admin_tools.py:2093` string-overwrite, `composition.py:443`+
  `:410-418` pop+rename, `composition.py:266-277` SQL column allowlist,
  `settings.py:101-112` binary 403-or-plaintext) into one redactor — all
  four already agree on the *who* (`is_confirmed_operator_tier`), only the
  *what* differs. Surface (don't unilaterally decide in this PR) the
  `rotate_token()` question: it exists, fully implements
  rotate-with-cache-rekey, has zero callers — either give it a caller or
  delete it; that's a product decision for the operator, not a refactor
  call. (N6's two fix-now items — the plaintext-bearer log line and the
  password-policy gap — are already done, Step 0/PR #721.)

## Delivery mechanics (same discipline as pentest-all's fix agents)

- One git worktree per phase, off the `main` HEAD at the time the phase
  starts (not a shared long-lived branch).
- TDD red-first: the red test in each phase above is the starting point, not
  an afterthought.
- Full local suite green (`pytest -n 2`, not `-n auto` if run concurrently
  with anything else on this host — see
  `docs/learnings/shared-host-test-parallelism.md`) before opening a PR.
- No `--no-verify`, no `git add -A`, no version bump per PR.
- Commits carry the same `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`
  trailer used throughout this session's other work on this repo.
- Merge gate: local green (this repo's current `auto_merge_on_green: local_only`
  convention), spot-check remote CI after merge rather than blocking on it.

## Explicit non-goals

- Not migrating `has_role` → `has_capability` call sites — that's
  `capability-based-authz.md`'s remaining work, parked pending a concrete
  multi-tenant-permission need. Nothing here depends on it.
- Not building the `group_capability` dashboard UI — same proposal, same
  parked status.
- Phase 4 (Finding E) does not migrate Prompts or Router admin onto
  `decide()` in its first cut — flagged above as a deliberate scope cut, not
  an oversight.
- No behavior change to what any role/capability is *allowed* to do anywhere
  in this plan — every phase is a mechanism change (where/how a check runs),
  never a policy change (who passes the check). Any phase whose tests can't
  stay green on that constraint should stop and get a second look before
  proceeding, not get force-fit.
