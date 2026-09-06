# agent-mcp home-manager module

[Agent-MCP](https://github.com/rinadelph/Agent-MCP), packaged via
the [`dvaerum/Agent-MCP`](https://github.com/dvaerum/Agent-MCP)
fork, exposed as a home-manager module. Provides multi-agent
coordination tools (spawn sub-agents, assign tasks, shared messages,
per-project RAG over markdown) to claude-code sessions on the same
host or anywhere on a tailnet.

## What this module ships

Three groups of user-scope systemd units:

- **`agent-mcp-router.service`** — always-on URL-keyed HTTP router on
  loopback (default `127.0.0.1:1337`). Serves the Next.js dashboard
  at `/agent-mcp/__dashboard/`, proxies MCP traffic to per-project
  backends, lazy-starts/stops them by activity, and exposes the
  add/remove/rename REST endpoints.
- **`agent-mcp@<name>.service`** (systemd template) — one instance
  per registered project, started lazily by the router on first MCP
  request, stopped after `services.agent-mcp.router.idleSec` seconds
  of inactivity. Listens on a Unix domain socket under
  `$XDG_RUNTIME_DIR/agent-mcp/<name>/backend.sock`.
- **`agent-mcp-daemon-agent@<project>--<agent_id>.service`** (systemd
  template) — one instance per entry in
  `services.agent-mcp.daemonAgents`. Runs an event-driven
  `wait_for_events` long-poll loop so the agent reacts to messages /
  task assignments without anyone keeping a Claude session open.

Project membership is **not** declared in nix. Every project is
registered at runtime via `POST /agent-mcp/__create` (dashboard form
or `curl`), recorded in `~/.config/agent-mcp/projects.local.json`.
The module materialises the router + systemd templates + the
daemon-agent wiring; the project list lives outside source control.

## Quick start

In your home-manager flake:

```nix
{
  inputs.agent-mcp.url = "github:dvaerum/Agent-MCP";

  outputs = { self, nixpkgs, home-manager, agent-mcp, ... }: {
    homeConfigurations."alice" = home-manager.lib.homeManagerConfiguration {
      pkgs = import nixpkgs { system = "x86_64-linux"; };
      modules = [
        agent-mcp.homeModules.default
        {
          home.username = "alice";
          home.homeDirectory = "/home/alice";
          home.stateVersion = "25.11";

          services.agent-mcp = {
            enable = true;
            router = {
              # Default; uncomment to change.
              # port = 1337;
              # idleSec = 14400;  # 4 hours

              # Used in dashboard wiring snippets so .mcp.json files
              # work from other devices on the tailnet, not only the
              # host's loopback.
              externalUrl = "https://my-host.tailfdae0.ts.net";

              # Where /agent-mcp/__create puts a project's workspace
              # when the form's Workspace field is empty.
              defaultWorkspaceParent =
                "/home/alice/.local/share/agent-mcp/projects";
            };
            dashboard.enable = true;
            daemonAgents = [
              # Reference instance — keeps the wiring exercised on
              # every redeploy. Remove or replace with your own.
              {
                project = "washing-brothers";
                agentId = "backend-dev";
                tokenPath =
                  "/home/alice/.config/agent-mcp/tokens/washing-brothers--backend-dev.token";
              }
            ];
          };
        }
      ];
    };
  };
}
```

After `home-manager switch`, the router boots on
`http://127.0.0.1:1337/agent-mcp/`. The first project you create
through the dashboard (or via
`curl -F name=foo http://127.0.0.1:1337/agent-mcp/__create`) appears
in `~/.config/agent-mcp/projects.local.json` and shows up in the
dashboard's overview.

## Options reference

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `services.agent-mcp.enable` | bool | `false` | Enable the router, template, and daemon-agent units. |
| `services.agent-mcp.source` | path | `self` (the flake) | Source tree the module builds from. Override to pin a local checkout. |
| `services.agent-mcp.pkgs` | package set | the consumer's `pkgs` | Package set every agent-mcp derivation is built from. See [Building from a different nixpkgs](#building-from-a-different-nixpkgs). |
| `services.agent-mcp.router.port` | port | `1337` | Router's loopback port. |
| `services.agent-mcp.router.idleSec` | int (positive) | `14400` | Seconds of inactivity before the router stops a per-project backend. |
| `services.agent-mcp.router.externalUrl` | str | (required) | Base URL the host can be reached at; used in `.mcp.json` snippets. |
| `services.agent-mcp.router.defaultWorkspaceParent` | str | (required) | Where new project workspaces are created when the form's Workspace field is empty. |
| `services.agent-mcp.dashboard.enable` | bool | `true` | Build and serve the Next.js dashboard. |
| `services.agent-mcp.dashboard.package` | package | computed | Override the dashboard derivation. |
| `services.agent-mcp.daemonAgents` | list of submodules | `[]` | Each entry expands to one `agent-mcp-daemon-agent@<project>--<agent_id>.service` unit. |

Each `daemonAgents` entry has:

| Sub-option | Type | Description |
|------------|------|-------------|
| `project` | str | Project slug (must exist via `/__create`). |
| `agentId` | str | Agent slug (must exist on the project). |
| `tokenPath` | str | Absolute path to the file holding the agent's bearer token. |

## Building from a different nixpkgs

The module builds agent-mcp from the **consumer's** package set — the
`pkgs` your home-manager configuration was evaluated with. agent-mcp's
own flake pin has no say in it, so adding or dropping
`inputs.agent-mcp.inputs.nixpkgs.follows` in your flake changes only
what `nix build` inside *this* repo produces, never your deployed
closure.

That matters when the host tracks a NixOS **stable** branch. Stable
branches don't take routine Python security backports, so the router's
aiohttp stays on whatever that branch froze on for the life of the
release, advisories included, while unstable already ships the fix.
`services.agent-mcp.pkgs` rebuilds agent-mcp — and only agent-mcp —
from a set you choose, leaving the rest of the profile on stable:

```nix
{ agent-mcp, pkgs, ... }:

{
  imports = [ agent-mcp.homeModules.default ];

  services.agent-mcp.pkgs = import agent-mcp.inputs.nixpkgs {
    inherit (pkgs.stdenv.hostPlatform) system;
  };
}
```

The Python tree, the interpreter the units exec, the wrappers and the
dashboard all come from that one set — mixing them is not offered,
because a wrapper that execs one channel's interpreter against another
channel's site-packages does not run. That is also why there is no
single-derivation override; `services.agent-mcp.package` was removed
(setting it now fails evaluation with a pointer here). The full
rationale is in the `pkgs` option's description in
[`home-manager-module.nix`](./home-manager-module.nix).

## Daemon-agent tokens

Each declared daemon-agent reads its bearer from `tokenPath`. The
file is operator-provisioned — for first-day setup, a chmod-0600
plaintext file is fine:

```sh
install -m 600 /dev/stdin \
  ~/.config/agent-mcp/tokens/washing-brothers--backend-dev.token \
  <<<'<bearer-token-from-dashboard>'
```

For production hosts, wire `tokenPath` through
[`sops-nix`](https://github.com/Mic92/sops-nix) so the token lands
in the right place at activation time. The module does not enforce
any particular provisioning mechanism; the runtime cares only that
the path resolves to a readable file containing the token.

## URL surface

The router exposes:

| URL | Purpose |
|-----|---------|
| `GET /agent-mcp/` | HTML index (Phase 3.5 redirects this to the dashboard overview). |
| `GET /agent-mcp/__dashboard/` | Next.js dashboard. |
| `GET /agent-mcp/__projects` | JSON list of registered projects. |
| `POST /agent-mcp/__create` | Register a new project (form field `name`, optional `workspace`). |
| `POST /agent-mcp/__create-agent` | Create a worker agent on a project. |
| `POST /agent-mcp/__rename` | Rename a project (creates a grace-period alias; ADR-0010). |
| `POST /agent-mcp/__unregister` | Drop a project. |
| `POST /agent-mcp/__stop` | Stop a project's backend (refuses if busy). |
| `ANY /agent-mcp/<name>/mcp` | Streamable HTTP MCP endpoint for `<name>`. |
| `GET /agent-mcp/__client-installer/<n>.sh?agent=<id>` | Curl-installable `.mcp.json` merger. |
| `GET /agent-mcp/__client-config/<n>.mcp.json?agent=<id>` | Raw `.mcp.json` snippet. |

Tailnet exposure (`externalUrl`) is configured outside this module
— e.g. via `services.tailscale.serve` at NixOS scope. See the
[deployment example](https://github.com/dvaerum/nixos-developer-system).

## Asset prefix

The dashboard's static export embeds a literal sentinel string
(`__AGENT_MCP_ASSET_PREFIX__`) wherever Next.js would normally bake
in `assetPrefix`. The router substitutes the configured runtime
prefix into served HTML / JS / CSS bodies on the fly so a single
build artifact serves any deployment URL.

* **Default** prefix: `/agent-mcp/__dashboard`. Operators who deploy
  the router straight onto loopback (the documented path) need no
  configuration — the default matches the router's own route table.
* **Custom mount**: deploying the dashboard behind a reverse proxy
  mounted at a different prefix (e.g. `/tools/`) is a one-line
  change. Set `AGENT_MCP_ASSET_PREFIX=/tools` in the router unit's
  `environment`, or pass `--asset-prefix /tools` on the
  `agent-mcp-router` command line. No rebuild required.

Substitution is Content-Type-gated: only `text/html`,
`text/css`, and `application/javascript` responses are eligible.
JSON API responses, fonts, images, and other binary assets pass
through verbatim, so substitution can never corrupt their bytes
even if a chance sequence happens to match the sentinel.

**Important — single-tenant requires a router-fronted serve.** Per
decision #1 of the [prancy-napping-pie
plan](https://github.com/dvaerum/Agent-MCP/blob/main/.claude/plans/prancy-napping-pie.md)
and [ADR-0008](../docs/adr/0008-single-tenant-url-parity.md), the
router runs in both single-tenant and multi-tenant modes. Substitution
happens at the router, so deploying the dashboard without the router
(e.g. serving the static export directly from nginx) would leak the
sentinel into served bytes and render the dashboard blank. Don't.

## Multi-tenant only (for now)

Phase 2 ships the multi-tenant deployment only — the router always
runs with project routing enabled. The `services.agent-mcp.multiTenant`
toggle (decision #1, [ADR-0008](../docs/adr/0008-single-tenant-url-parity.md))
lands in Phase 3, alongside `pkgs.nixosTest` VM tests for both modes.

## Wiring claude

Once the router is up, point claude at any registered project's MCP
endpoint. The dashboard's "Wiring help" panel shows three ready-to-paste
recipes per project:

1. **One-line installer** (`curl … | bash`) — merges `agent-mcp`
   into the current directory's `.mcp.json`, creating one if missing.
   Idempotent.
2. **Raw `.mcp.json` snippet** — paste into an existing
   `.mcp.json`'s `mcpServers` block.
3. **`claude mcp add`** invocation — writes to `~/.claude.json`
   (user scope, not project scope).

All three use the Streamable HTTP transport (`type: http`). The
legacy SSE pair (`type: sse`) was retired in `dvaerum/Agent-MCP`
3.0.0.

## Architectural background

| Decision | Doc |
|----------|-----|
| Single-tenant runs the router too (URL parity) | [ADR-0008](../docs/adr/0008-single-tenant-url-parity.md) |
| Dashboard owns the ops surface (no `/__admin/`) | [ADR-0009](../docs/adr/0009-dashboard-owns-ops-surface.md) |
| Project rename uses alias-with-grace, agent warning via `serverInfo.instructions` | [ADR-0010](../docs/adr/0010-rename-alias-with-grace.md) |

For the operator's day-to-day workflow (creating projects, wiring
clients, running daemon agents), see the in-dashboard help panel
once the router is running.
