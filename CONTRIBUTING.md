# Contributing to dvaerum/Agent-MCP

This is a maintained fork of [`rinadelph/Agent-MCP`](https://github.com/rinadelph/Agent-MCP).
Upstream has been effectively dormant since October 2025, so this fork
hosts the active work — bug fixes, security hardening, and the
deployment-related changes that previously lived as an out-of-tree
patch series in [nixos-developer-system].

If you're here because something is broken, file an issue. If you're
here to fix it, read on.

The original upstream `CONTRIBUTING.md` is preserved in git history at
sha `13d98b2` if you want the generic OSS-onboarding view.

[nixos-developer-system]: https://cms.best.aau.dk/dennis/nixos-developer-system

## Development setup

The backend/router implementation (`rust/` — `conexus-backend`,
`conexus-router`, `conexus-tools`, `conexus-db`, `conexus-auth`,
`conexus-core`, `conexus-wakeloop`, `conexus-cli`,
`conexus-daemon-agent`, `conexus-vec`) is Rust. The dashboard
(`agent_mcp/dashboard/`) is a Next.js/TypeScript frontend, unrelated
to that migration — it was never Python and didn't need to move. Both
implementations were originally Python; see
`~/.claude/plans/prancy-napping-pie.md` (or its final state in this
repo's history) for the full migration record if you're curious why
the tree is shaped this way.

Prerequisites:

- **Rust** (stable toolchain — `rustup toolchain install stable
  --component rustfmt --component clippy`)
- **Node.js 22.x** (for the dashboard)
- **Nix** with flakes enabled (for packaging/deployment checks —
  optional unless you're touching `nix/`)
- **Ollama** with a small embedding model (`qwen3-embedding:0.6b`
  recommended; or any OpenAI-compatible embedding endpoint via
  `OPENAI_BASE_URL` / `OPENAI_API_KEY`) — only needed for the RAG
  indexer/query path

```sh
git clone https://github.com/dvaerum/Agent-MCP.git
cd Agent-MCP

# Backend/router (rust/)
cd rust
cargo build --workspace --locked
cargo test --workspace --locked
cargo clippy --workspace --all-targets --locked -- -D warnings
cargo fmt --all --check
cargo audit
cd ..

# Dashboard (agent_mcp/dashboard/)
cd agent_mcp/dashboard
npm ci
npm test          # vitest — source-grep regression guards, no jsdom
npm run lint
npm run build
cd ../..

# Nix/home-manager-module regression checks
bash nix/tests/checks/run-all.sh
```

OpenAI API key not required — local Ollama works fine. Set
`OPENAI_BASE_URL=http://127.0.0.1:11434/v1` and any non-empty
`OPENAI_API_KEY`.

## Branch layout

- **`main`** — production HEAD. The Nix deployment pins to a sha on
  this branch. Every accepted change lands here via PR.
- **`upstream-mirror`** — fast-forward only; mirrors
  `rinadelph/Agent-MCP:main`. Useful for `git log upstream-mirror..main`
  to see "what did we add", and as the base for cherry-picks intended
  for upstream PRs.
- **Topic branches** off `main`:
  - `fix/<short-slug>` — bug fix
  - `feat/<short-slug>` — new capability
  - `chore/<short-slug>` — build, CI, docs, refactor
  - `upstream/<short-slug>` — branch off `upstream-mirror`, used only
    when we're preparing a PR to send back to upstream

Don't push directly to `main`. Don't push to `upstream-mirror` from
your machine — it ff-syncs from upstream:

```sh
git fetch upstream
git checkout upstream-mirror
git pull --ff-only        # pulls upstream/main into upstream-mirror
git push origin upstream-mirror
```

(If `upstream` isn't a remote yet:
`git remote add upstream https://github.com/rinadelph/Agent-MCP.git`.)

## TDD: red, green, ship

Every PR with behavioral change must include:

1. A failing test that demonstrates the bug or missing capability
   (the "red" commit).
2. The minimum code change that makes it pass (the "green" commit).
3. Optional refactor commits after green.

Build-only PRs (Nix build hygiene, CI config, font/asset changes) are
exempt — they just need to keep CI green.

Backend/router tests live alongside the code they test, `#[cfg(test)]
mod tests` in the same file (the convention throughout `rust/`) — not
a separate `tests/` tree. Prefer a real fixture over a mock: a real
temp-file-backed SQLite connection (`rusqlite`/`sea-orm` both point at
the same file when a test needs both), a real bound TCP listener for
an HTTP dependency, a real subprocess for a CLI/systemd contract —
this project's own established discipline, not a stylistic preference.
Dashboard tests live in `agent_mcp/dashboard/tests/` (vitest) — mostly
source-text regression guards (no jsdom/RTL setup in this repo), since
most of what they pin is a property of the source, not runtime
behaviour. Nix/home-manager regression checks live at
`nix/tests/checks/*.sh` (run via `bash nix/tests/checks/run-all.sh`).
There is no repo-root `tests/` tree anymore — every test lives next to
what it tests.

End-to-end tests against a real systemd + Ollama deployment live in
[nixos-developer-system/users/dennis/agent-mcp/tests/] and are run
manually as part of release verification — **not** part of CI here.

[nixos-developer-system/users/dennis/agent-mcp/tests/]: https://cms.best.aau.dk/dennis/nixos-developer-system/src/branch/main/users/dennis/agent-mcp/tests

## CI must pass

`.github/workflows/ci.yml` runs, per job:

- **`conexus` / `aoe-bridge`** (Rust) — `cargo fmt --check`,
  `cargo clippy --all-targets --locked -- -D warnings`,
  `cargo test --locked`, `cargo audit`, each over its own workspace
  (`rust/`, `aoe-bridge/` — unrelated release cadences, deliberately
  separate jobs/caches).
- **`dashboard`** — `npm ci`, `npm test` (vitest), `npm run lint`,
  `npm run build`, plus a CSS-bundle-size sanity check (a dead
  `@import` can make `next build` exit 0 while silently emitting a
  near-empty stylesheet — see
  `docs/learnings/next-build-swallows-unresolvable-css-import.md`).
- **`nix-checks`** — `bash nix/tests/checks/run-all.sh` (Nix
  eval/build regression guards + a couple of bash-script/`.gitignore`
  contract checks that have nothing to do with Nix specifically but
  live there now that there's no Python interpreter to run them
  under).
- **`nix-flake-check`** — the 4 nixosTest VM checks (sharded, one per
  runner), exercising a full server boot + real HTTP assertions.
- **`dependency-audit`** — `npm audit` (dashboard deps; Rust's own
  `cargo audit` runs inside the `conexus`/`aoe-bridge` jobs above).

Red CI blocks merge.

## PR template

`.github/PULL_REQUEST_TEMPLATE.md` is auto-populated on every new PR.
Fill in every section that applies. "I'll add tests later" is a no.

## Upstreaming a fix to rinadelph/Agent-MCP

Upstream is still Python; this fork's backend/router is now Rust
(`rust/`). A `git cherry-pick` of a backend/router fix genuinely
applies only if upstream's equivalent Python code still exists in
the same shape — check before assuming it applies. Dashboard fixes
(`agent_mcp/dashboard/`) are unaffected by the language split and
cherry-pick the same way they always did.

When a fix is general (i.e. anyone running upstream could use it,
not just our deployment) and still applies as a literal patch, open
a PR against upstream too:

```sh
git fetch upstream
git checkout -b upstream/<slug> upstream-mirror
git cherry-pick -x <merged-fix-sha>     # -x records the cherry-pick source
git push origin upstream/<slug>
gh pr create -R rinadelph/Agent-MCP --base main \
  --title "..." --body "..."
```

Expect months of latency on review (upstream is dormant). The point
is that our patches are upstream-shaped if/when they wake up.

## Tree layout reminder

This repo's backend/router implementation lives in `rust/` (the
`conexus-*` crates, Rust — what the NixOS deployment runs; the
original Python implementation was deleted wholesale once the Rust
rewrite reached functional completeness). The dashboard lives in
`agent_mcp/dashboard/` (Next.js/TypeScript, unrelated to that
migration — it predates it and was never Python). PRs target
whichever of the two the change actually touches; CI gates both
independently (see "CI must pass" above), plus a Nix/home-manager
regression suite covering neither.

## Out of scope for this fork

- Per-user dashboard authentication (the dashboard is admin-by-design
  here; securing the URL is the deployer's job).
- Migration to Postgres+pgvector (deliberate — SQLite per project
  matches the deployment's per-project isolation model).
- Single-process multi-tenant rewrite (the systemd-per-project +
  router model stays — blast radius is the reason).

See [ADRs] in the deployment repo for the trade-offs.

[ADRs]: https://cms.best.aau.dk/dennis/nixos-developer-system/src/branch/main/users/dennis/agent-mcp/docs/adr
