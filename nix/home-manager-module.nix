{ config, lib, pkgs, ... }:

# Home-manager module exposing agent-mcp as user-scope systemd units.
#
# This module ships in the dvaerum/Agent-MCP fork (Phase 2 of the
# router-upstream plan, prancy-napping-pie). It mirrors the
# multi-tenant deployment that previously lived in
# nixos-developer-system/users/dennis/agent-mcp/default.nix verbatim,
# so the byte-shape of the resulting systemd unit files is identical
# up to store-path hashes.
#
# Shape:
#
#   - One declared list `daemonAgents` → N systemd template instances
#     (agent-mcp-daemon-agent@<project>--<agent_id>.service), each
#     running an event-driven wait_for_events loop against the router.
#   - A thin always-on router (agent-mcp-router.service) on
#     127.0.0.1:<port> (default 1337) does URL-path routing + activity
#     tracking, calls systemctl --user start/stop for lazy spawn +
#     idle shutdown, and serves the Next.js static dashboard at
#     /agent-mcp/__dashboard/<name>/.
#   - Per-project backends (agent-mcp@<name>.service template) are
#     lazy-started by the router on first MCP request and idle-stopped
#     after services.agent-mcp.router.idleSec seconds.
#
# Project membership is *not* declared in nix. Every project is
# registered at runtime via POST /agent-mcp/__create (dashboard form
# or `curl`), recorded in ~/.config/agent-mcp/projects.local.json.
# The module materialises the router + systemd template + the
# daemon-agent wiring; project list lives outside source control.
#
# Phase 2 ships multi-tenant only — the router unconditionally runs
# with project routing enabled. The `services.agent-mcp.multiTenant`
# toggle (decision #1, ADR-0008) lands in Phase 3.

let
  cfg = config.services.agent-mcp;

  pkgs' = import ./packages.nix {
    inherit pkgs lib;
    # cfg.source defaults to the fork's repo root via the flake's
    # `homeManagerModules.default` wrapper. Operators can override
    # to pin a different source tree (e.g. for local development).
    src = cfg.source;
  };

  # When the operator overrides services.agent-mcp.package, we use
  # their derivation for the Python tree but still derive the
  # ancillary wrappers from pkgs' (they depend on the Python tree's
  # site-packages path). This branch is the override path; the
  # default path uses pkgs'.agentMcpPy throughout.
  resolvedPkgs =
    if cfg.package == null then pkgs'
    else pkgs' // { agentMcpPy = cfg.package; };

  daemonAgentInstanceName = a: "${a.project}--${a.agentId}";

  daemonAgentWrapper =
    resolvedPkgs.agentMcpDaemonAgentWrapper cfg.router.port;

in {
  options.services.agent-mcp = {
    enable = lib.mkEnableOption "agent-mcp (multi-tenant router + daemon agents)";

    source = lib.mkOption {
      type = lib.types.path;
      description = ''
        Path to the agent-mcp source tree. Defaults to the fork's
        repo root via the flake's `homeManagerModules.default`
        wrapper; override to pin a different source tree.
      '';
    };

    package = lib.mkOption {
      type = lib.types.nullOr lib.types.package;
      default = null;
      description = ''
        Override for the agent-mcp Python derivation. When null
        (default) the module builds it from `source`. Override to
        pin a pre-built derivation, e.g. from a flake input.
      '';
    };

    router = {
      port = lib.mkOption {
        type = lib.types.port;
        default = 1337;
        description = "TCP port the router listens on (loopback only).";
      };

      idleSec = lib.mkOption {
        type = lib.types.ints.positive;
        default = 14400;
        description = ''
          Seconds of inactivity before the router stops a per-project
          backend. Default is 4h, matching the dashboard's "sleeping"
          status bucket.
        '';
      };

      externalUrl = lib.mkOption {
        type = lib.types.str;
        example = "https://nixos-developer-system.tailfdae0.ts.net";
        description = ''
          Base URL the host can reach the router at. Used in
          `.mcp.json` snippets the dashboard hands out and in the
          wiring-help panel URLs so the same file works from other
          devices (laptop, phone), not only this host's loopback.

          Required when `services.agent-mcp.enable = true`.
        '';
      };

      defaultWorkspaceParent = lib.mkOption {
        type = lib.types.str;
        example = "/home/alice/.local/share/agent-mcp/projects";
        description = ''
          Where /agent-mcp/__create puts a project's workspace when
          the user leaves the "Workspace" form field blank. Each
          project then lives at `''${defaultWorkspaceParent}/<name>/`.

          The user can override per-project at create time by typing
          a path into the form's Workspace field; this default only
          kicks in when that field is empty.
        '';
      };
    };

    dashboard = {
      enable = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = ''
          Whether to build and serve the Next.js dashboard. Disable
          to save build time on headless hosts; the MCP transport
          still works without the dashboard.
        '';
      };

      package = lib.mkOption {
        type = lib.types.package;
        default = resolvedPkgs.agentMcpDashboard;
        defaultText = lib.literalExpression "pkgs.agent-mcp-dashboard";
        description = "Dashboard derivation (Next.js static export).";
      };
    };

    daemonAgents = lib.mkOption {
      type = lib.types.listOf (lib.types.submodule {
        options = {
          project = lib.mkOption {
            type = lib.types.str;
            example = "washing-brothers";
            description = ''
              Project slug. Must match an existing project registered
              via POST /agent-mcp/__create.
            '';
          };
          agentId = lib.mkOption {
            type = lib.types.str;
            example = "backend-dev";
            description = ''
              Agent slug. Must match an existing agent on the project.
            '';
          };
          tokenPath = lib.mkOption {
            type = lib.types.str;
            example = "/home/alice/.config/agent-mcp/tokens/washing-brothers--backend-dev.token";
            description = ''
              Absolute path to the file containing the agent's bearer
              token. The file is operator-provisioned; for production
              hosts wire it through sops (see
              docs/EVENT_DRIVEN_AGENT_LOOP.md). For first-day setup a
              chmod-0600 plaintext file is fine.
            '';
          };
        };
      });
      default = [ ];
      description = ''
        Daemon-agent instances to enable. Each entry expands to one
        `agent-mcp-daemon-agent@<project>--<agent_id>.service` unit,
        WantedBy `default.target` so it's symlinked from
        `default.target.wants/`.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    home.packages = [
      resolvedPkgs.agentMcpBackendWrapper        # invoked by the systemd template launcher
      resolvedPkgs.agentMcpRouterWrapper         # invoked by agent-mcp-router.service
      resolvedPkgs.agentMcpLauncher              # invoked by agent-mcp@.service via %i
      daemonAgentWrapper                          # invoked by agent-mcp-daemon-agent@.service via %i
      resolvedPkgs.agentMcpDaemonAgentPrecompactHook  # operator-installed PreCompact hook
    ];

    # ── Systemd services ───────────────────────────────────────────
    # All three groups live under one attrset so the daemon-agent
    # per-instance expansion (lib.listToAttrs over cfg.daemonAgents)
    # can be merged in via `//` without tripping the module-system
    # "attribute path already defined" error that hits when two
    # separate `systemd.user.services = …` assignments coexist at
    # the file scope.
    #
    # Three groups:
    #
    #   "agent-mcp@"             — per-project backend template
    #                              (started lazily by the router).
    #   "agent-mcp-router"       — always-on router (URL-keyed,
    #                              idle-stop).
    #   "agent-mcp-daemon-agent@<instance>"
    #                            — per-instance daemon-agent runner,
    #                              one entry per cfg.daemonAgents
    #                              element, each WantedBy default.target
    #                              so home-manager actually symlinks
    #                              it from default.target.wants/.
    systemd.user.services = {
      "agent-mcp@" = {
        Unit = {
          Description = "Agent-MCP backend — project %i (UDS)";
          # The backend talks to ollama for embeddings; ordering is
          # advisory (ollama is a system service so user-scope can't
          # bind it as a hard dep, but After= still helps at boot).
          After = [ "ollama.service" ];
        };
        Service = {
          Type = "simple";
          # systemd creates and owns %t/agent-mcp/%i
          # (= $XDG_RUNTIME_DIR/agent-mcp/<name>/) — tmpfs, 0700.
          RuntimeDirectory = "agent-mcp/%i";
          RuntimeDirectoryMode = "0700";
          # Defensive: kill any stale socket file before launching so the
          # backend's bind() can succeed cleanly.
          ExecStartPre = "${pkgs.coreutils}/bin/rm -f %t/agent-mcp/%i/backend.sock";
          ExecStart = "${resolvedPkgs.agentMcpLauncher}/bin/agent-mcp-launcher %i";
          Restart = "on-failure";
          RestartSec = 5;
          TimeoutStopSec = 10;
        };
        # Not WantedBy any target — instances are started on demand by
        # the router (`systemctl --user start agent-mcp@<name>`).
      };

      "agent-mcp-router" = {
        Unit = {
          Description = "Agent-MCP router (URL-keyed, idle-stop)";
          After = [ "ollama.service" ];
        };
        Service = {
          Type = "simple";
          Environment = [
            "AGENT_MCP_PROJECTS_FILE=%h/.config/agent-mcp/projects.local.json"
            "AGENT_MCP_SOCK_DIR=%t/agent-mcp"
            "AGENT_MCP_DASHBOARD_DIR=${cfg.dashboard.package}/share/agent-mcp-dashboard"
            "AGENT_MCP_EXTERNAL_URL=${cfg.router.externalUrl}"
            "AGENT_MCP_DEFAULT_WORKSPACE=${cfg.router.defaultWorkspaceParent}"
            "AGENT_MCP_ROUTER_PORT=${toString cfg.router.port}"
            "AGENT_MCP_IDLE_SEC=${toString cfg.router.idleSec}"
            "AGENT_MCP_README_HTML=${resolvedPkgs.readmeHtml}"
            "AGENT_MCP_INSTALLER_TEMPLATE=${resolvedPkgs.installerTemplate}"
          ];
          # systemd creates and owns %t/agent-mcp/ at 0700 so the per-
          # project subdirs the template creates inherit a private
          # parent. (The template's own RuntimeDirectory creates each
          # %t/agent-mcp/<name>/; this just ensures the parent exists.)
          RuntimeDirectory = "agent-mcp";
          RuntimeDirectoryMode = "0700";
          ExecStart = "${resolvedPkgs.agentMcpRouterWrapper}/bin/agent-mcp-router";
          Restart = "on-failure";
          RestartSec = 10;
        };
        Install.WantedBy = [ "default.target" ];
      };
    } // lib.listToAttrs (map (a: {
      name = "agent-mcp-daemon-agent@${daemonAgentInstanceName a}";
      value = {
        Unit = {
          Description = "Agent-MCP daemon agent — ${daemonAgentInstanceName a} (event-driven wait_for_events loop)";
          After = [ "agent-mcp-router.service" ];
          Wants = [ "agent-mcp-router.service" ];
          # StartLimit* live in [Unit] (per `man systemd.unit`), not
          # in [Service] — putting them in Service makes systemd log
          # "Unknown key ... ignoring" and the rate-limiter never
          # kicks in. A 5-in-60s cap forces operator attention when
          # the token's missing rather than restart-storming forever.
          StartLimitBurst = 5;
          StartLimitIntervalSec = 60;
        };
        Service = {
          Type = "simple";
          ExecStart = "${daemonAgentWrapper}/bin/agent-mcp-daemon-agent ${daemonAgentInstanceName a}";
          Restart = "on-failure";
          RestartSec = 10;
        };
        Install.WantedBy = [ "default.target" ];
      };
    }) cfg.daemonAgents);
  };
}
