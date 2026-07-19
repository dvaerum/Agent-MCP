# ADR-0019: Agent self-service profiles; retire structured capability-tag routing

* Status: Accepted
* Date: 2026-07-19
* Plan: `agent-profile-self-service.md` — "Agent self-service profiles
  (+ freshness nudge, peer roster, retire tag-routing)"
* Supersedes: nothing
* Builds on: ADR-0016 (config in `project_settings`), ADR-0018
  (settings-schema registry)

## Context

Agents had no self-authored, human-readable description of what they do —
"what I work on, what tools I have, how I work, what to ask me about."
Coordination (`request_assistance`, task routing decisions) had nothing
to consult to answer "who do I ask?". The one structured field that
_looked_ like it should serve routing — the JSON `agents.capabilities`
tag list, matched against `tasks.required_capabilities` in the
unassigned-task collector via a `required ⊆ caps` subset test — was
**never used** in any project on the host (a design-time scan found zero
agents with tags set and zero tasks with required tags), and the filter
is already vacuous because empty-required matches everyone. So it routed
nothing while overloading the word "capabilities" (which now also names
the Wave-9 authorization vocabulary in `core/capabilities.py`).

## Decision

1. **One free-text `profile` per agent, self-authored.** A single new
   `profile` TEXT column on `agents` (migration 0018), NOT a tag list. It
   is **routing-neutral** — editing it has zero operational side effect,
   which is what keeps the governance story simple.

2. **Review-vs-change bookkeeping.** Two timestamps + an editor on
   `agents`:
   * `profile_updated_at` — bumped **only on content change** (drives the
     peer broadcast).
   * `profile_reviewed_at` — bumped on **every** review, even a no-op
     confirm (drives the staleness nudge). A forced review that changes
     nothing moves only this, so peers are never spammed.
   * `profile_updated_by` — agent_id of the last content editor. The
     peer-broadcast excludes the **editor**, not the subject: a manager
     editing a worker notifies the worker, not the manager.

3. **One self-service tool, `update_agent_profile`.** The server hashes
   the submitted content: always bump `reviewed_at`; if the hash differs
   from stored, also bump `updated_at` + `updated_by` and enqueue the
   peer event. Calling with no `profile` arg = "confirm still accurate"
   (bumps `reviewed_at` only).

4. **Governance via toggles, not a safety gate.** Because profile edits
   are routing-neutral, the operator gets on/off preferences rather than
   a hard gate: `config_allow_worker_update_own_profile` (default True),
   `config_allow_manager_update_own_profile` (default True),
   `config_allow_manager_curate_profiles` (default True). Managers may
   edit any **worker's** profile in the project (no subordinate tree
   exists — `agent_role` is only `worker|manager`, so "team" = all
   workers); managers may **not** edit other managers.

5. **Managers seeded with a charter; workers start blank.**
   `register_agent_tool_impl` stamps `MANAGER_DEFAULT_PROFILE`
   (`core/agent_profile_defaults.py`) with
   `reviewed_at = updated_at = created_at` and `updated_by = NULL` so a
   fresh manager is not instantly "stale" and the seed fires no peer
   broadcast. Workers get NULL.

6. **Self-read rides the event loop, NOT `get_system_prompt`.** (Locked
   design decision.) The agent's own profile is surfaced in a
   `profile_review` section on `wait_for_events` / `fetch_events_since`
   — fired on the first event-loop call of a session (greet-once) OR when
   overdue. **Nothing goes into `get_system_prompt`**: zero standing
   per-turn token cost; the profile appears exactly when review is being
   asked for. (This is why the PR1 slice does not touch
   `get_system_prompt` — the self-read integration lands with the PR3
   `profile_review` surface.) The peer roster tool `view_agents` gives
   any agent the whole team's profiles for "who do I ask?".

7. **Retire structured capability-tag routing.** `agents.capabilities`,
   `tasks.required_capabilities`, and the `req ⊆ caps` subset match are
   removed (migration 0019 physically drops both columns via
   `batch_alter_table`, carrying forward all constraints/indexes). This
   is **behaviour-preserving**: the filter was already a no-op, so
   unassigned-task events already notified everyone. The word
   "capabilities" now unambiguously means the authorization vocabulary in
   `core/capabilities.py`.

## Consequences

* Coordination gains a human-readable roster to route against; the
  staleness nudge keeps it current without any per-turn prompt cost.
* One overloaded word ("capabilities") collapses to one meaning.
* The 0019 column drop is the feature's highest-risk migration (two of
  the busiest tables) and carries a dedicated migration-path test plus a
  migration-path VM E2E.

## Rollout (5 PRs)

PR1 foundation (schema, tool, manager seed, config keys, this ADR) →
PR2/PR3/PR4 in parallel (peer broadcast / event-loop review surface /
`view_agents`) → PR5 (retire tags + column drop, after PR2) → release
bump. Each PR is independently CI-green and TDD red/green.
