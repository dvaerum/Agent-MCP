# Dashboard known gaps (unfixed, tracked)

Migrated 2026-09-06 from the deploy repo's (`home-manager-config`,
formerly `nixos-developer-system`) `docs/LEARNINGS.md` §9 and
`docs/UPSTREAM_ISSUES.md` §F — real, still-open Agent-MCP dashboard
bugs found while operating the deployment, misfiled as deploy notes
since this fork had no place to track them at the time. Confirmed
still relevant (not a duplicate of anything already tracked here)
before moving. Not superseded by the Rust migration — these are
dashboard/frontend-side, language-independent of the backend.

## Agents tab counters don't match the row statuses

Create 3 agents on a project, terminate 2. The Agents tab's summary
row shows something like `Total 3 / Running 0 / Pending 0 / Failed 0`
even though the table has 1 system agent + 2 terminated rows — no
bucket accounts for `system` or `terminated` status. The donut/summary
is misinformation; users end up distrusting either the summary or the
table underneath it.

Fix direction: add buckets for `system`/`terminated`, or sum only the
buckets the summary already exposes — whichever, make the two numbers
agree.

## "System Online" sidebar indicator is hardcoded

The dashboard sidebar's online/offline indicator is hardcoded true
upstream; the dashboard actually polls REST `/all-data`, which always
succeeds because the router cold-starts the backend on demand
regardless of whether anything is actually healthy. A real indicator
needs a dedicated health endpoint backed by genuine systemd/process
state, not "the aggregate endpoint didn't 500."

## "Server Management" sidebar item is vestigial

Left over from a host:port multi-server selection UI that doesn't
apply to this project's actual deployment shape (one router, lazily
spawned per-project backends). Harmless but confusing; hide it or
repurpose it rather than leaving a dead menu entry visible.
