<!--
Thanks for the PR. Fill in every section that applies.
"I'll add tests later" is a no — see CONTRIBUTING.md.
-->

## What changed

<!-- One or two sentences. The diff already shows the code. -->

## Why

<!--
The problem this fixes, the capability this adds, or the constraint
this relieves. If there's an issue (in this repo or upstream's),
link it.
-->

## Red → green

<!--
Required for any PR with behavioral change. Build-only PRs (Nix
build hygiene, CI config, font/asset changes) can write "n/a — build
only" here.

- Failing test commit: <sha or test name>
- Passing test commit: <sha or test name>

Backend/router tests live alongside the code (`#[cfg(test)] mod
tests` in the same `.rs` file); dashboard tests live in
`agent_mcp/dashboard/tests/*.test.ts`; Nix/home-manager tests live in
`nix/tests/checks/*.sh`. Name the specific test(s) so a reviewer can
run them locally.
-->

## Upstream issue link (if applicable)

<!--
- This PR resolves issue X in `docs/UPSTREAM_ISSUES.md` of the
  deployment repo: <link to the relevant section>
- Upstream issue / PR (rinadelph/Agent-MCP): <URL if filed>
-->

## Checklist

- [ ] Tests added (or "n/a — build only" above)
- [ ] Rust: `cargo fmt --check` / `cargo clippy --all-targets --locked -- -D warnings` / `cargo test --locked` clean (in `rust/`, `aoe-bridge/`, or both — whichever this PR touches)
- [ ] Dashboard: `npm test` / `npm run lint` / `npm run build` green (`cd agent_mcp/dashboard`) — if touching the dashboard
- [ ] `bash nix/tests/checks/run-all.sh` green — if touching `nix/`
- [ ] CI green on this PR
- [ ] Branch name matches `fix/…`, `feat/…`, `chore/…`, or `upstream/…`
- [ ] Targeting `main` (not directly pushing)
