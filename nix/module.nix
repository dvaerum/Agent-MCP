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
  };

  # NOTE: A legacy `autoProject` option used to live here, backed by
  # an `agent-mcp-bootstrap.service` oneshot that POSTed
  # `/agent-mcp/__create` on first boot. The `__create` endpoint was
  # deleted in ADR 0014 (see agent_mcp/router/app.py:1410-1415) and
  # the REST replacement at `POST /api/router/projects` requires a
  # session cookie that a oneshot can't have. The bootstrap unit was
  # silently succeeding via curl -L following the empty-users
  # redirect to /setup (HTTP 200), then touching its marker file
  # without creating anything. Retired entirely — operators create
  # projects via the dashboard UI after first login. The first-boot
  # operator can still be auto-seeded by setting
  # `AGENT_MCP_BOOTSTRAP_USERNAME` / `_PASSWORD` on the router
  # service environment (see agent_mcp/router/identity.py).

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
        # F015 v4: lift systemd's default start-rate-limit (5 starts /
        # 10 s) so the unit can keep restarting through a transient
        # failure without the unit getting wedged by
        # ``Failed to schedule restart job: Start request repeated
        # too quickly``. StartLimit* live in [Unit], not [Service]
        # (per ``man systemd.unit``); NixOS exposes them as top-level
        # options on ``systemd.services.<name>``.
        startLimitBurst = 100;
        startLimitIntervalSec = 300;
        serviceConfig = {
          Type = "simple";
          User = cfg.user;
          Group = cfg.group;
          RuntimeDirectory = "agent-mcp/%i";
          RuntimeDirectoryMode = "0750";
          # F015 v3 (defence-in-depth): without this, systemd's default
          # ``RuntimeDirectoryPreserve=no`` wipes ``/run/agent-mcp/%i/``
          # on every ``systemctl stop`` — including the
          # ``forwarding_hmac`` key file the backend's
          # ``--forwarding-hmac-in`` flag points at. Preserving the
          # runtime dir across restarts keeps the file alive between
          # stop/start cycles.
          RuntimeDirectoryPreserve = "yes";
          Environment = [
            "AGENT_MCP_PROJECTS_FILE=${cfg.stateDir}/projects.local.json"
            "AGENT_MCP_SOCK_DIR=${cfg.runtimeDir}"
          ];
          # F015 v4: generate the per-project HMAC key in the unit
          # ExecStartPre (not the router) so EVERY path that starts
          # the unit guarantees the file exists. The router (PRs
          # #208-#213) wrote the key before invoking systemctl, but
          # systemd's ``Restart=on-failure`` loop reactivates the unit
          # autonomously after a crash — bypassing the router entirely.
          # The live VM hit a 9569-deep restart loop because the
          # backend crashed once (key missing for whatever reason),
          # systemd kept restarting it, and the router-side self-heal
          # in ``ensure_forwarding_hmac_key`` never ran on those
          # autonomous restarts. Generating the key here makes the
          # file a unit-lifecycle invariant: present whenever the
          # unit is starting, regardless of who triggered the start.
          # F015 v6: coreutils does NOT ship `sh` — the original v4
          # interpolation (``${pkgs.coreutils}/bin/sh``) failed with
          # ``status=203/EXEC`` on every backend start. ``runtimeShell``
          # resolves to the bash/dash/POSIX shell appropriate for the
          # platform. ``head`` and ``chmod`` ARE in coreutils.
          ExecStartPre = [
            "${pkgs.runtimeShell} -c 'test -f \"$RUNTIME_DIRECTORY/forwarding_hmac\" || { ${pkgs.coreutils}/bin/head -c 32 /dev/urandom > \"$RUNTIME_DIRECTORY/forwarding_hmac\" && ${pkgs.coreutils}/bin/chmod 600 \"$RUNTIME_DIRECTORY/forwarding_hmac\"; }'"
            "${pkgs.coreutils}/bin/rm -f ${cfg.runtimeDir}/%i/backend.sock"
          ];
          ExecStart = "${pkgs'.agentMcpLauncher}/bin/agent-mcp-launcher %i";
          Restart = "on-failure";
          RestartSec = 5;
          TimeoutStopSec = 10;
        };
      };

      # First-boot project bootstrap retired — see the autoProject
      # NOTE above. Operators create projects via the dashboard
      # `POST /api/router/projects` (which requires session-cookie
      # auth) after logging in.
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
        # v5.0.53: core.config now seeds Ollama defaults automatically
        # when OPENAI_API_KEY is unset, so the unit no longer needs to
        # set OPENAI_BASE_URL / OPENAI_API_KEY / OPENAI_MODEL /
        # AGENT_MCP_EMBEDDING_MODEL / AGENT_MCP_EMBEDDING_DIMENSION.
        # Re-add an `environment` block here if you need to point this
        # tenant at a non-default Ollama endpoint or model.
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
