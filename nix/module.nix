{ config, lib, pkgs, ... }:

# NixOS module exposing the agent-mcp deployment as system services.
# Two shapes, selected at evaluation time:
#
#   services.agent-mcp.mode = "multi"
#     → router on :1337 + agent-mcp@<name>.service template
#       (mirrors the production nixos-developer-system deployment).
#
#   services.agent-mcp.mode = "single"
#     → one backend on TCP :8080, no router. Smallest path to
#       smoke-testing the agent-mcp HTTP API.
#
# The flake passes `src` (the repo root) through `_module.args.src`
# so we can use the same code path from both the VM and any other
# NixOS host that wants to import this module.

let
  cfg = config.services.agent-mcp;
  pkgs' = import ./packages.nix {
    inherit pkgs lib;
    src = cfg.src;
  };
in {
  options.services.agent-mcp = {
    enable = lib.mkEnableOption "agent-mcp (multi-tenant router or single-tenant backend)";

    mode = lib.mkOption {
      type = lib.types.enum [ "multi" "single" ];
      default = "multi";
      description = ''
        "multi": router + per-project agent-mcp@<name>.service templates.
        "single": one always-on agent-mcp backend on a TCP port, no router.
      '';
    };

    src = lib.mkOption {
      type = lib.types.path;
      description = ''
        Path to the agent-mcp source tree. The flake wires this to
        the repo root via `_module.args`.
      '';
    };

    user = lib.mkOption {
      type = lib.types.str;
      default = "agent-mcp";
      description = "System user that owns the deployment.";
    };

    group = lib.mkOption {
      type = lib.types.str;
      default = "agent-mcp";
    };

    stateDir = lib.mkOption {
      type = lib.types.str;
      default = "/var/lib/agent-mcp";
      description = "Persistent state root (projects.local.json, workspaces).";
    };

    runtimeDir = lib.mkOption {
      type = lib.types.str;
      default = "/run/agent-mcp";
      description = "Volatile UDS root (cleared on reboot).";
    };

    routerPort = lib.mkOption {
      type = lib.types.port;
      default = 1337;
      description = "TCP port the router listens on (multi mode only).";
    };

    backendPort = lib.mkOption {
      type = lib.types.port;
      default = 8080;
      description = "TCP port the single-tenant backend listens on.";
    };

    externalUrl = lib.mkOption {
      type = lib.types.str;
      default = "http://localhost:5454";
      description = ''
        Base URL the host can reach the VM at. The router renders
        this into copy-pastable .mcp.json snippets, so it has to
        match the qemu hostfwd the wrapper script sets up.
      '';
    };

    autoProject = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = "e2e";
      description = ''
        If non-null and mode == "multi", first-boot bootstrap POSTs
        /agent-mcp/__create with this name. Set to null to skip.
      '';
    };
  };

  config = lib.mkIf cfg.enable (lib.mkMerge [
    {
      users.users.${cfg.user} = {
        isSystemUser = true;
        group = cfg.group;
        home = cfg.stateDir;
        createHome = true;
      };
      users.groups.${cfg.group} = { };

      systemd.tmpfiles.rules = [
        "d ${cfg.stateDir} 0750 ${cfg.user} ${cfg.group} - -"
        "d ${cfg.stateDir}/projects 0750 ${cfg.user} ${cfg.group} - -"
        "d ${cfg.runtimeDir} 0750 ${cfg.user} ${cfg.group} - -"
      ];
    }

    # ── Multi-tenant: router + template ───────────────────────────
    (lib.mkIf (cfg.mode == "multi") {
      systemd.services.agent-mcp-router = {
        description = "Agent-MCP router (URL-keyed, system-mode systemctl)";
        wantedBy = [ "multi-user.target" ];
        after = [ "network.target" "ollama.service" ];
        environment = {
          AGENT_MCP_PROJECTS_FILE = "${cfg.stateDir}/projects.local.json";
          AGENT_MCP_SOCK_DIR = cfg.runtimeDir;
          AGENT_MCP_DASHBOARD_DIR =
            "${pkgs'.agentMcpDashboard}/share/agent-mcp-dashboard";
          AGENT_MCP_EXTERNAL_URL = cfg.externalUrl;
          AGENT_MCP_DEFAULT_WORKSPACE = "${cfg.stateDir}/projects";
          AGENT_MCP_ROUTER_PORT = toString cfg.routerPort;
          # Bind on the wildcard so qemu user-mode hostfwd packets
          # (which arrive on the guest's primary IP, not loopback)
          # can be served.
          AGENT_MCP_ROUTER_HOST = "0.0.0.0";
          AGENT_MCP_IDLE_SEC = "14400";
          AGENT_MCP_README_HTML = "${pkgs'.readmeHtml}";
          AGENT_MCP_INSTALLER_TEMPLATE = "${pkgs'.installerTemplate}";
          # Crucial: VM has no per-user systemd instance — router
          # has to drive the system bus directly.
          AGENT_MCP_SYSTEMCTL_MODE = "system";
        };
        serviceConfig = {
          Type = "simple";
          User = cfg.user;
          Group = cfg.group;
          # Needs to call `systemctl start agent-mcp@…` against the
          # system bus, which requires root or polkit grants. Root
          # is the path of least friction inside a single-purpose VM.
          AmbientCapabilities = [ ];
          ExecStart = "${pkgs'.agentMcpRouterWrapper}/bin/agent-mcp-router";
          Restart = "on-failure";
          RestartSec = 10;
        };
      };

      # Polkit rule so the router's unprivileged user can drive
      # agent-mcp@*.service via systemctl without sudo prompts.
      security.polkit.enable = true;
      security.polkit.extraConfig = ''
        polkit.addRule(function(action, subject) {
          if (action.id == "org.freedesktop.systemd1.manage-units" &&
              subject.user == "${cfg.user}") {
            var unit = action.lookup("unit");
            if (unit && (unit.indexOf("agent-mcp@") == 0)) {
              return polkit.Result.YES;
            }
          }
        });
      '';

      systemd.services."agent-mcp@" = {
        description = "Agent-MCP backend — project %i (UDS)";
        after = [ "network.target" "ollama.service" ];
        # Never auto-started; router lazy-spawns on first request.
        serviceConfig = {
          Type = "simple";
          User = cfg.user;
          Group = cfg.group;
          RuntimeDirectory = "agent-mcp/%i";
          RuntimeDirectoryMode = "0750";
          Environment = [
            "AGENT_MCP_PROJECTS_FILE=${cfg.stateDir}/projects.local.json"
            "AGENT_MCP_SOCK_DIR=${cfg.runtimeDir}"
          ];
          ExecStartPre = "${pkgs.coreutils}/bin/rm -f ${cfg.runtimeDir}/%i/backend.sock";
          ExecStart = "${pkgs'.agentMcpLauncher}/bin/agent-mcp-launcher %i";
          Restart = "on-failure";
          RestartSec = 5;
          TimeoutStopSec = 10;
        };
      };

      # ── First-boot project bootstrap ─────────────────────────────
      systemd.services.agent-mcp-bootstrap = lib.mkIf (cfg.autoProject != null) {
        description = "Create the default agent-mcp project on first boot";
        after = [ "agent-mcp-router.service" ];
        wants = [ "agent-mcp-router.service" ];
        wantedBy = [ "multi-user.target" ];
        path = [ pkgs.curl ];
        serviceConfig = {
          Type = "oneshot";
          RemainAfterExit = true;
          User = cfg.user;
          Group = cfg.group;
          # Marker file in stateDir means "already done"; idempotent
          # across reboots without re-POSTing.
          ExecStart = pkgs.writeShellScript "agent-mcp-bootstrap" ''
            set -eu
            marker="${cfg.stateDir}/.bootstrap-${cfg.autoProject}"
            if [[ -e "$marker" ]]; then
              echo "agent-mcp-bootstrap: marker exists, skipping"
              exit 0
            fi
            # Wait up to 60s for the router to accept connections.
            for i in $(seq 1 60); do
              if ${pkgs.curl}/bin/curl -fsS -o /dev/null \
                  "http://127.0.0.1:${toString cfg.routerPort}/agent-mcp/__projects"; then
                break
              fi
              sleep 1
            done
            ${pkgs.curl}/bin/curl -fsSL -o /dev/null -F name=${cfg.autoProject} \
              "http://127.0.0.1:${toString cfg.routerPort}/agent-mcp/__create" \
              || ${pkgs.curl}/bin/curl -fsSL -o /dev/null -L -F name=${cfg.autoProject} \
                  "http://127.0.0.1:${toString cfg.routerPort}/agent-mcp/__create"
            touch "$marker"
          '';
        };
      };
    })

    # ── Single-tenant: one backend, no router ─────────────────────
    (lib.mkIf (cfg.mode == "single") {
      systemd.services.agent-mcp-backend = {
        description = "Agent-MCP single-tenant backend";
        wantedBy = [ "multi-user.target" ];
        # local-fs.target ensures /persist (9p) is mounted; tmpfiles
        # alone doesn't guarantee that for non-default-fs paths.
        after = [ "network.target" "ollama.service" "local-fs.target" ];
        requires = [ "local-fs.target" ];
        environment = {
          OPENAI_BASE_URL = "http://127.0.0.1:11434/v1";
          OPENAI_API_KEY = "ollama";
          # v5.0.44: completion_service.completion_client() requires
          # OPENAI_MODEL when OPENAI_API_KEY is set. Matches the chat
          # model loaded by services.ollama.loadModels.
          OPENAI_MODEL = "qwen3:1.7b";
          AGENT_MCP_EMBEDDING_MODEL = "qwen3-embedding:0.6b";
          AGENT_MCP_EMBEDDING_DIMENSION = "1024";
        };
        serviceConfig = {
          Type = "simple";
          User = cfg.user;
          Group = cfg.group;
          # No WorkingDirectory — agent-mcp respects --project-dir
          # and we don't want CHDIR failures pre-empting our mkdir.
          # Two ExecStartPres, the `+` prefix runs as root so we
          # can mkdir under /persist regardless of its ownership.
          ExecStartPre = [
            "+${pkgs.coreutils}/bin/mkdir -p ${cfg.stateDir}/single"
            "+${pkgs.coreutils}/bin/chown ${cfg.user}:${cfg.group} ${cfg.stateDir}/single"
          ];
          ExecStart = ''
            ${pkgs'.agentMcpBackendWrapper}/bin/agent-mcp-backend \
              --port ${toString cfg.backendPort} \
              --project-dir ${cfg.stateDir}/single \
              --no-tui
          '';
          Restart = "on-failure";
          RestartSec = 5;
        };
      };
    })
  ]);

}
