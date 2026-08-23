# Proposal: Security-architecture hardening (authorization seam + SSO identity type)

* Status: **Proposed** (Phase 0 — Findings H, G — landed: PRs #717, #718)
* Date: 2026-08-23
* Source: security-focused `/improve-codebase-architecture` review, run in
  parallel with pentest-all round 21 (see
  `~/.claude/plans/pentest-all-Agent-MCP.md` for the full pentest ledger this
  review draws on)
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

| ID | Finding | Files | Strength | Effort |
|---|---|---|---|---|
| A | Capability is a decorator, not a registration argument — 37/53 MCP tools bypass the pre-schema authz gate | `tools/registry.py`, `tools/access.py`, all `tools/*.py` | Strong | Medium |
| B | `_build_route_principal` hardcodes `project_name=None` → one route duplicates identity construction | `app/_dispatch_helpers.py`, `app/routers/agents.py` | Strong | Trivial |
| C | SSO subject key is an unescaped f-string carrying 4 responsibilities | `router/sso.py` | Strong | Medium |
| D | Backend REST identity is an untyped 3-shape dict + a ContextVar side-channel | `app/deps.py`, `core/operator_tier.py` | Worth exploring | High |
| E | MCP Resources authz is a disconnected 4th mechanism, no capability consulted | `resources/__init__.py`, `core/auth.py` | Worth exploring | Medium |
| F | Agent liveness checked 6 ways, one hand-duplicated constant | `repositories/agent_repository.py`, `tools/scheduled_directive_tools.py` | Speculative | Trivial |
| G | Arch-enforcement test scans a hardcoded 2-module allowlist | `tests/router/test_arch_enforced_revalidation.py` | Speculative | Trivial |
| H | No `CONTEXT.md` — the same identity concept has 3–5 names across modules | repo root | Speculative | Trivial |

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

### Phase 0 — now, zero file overlap, start immediately

- **H — CONTEXT.md.** Write the identity/authorization glossary: `Principal`,
  `capability`, `operator-tier` vs `sysadmin` vs `catalog role "admin"` (pick
  ONE canonical term for each and note the others as deprecated synonyms to
  grep-replace over time), `agent bearer`, `forwarding header`. ~1 hour,
  no code change, no risk.
- **G — dynamic module discovery.** `test_arch_enforced_revalidation.py`
  currently hardcodes 2 target modules; walk every module importing
  `perm_gates.require_capability` instead. Pure test-infra change, own
  worktree, own PR, no production code touched — safe to land immediately
  regardless of round 21's state.

### Phase 1 — as soon as round 21 merges, quick wins

- **B — thread `project_name` through `_build_route_principal`.** One
  optional parameter + delete the 20-line inline duplicate in
  `app/routers/agents.py`. TDD: RED test is a forwarding-VIEWER hitting
  `agents.py`'s route and getting the full operator bundle (mirrors AZ-R14-1's
  original repro). Trivial effort, own PR.
- **F — dedupe the liveness constant.** `scheduled_directive_tools.py:67`
  imports `TERMINAL_AGENT_STATUSES` from `agent_repository.py` instead of
  redeclaring it. RED test: assert identity (`is`) between the two names
  before the fix would fail; after, it's a no-op import. Trivial, bundle with
  B in the same PR if file-disjoint, else its own tiny PR.

### Phase 2 — the structural lever

- **A — capability as a registration argument.** This is the big one:
  `register_tool(..., requires=Cap("agents.terminate"))` becomes a required
  argument (no default), stamped automatically so `dispatch_tool_call`'s
  pre-schema gate and `access.py`'s visibility derivation both see it.
  - TDD: RED test is a parametrized sweep over all 53 tool names asserting
    each carries a stamped requirement OR is explicitly declared
    capability-free (a tiny allowlist for the truly public tools, if any
    exist) — this test should fail loudly today for the ~34 remaining
    undecorated tools (3 will already be fixed by R21-F1 once it merges).
  - Migrate file-by-file (`admin_tools.py`'s 12, `project_context_tools.py`'s
    7, `agent_communication_tools.py`'s 5, `project_settings_tools.py`'s 3,
    `task_notes_tools.py`'s 3, `file_management_tools.py`/
    `file_metadata_tools.py`'s 4, `rag_tools.py`, `agent_roster_tools.py`) —
    each migration is mechanical (move the existing capability string from
    the in-body call into the `requires=` kwarg) and independently
    verifiable, so this can be one PR per file or a few grouped PRs, not one
    giant PR.
  - Last step: delete `access.py`'s step-3 hand-synced `visibility=` fallback
    and the four in-body denial helpers
    (`admin_tools._require_capability`,
    `project_settings_tools._deny_without_config_write_cap`, the 2 verbatim
    three-clause compounds in `file_management_tools.py`/
    `file_metadata_tools.py`) once zero call sites remain — same
    subtraction-is-the-completion-proof shape as the capability-based-authz
    proposal's own Phase 3.
  - This phase's own worktree, off whatever `main` is once round 21 merges.

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

### Phase 4 — after Phase 2 lands (needs its adapter shape)

- **E — shared `decide()` seam for Tools/Resources/Prompts/Router.** Depends
  on Phase 2 existing so the Tools adapter has a concrete shape to mirror.
  Scope this phase narrowly at first: build `core/access.py`'s `decide()` +
  `Request`/`Decision`, migrate the Resources surface only (closing R21-F4's
  bug class structurally instead of the two-line reorder pentest-all already
  shipped), and leave Prompts/Router as a documented follow-up rather than
  doing all four surfaces in one PR.
  - TDD: RED tests are the R21-F4 repro (admin cross-agent resource read) plus
    the existing non-admin-denied regression test, both now going through
    `decide()`.

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
