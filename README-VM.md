# Agent-MCP — NixOS VM for end-to-end testing

This directory ships a Nix flake that boots a self-contained NixOS
VM running the full agent-mcp deployment (router, per-project
backends, dashboard, local Ollama embeddings). The VM is built to be
the smallest reproducible mirror of the production deployment that
still lets you point a real client at `http://localhost:5454` and
get a working `.mcp.json` back.

## Prerequisites

- Linux host (x86_64). macOS qemu is out of scope.
- Nix with flakes enabled (`experimental-features = nix-command flakes`
  in `~/.config/nix/nix.conf`).
- KVM-capable kernel (the VM falls back to TCG, ~5x slower boot,
  but works).
- ~3 GB free disk for the closure + first-boot Ollama download.

QEMU itself is brought in by the flake.

## Quick start

```sh
# Multi-tenant (default): router + auto-created project "e2e".
nix run github:dvaerum/Agent-MCP

# Once the boot output stops scrolling:
curl -fsS http://localhost:5454/agent-mcp/__projects
# → {"projects": ["e2e"]}
xdg-open http://localhost:5454/agent-mcp/__dashboard/e2e/
```

State persists to `./vm-persistent-data/` in the directory you ran
the command from; nothing leaks into `~`. Ctrl-C cleanly shuts the
VM down.

## Flags

```
nix run github:dvaerum/Agent-MCP -- [flags]
```

| Flag                  | Meaning                                                                                                       |
|-----------------------|---------------------------------------------------------------------------------------------------------------|
| (default)             | Multi-tenant router on guest:1337, auto-create project `e2e`, persistent storage at `./vm-persistent-data/`. |
| `--minimal`           | Single-tenant agent-mcp backend on guest:8080. No router, no `/agent-mcp/` path prefix.                       |
| `--ephemeral`         | Use a tmpdir for state; everything dies with the VM. Mutually exclusive with `--persist`.                     |
| `--persist DIR`       | Persistent state directory on the host (default `./vm-persistent-data/`).                                     |
| `--project NAME`      | Rename the auto-created project. Multi-tenant only; default `e2e`.                                            |
| `--no-auto-project`   | Skip auto-create; POST `/agent-mcp/__create` yourself once the router is up. Multi-tenant only.               |
| `--help`, `-h`        | Print usage and exit.                                                                                         |

The host always reaches the VM on `http://localhost:5454`. The
wrapper translates that to guest port 1337 (multi) or 8080
(single) via qemu user-mode hostfwd, bound to 127.0.0.1.

## Modes

### Multi-tenant (default)

Mirrors the production `nixos-developer-system` deployment:

- `agent-mcp-router.service` — always-on aiohttp proxy on `:1337`
  that fronts per-project backends and serves the static Next.js
  dashboard under `/agent-mcp/__dashboard/<name>/`.
- `agent-mcp@<name>.service` — systemd template; one instance per
  registered project, listening on a UDS at
  `/run/agent-mcp/<name>/backend.sock`. Lazy-started by the router
  on first request, idle-reaped after 4 h.
- `agent-mcp-bootstrap.service` — one-shot that POSTs
  `/agent-mcp/__create -F name=<auto-project>` on first boot.
  Idempotent (marker file in the state dir).

URL convention (router-internal segments start with `__`):

```
http://localhost:5454/agent-mcp/                       # index
http://localhost:5454/agent-mcp/__projects             # JSON list
http://localhost:5454/agent-mcp/__create               # POST name=<n>
http://localhost:5454/agent-mcp/__sse/<n>              # MCP SSE
http://localhost:5454/agent-mcp/__messages/<n>/...     # MCP messages
http://localhost:5454/agent-mcp/__api/<n>/...          # REST API
http://localhost:5454/agent-mcp/__dashboard/<n>/       # dashboard
```

### Single-tenant (`--minimal`)

One backend on guest TCP `:8080` with no router and no path prefix:

```
http://localhost:5454/sse                              # MCP SSE
http://localhost:5454/messages/<id>                    # MCP messages
http://localhost:5454/api/tokens                       # admin_token
```

This is the lowest-overhead path to smoke-test the agent-mcp HTTP
API itself.

## State layout

The persist directory holds a single qcow2 disk image — that
image is the VM's writable filesystem, and `/var/lib/agent-mcp/`
inside the VM is what agent-mcp persists to:

```
./vm-persistent-data/
└── disk.qcow2                       # 8 GB sparse, qcow2

# Inside the VM:
/var/lib/agent-mcp/
├── projects.local.json              # {<name>: <path>} registry
├── projects/<name>/                 # workspace (SQLite DB in .agent/)
└── .bootstrap-<name>                # marker
```

We deliberately avoid qemu's 9p host-share for state because
SQLite's WAL mode needs real `fcntl` locks, which 9p can't fake.
Putting state on the qcow2 disk dodges that entirely.

Delete the dir (or just the `disk.qcow2`) to nuke state. With
`--ephemeral` the wrapper mktemp's a temporary dir and removes it
on exit.

## Running the e2e tests against the VM

The flake doesn't ship any test runner — point your existing
test suite at the VM:

```sh
AGENT_MCP_BASE=http://localhost:5454 uv run pytest tests/e2e/
```

Multi-tenant tests should target `http://localhost:5454/agent-mcp/`
endpoints. Single-tenant tests want `--minimal` and target the
backend directly.

## Build artefacts

The flake exposes:

```
nix build github:dvaerum/Agent-MCP#agent-mcp            # python package
nix build github:dvaerum/Agent-MCP#agent-mcp-dashboard  # static export
nix build github:dvaerum/Agent-MCP#vm-multi             # multi-tenant VM
nix build github:dvaerum/Agent-MCP#vm-single            # single-tenant VM
nix build github:dvaerum/Agent-MCP#default              # wrapper script
```

`nix flake check` builds the python package + dashboard (CI-cheap)
but skips the VM (CI-expensive). The repo's GitHub Actions
workflow remains Python + dashboard only; the flake is opt-in.

## Reusing the NixOS module standalone

```nix
{
  inputs.agent-mcp.url = "github:dvaerum/Agent-MCP";
  outputs = { self, nixpkgs, agent-mcp, ... }: {
    nixosConfigurations.example = nixpkgs.lib.nixosSystem {
      modules = [
        agent-mcp.nixosModules.default
        ({ ... }: {
          services.agent-mcp = {
            enable = true;
            mode = "multi";
            src = agent-mcp;
            externalUrl = "https://agent.example.com";
          };
        })
      ];
    };
  };
}
```

The module covers the systemd shape only — TLS termination
(nginx, tailscale serve, …) is the operator's job.

## Architecture notes

- The vendored `nix/router.py` is `nixos-developer-system`'s
  router with one knob added: `AGENT_MCP_SYSTEMCTL_MODE`
  switches between `systemctl --user` (production, home-manager)
  and plain `systemctl` (VM, where there's no per-user
  systemd instance). Plus `AGENT_MCP_ROUTER_HOST` so the VM can
  bind `0.0.0.0` for qemu hostfwd.
- A polkit rule (in `nix/module.nix`) grants the unprivileged
  `agent-mcp` user permission to start/stop `agent-mcp@*.service`
  units via systemd, so the router doesn't need root.
- Ollama runs in-VM with `qwen3-embedding:0.6b` (1024-dim).
  The model (~620 MB) is downloaded on first boot, not baked
  into the image, and lives at `/var/lib/ollama/` inside the
  guest — which means it's on the qcow2 disk in your persist
  dir. Subsequent runs reuse the downloaded blob; no re-download
  unless you delete `./vm-persistent-data/disk.qcow2` (or use
  `--ephemeral`, which always starts fresh). Verified: first
  boot ~62 s (cold-includes Ollama pull), second boot ~28 s.
