# vm-dev preload fixtures

Captured **test-data bundles** for the `vm-dev` sandbox. Each
`<name>.tar.zst` here is a raw tar of the agent-mcp state-dir subtree
(`/var/lib/agent-mcp` — `router.db`, `projects.local.json`, and every
`projects/<slug>/.agent/mcp_state.db`) taken from a seeded, quiesced VM.

Restoring one lets `vm-dev` boot with real projects / agents / tasks
already present, instead of the empty first-boot state — no re-walking
the create-project → register-agent → add-tasks flow every run.

## Use

```sh
AGENT_MCP_VM_DEV_PRELOAD=<name> nix run .#vm-dev
# stack several (later files win per-path):
AGENT_MCP_VM_DEV_PRELOAD=default,extra nix run .#vm-dev
```

Selection travels to the guest on the kernel command line (no rebuild).
The restore runs **only on a fresh disk** (a marker at
`/var/lib/agent-mcp/.vm-dev-preloaded` blocks re-runs, so a persistent
VM keeps whatever you changed). To re-preload: `--ephemeral`, or delete
`vm-dev-persistent-data/disk.qcow2`.

## Capture / re-capture

```sh
nix run .#capture-vm-dev-fixture -- <name>
```

Boots an ephemeral `vm-dev`, seeds a dataset through the **real REST
API** (the same path a user takes), quiesces the DBs, and writes
`nix/vm-dev/fixtures/<name>.tar.zst`. Commit the result.

## Why raw `.db`, not a `.sql` dump

RAG uses `sqlite-vec`, whose `vec0` **virtual tables** don't round-trip
through `sqlite3 .dump`. A raw file copy preserves them exactly. If a
fixture predates the current schema, the app's Alembic upgrade runs
forward on first open — so a slightly-stale bundle still boots. **Re-capture
after a migration that changes seeded tables** to keep fixtures current.

## Scope

Dev/test only. These bundles are wired exclusively from `nix/vm-dev.nix`
and never touch the shared home-manager module or any production path.
They may contain throwaway operator credentials (the `dev`/`dev`
sandbox operator) — never real secrets.
