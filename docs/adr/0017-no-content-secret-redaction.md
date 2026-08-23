# ADR-0017: No content-based secret detection or redaction

* Status: Accepted
* Date: 2026-07-15
* Plan: "prancy-napping-pie" Wave 12 — ephemeral plan file, never
  committed, no longer available; this ADR is the durable record
* Supersedes: the content-scanning approach of the pentest R2-F3 /
  R2-F3b / R3-F2 / R4 / R5 / pentest-R1 secret-in-content findings
* Builds on: ADR-0016 (separate project config from project memory)

## Context

Rounds 2-5 of the pentest hardening tried to keep secrets out of shared
project content by **detecting and censoring them in the content itself**:
a key-name vocabulary (`is_secret_key` — `token` / `secret` / `password` /
`api_key` / …) plus a value/description scanner
(`_value_has_embedded_secret` — well-known token prefixes AND two
high-entropy runs, `_HIGH_ENTROPY_DENSE_RE = [A-Za-z0-9+/=]{24,}` /
`_HIGH_ENTROPY_LONG_RE = [A-Za-z0-9+/=_-]{40,}`). That detector was wired
into every project_context surface (the `view_project_context` tool, the
RAG index + query + repository seams, the dashboard composition REST
reads, the graph-data node builder, and the Memories-UI TSX), and into a
one-time RAG purge that evicted "secret-bearing" chunks from the index.

Two symptoms, both diagnosed live against the washing-brothers
deployment, proved this is the wrong tool:

1. **False-positive over-redaction.** The value scanner fires on ordinary
   file paths, GitHub URLs, long slugs, base64-ish blobs, commit SHAs, and
   camelCase method names. It masked ~11 of 25 legitimate washing-brothers
   memory notes behind `[redacted]` AND excluded them from RAG semantic
   search. None were secrets. The operator could not read their own
   project's architecture/status notes.
2. **False negatives give false confidence.** A denylist can never be
   complete — a live worker exfiltrated real credentials verbatim through
   `ask_project_rag` simply by using formats the denylist didn't match
   (a `postgres://user:pass@host` DSN, a `sk_live_` Stripe key, base32
   TOTP seeds, short hex keys). Every gap patched spawned the next
   whack-a-mole round.

Heuristic content-scanning is unreliable in **both** directions. The
right axis of protection is **authorization** (who may read the project),
not **guessing content**.

## Decision

agent-mcp does **not** detect-and-censor secrets in content — anywhere.
Scope is everywhere shared project content lives: **memory**
(`project_context`), **task** descriptions/titles, **code** files, and
**markdown** docs. All of it is indexed and returned **AS-IS**.

Terminology, stated plainly (this ADR is the canonical home):

* **shared project content** = memory + tasks + code + markdown. Authored
  by agents and operators, RAG-indexed, readable by any principal
  authorized on the project. NOT a secret store.
* **real secrets** = credentials, keys, tokens. They belong in **sops
  refs** (referenced by path, e.g. the existing `config_aoe_bearer_token_file`
  pattern) or the operator-only, non-RAG **`project_settings`** store
  (ADR-0016). They are NEVER raw values in shared project content.

Concretely:

1. **Delete the content-detection machinery.** `is_secret_key` +
   `_SECRET_SUFFIX_RE` + `_CAMEL_BOUNDARY_RE`
   (`tools/project_context_tools.py`); `_value_has_embedded_secret` +
   `_EMBEDDED_SECRET_PATTERNS` + the high-entropy runs + the one-time
   `_CTX_SECRET_PURGE` / `_ALLSOURCE_SECRET_PURGE` purge helpers
   (`features/rag/indexing.py`); the `_is_secret_key` /
   `_value_has_embedded_secret` / `_drop_secret_chunks` /
   `_drop_secret_tasks` / `_scrub_secret_parts` seams and their call
   sites (`features/rag/query.py`); the repository-seam versions + the
   `bulk_index_chunks` ingest scan + the `fetch_recent_context` filter
   (`repositories/rag_repository.py`); `_context_value_should_redact` +
   the three composition call sites + `_redact_context_row` /
   `_REDACTED_VALUE` (`app/routers/composition.py`); the `/graph-data`
   node-description redaction (`features/dashboard/api.py`); and the
   Memories-UI key-name masking (`memories-dashboard.tsx` /
   `view-memory-modal.tsx`).
2. **Protection is by authorization.** RAG retrieval is per-project, and
   task retrieval stays **ownership-scoped** (`_drop_unowned_task_chunks`
   / `_task_ownership_sql` in `query.py`) — a worker still only retrieves
   task chunks it is entitled to via `view_tasks`. That is an
   authorization control (who owns the task), not content-guessing, so it
   SURVIVES.
3. **The `project_settings` store keeps ITS redaction.** Real secrets in
   the operator-only settings store (the AoE bearer) still mask for
   non-confirmed tiers via `_SECRET_SETTING_KEYS` / `redact_settings_row`
   (`tools/project_settings_tools.py`). That store is a genuine secret
   store with a known, literal schema — the exact opposite of guessing at
   free-form content. It is untouched by this wave.
4. **The operator is confirmed-tier on their own project.** Wave 12 PR A
   (`composition.is_confirmed_operator_tier` + `deps.py`) stops
   discarding the cookie operator's resolved `project_role`/`sysadmin`,
   so a genuine operator is never redacted from their own project's
   settings-store secrets or agent bearer tokens. That tier check still
   gates the surviving surfaces (agent BEARER tokens on `/api/all-data` +
   `/api/tokens`, and settings-store secrets on `/api/settings-data`).
5. **`config_*` stays unwritable to memory (ADR-0016).** The
   `^config_` write-rejection (`_CONFIG_KEY_RE` + `_check_write_authorization`
   + the bulk-delete guard) is NOT part of the redaction machinery and is
   kept — config can never be smuggled back into the RAG-indexed store.

## Consequences

### Positive

* Operators see their own data. The washing-brothers memory notes render
  in FULL and are RAG-retrievable again.
* RAG stops dropping legitimate content from the index and from answers;
  semantic search covers the whole corpus.
* ~600 lines of unreliable heuristic (detector + purges + per-seam drops +
  composition/graph redaction + TSX masking) are deleted. One
  comprehensible model replaces five layers of "belt-and-suspenders"
  content scanning that never actually held.
* No more whack-a-mole: a future pentest sweep can't reopen the class by
  patching "one more regex gap" — there is no regex to patch.

### Negative / Residual risk

* **A secret PASTED INTO shared project content is retrievable** by that
  **same project's** worker agents via RAG, and visible to project
  members on the dashboard reads. This is bounded to the **same project**
  — never cross-tenant: RAG is per-project and task retrieval is
  ownership-scoped, so a project-A worker cannot retrieve project-B
  content. It is the accepted trade for a reliable model.
* Mitigations: the pre-merge safety pre-sweep (a read-only, high-precision
  audit of memory + tasks + code/markdown that surfaces genuine
  secret-shaped values to the operator to relocate BEFORE the net is
  removed — the live deployment scanned clean, 0 real secrets), the
  "secrets belong in sops / the settings store" principle, and an
  optional write-time WARN (deferred — see below).

## Supersedes

This ADR is the record that **stops a future pentest sweep from
re-adding the content scanner**. The following findings moved the wrong
control (content-scanning) and are superseded — their residual risk is
accepted per "Negative / Residual risk" above:

* **R2-F3** — non-context embedded-secret leak via the `rag_chunks`
  ingest / `search_similar` seam.
* **R2-F3b** — live-task title/description secret leak via the raw task
  fetch + assembly-seam scrub.
* **R3-F2** — secret-detector denylist gaps (novel token formats).
* **R4** — value-embedded-secret backstop on the composition REST reads.
* **R5 / pentest-R1** — secret-in-description leak on the composition
  reads + the `/api/graph-data` node title.

The AUTHORIZATION-based control from the same era — **R4-F4 / R5-F1** RAG
task ownership scoping — is explicitly KEPT (it is not content-guessing).

## Verification

* `tests/test_wave12_prb_no_content_redaction.py` — the RED→GREEN
  headline: a secret-named memory row with a live-credential-shaped value
  is returned IN FULL via `view_project_context` (worker AND operator),
  `/api/context-data` + `/api/all-data`, and the RAG live-context path.
* Migrated (redact → return-in-full) suites:
  `test_sec_r2_secret_redaction`, `test_sec_r3_secret_leak`,
  `test_view_project_context_redaction`, `test_sec_rag_secret_redaction`,
  `test_sec_r4_composition_backstop`, `test_sec_r5_composition_description`,
  `test_sec_pentest_r1_graph_data_redaction`,
  `test_arch_r2_4_rag_secret_seam`, `test_arch_r5_4_rag_query_dedup`,
  `test_sec_composition_policy_readback`,
  `test_wave6_pr3_project_context_e2e`, `test_aoe_notification`
  (re-pointed to the settings-store membership).
* Deleted detector-unit / non-memory-RAG suites:
  `test_sec_r3_f2_secret_detector_gaps`,
  `test_sec_r2_f3_noncontext_secret_leak`,
  `test_sec_r2_f3b_live_task_secret_leak`,
  `test_dashboard_secret_redaction`.
* KEPT untouched (the surviving controls):
  `test_wave11_project_settings_store` (settings-store redaction),
  `test_sec_r4_f4_rag_task_ownership_scope` (ownership scoping),
  `test_wave12_pra_operator_tier` (PR A operator-tier confirmation),
  `test_sec_viewer_read_gating` (admin-tool token masking).
