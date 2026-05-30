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

Prerequisites:

- **Python 3.10+** with [uv](https://github.com/astral-sh/uv)
- **Node.js 22.x** (for the dashboard build)
- **Ollama** with a small embedding model (`qwen3-embedding:0.6b`
  recommended; or any OpenAI-compatible embedding endpoint via
  `OPENAI_BASE_URL` / `OPENAI_API_KEY`)

```sh
git clone https://github.com/dvaerum/Agent-MCP.git
cd Agent-MCP

uv venv && uv pip install -e .[dev]

# Dashboard (only needed if you're touching the dashboard)
( cd agent_mcp/dashboard && npm ci )

# Run tests
pytest

# Lint
ruff check .

# Dashboard build (CI does this)
( cd agent_mcp/dashboard && npm run build )
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

Tests live in `tests/`. The default surface is **integration tests**:
spin up agent-mcp as an in-process ASGI app via the httpx test client,
hit it with real MCP-over-SSE / JSON-RPC, assert. Unit tests are fine
for pure-logic bits (regex, schema generation). End-to-end tests
against a real systemd + Ollama deployment live in
[nixos-developer-system/users/dennis/agent-mcp/tests/] and are run
manually as part of release verification — **not** part of CI here.

[nixos-developer-system/users/dennis/agent-mcp/tests/]: https://cms.best.aau.dk/dennis/nixos-developer-system/src/branch/main/users/dennis/agent-mcp/tests

## CI must pass

`.github/workflows/ci.yml` runs `pytest`, `ruff check .`, and
`( cd agent_mcp/dashboard && npm ci && npm run build )` on every
push and PR. Red CI blocks merge.

## PR template

`.github/PULL_REQUEST_TEMPLATE.md` is auto-populated on every new PR.
Fill in every section that applies. "I'll add tests later" is a no.

## Upstreaming a fix to rinadelph/Agent-MCP

When a fix is general (i.e. anyone running upstream could use it,
not just our deployment), open a PR against upstream too:

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

This repo has **two parallel implementations**: `agent_mcp/` (Python,
what the NixOS deployment runs) and `agent-mcp-node/` (TypeScript,
upstream's newer rewrite). PRs in this fork target Python by default.
CI builds + tests Python only. Don't delete the Node tree — keeping
both keeps upstream PRs clean and leaves room to absorb Node work
into Python later.

## Out of scope for this fork

- Per-user dashboard authentication (the dashboard is admin-by-design
  here; securing the URL is the deployer's job).
- Migration to Postgres+pgvector (deliberate — SQLite per project
  matches the deployment's per-project isolation model).
- Single-process multi-tenant rewrite (the systemd-per-project +
  router model stays — blast radius is the reason).

See [ADRs] in the deployment repo for the trade-offs.

[ADRs]: https://cms.best.aau.dk/dennis/nixos-developer-system/src/branch/main/users/dennis/agent-mcp/docs/adr
