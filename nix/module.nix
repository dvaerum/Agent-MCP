{ config, lib, pkgs, ... }:

# NixOS module exposing the agent-mcp deployment as system services.
#
#   services.agent-mcp.enable = true
#     → router (conexus-router) on :1337 + per-project conexus@<name>.service
#       template (mirrors the production nixos-developer-system deployment).
#
# The flake passes `src` (the repo root) through `_module.args.src`
# so we can use the same code path from both the VM and any other
# NixOS host that wants to import this module.
#
# History: this module used to also support `mode = "single"` (one
# always-on Python backend on a bare TCP port, no router) and
# `mode = "multi"` selecting between that and the router shape below.
# Both modes ran the Python implementation (`agent-mcp@<name>.service`
# / `agent-mcp-router.service`), retired together with the Python
# source tree. The router shape survives as the module's only shape,
# rebuilt on the CoNexus Rust binaries (`conexus@<name>.service` /
# `conexus-router.service`) — `conexusLauncherPackage`/
# `conexusRouterPackage` below. The bare-TCP-port single-backend shape
# has NO Rust equivalent: `conexus-backend` only serves over `--uds`
# (see rust/conexus-backend/src/main.rs's own module doc — host:port
# serving was explicitly never ported, since no `conexus@<name>.service`
# caller needs it). Reviving it would mean adding TCP-serving support
# to conexus-backend, which is out of scope for this retirement; until
# that happens, single-backend deploys have no supported path through
# this module.

let
  cfg = config.services.agent-mcp;
  pkgs' = import ./packages.nix {
    inherit pkgs lib;
    src = cfg.src;
  };

  # ── Systemd hardening (defense-in-depth) ──────────────────────────
  # The SAFE sandboxing subset is shared verbatim with the home-manager
  # (user-scope) module via nix/hardening.nix — one source of truth for
  # both deployment shapes. See that file for the per-directive
  # rationale and the deliberately-omitted set.
  #
  # system-mode runs as a dedicated system user (not under $HOME), so it
  # goes stricter than the shared subset: ProtectSystem="strict" +
  # ProtectHome + explicit ReadWritePaths lock the filesystem down to
  # exactly the state + runtime dirs the service writes (all units keep
  # their persistent state under cfg.stateDir and their sockets / HMAC
  # keys under cfg.runtimeDir). Merged into every serviceConfig via
  # `// systemHardening`.
  hardening = import ./hardening.nix;
  systemHardening = hardening // {
    ProtectSystem = "strict";
    ProtectHome = true;
    ReadWritePaths = [ cfg.stateDir cfg.runtimeDir ];
  };
in {
  options.services.agent-mcp = {
    enable = lib.mkEnableOption "agent-mcp (multi-tenant router + per-project backends)";

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
      description = "TCP port the router listens on.";
    };

    routerHost = lib.mkOption {
      type = lib.types.str;
      default = "127.0.0.1";
      description = ''
        Interface the router binds, passed through as
        `--host`/`AGENT_MCP_ROUTER_HOST`-equivalent config. Defaults to
        loopback — matching the application's deliberately-safe
        default — so a bare import of this module keeps the router
        behind a reverse proxy (nginx on loopback handles
        rate-limiting, XFF sanitization, TLS) instead of exposing it on
        every interface. The VM configs (nix/vm.nix) override this to
        "0.0.0.0" because qemu user-mode hostfwd delivers packets to the
        guest's primary IP, not loopback — see the comment there.
      '';
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

    conexusLauncherPackage = lib.mkOption {
      type = lib.types.nullOr lib.types.package;
      default = null;
      description = ''
        The CoNexus Rust backend's systemd-template launcher (`nix/
        conexus.nix`'s `conexusLauncher`), for the `conexus@<name>.service`
        template -- the ONLY per-project backend implementation now
        that the Python one was retired. `null` (the default) omits
        the template entirely — this module has no direct access to
        the `crane` flake input, so whichever caller DOES (the flake's
        own outputs, `nix/vm.nix`) is responsible for building `nix/
        conexus.nix` and setting this option, the same "build
        elsewhere, pass the package in" pattern `src` already uses one
        option up.
      '';
    };

    conexusRouterPackage = lib.mkOption {
      type = lib.types.nullOr lib.types.package;
      default = null;
      description = ''
        The CoNexus Rust router wrapper (`nix/conexus.nix`'s
        `conexusRouterWrapper`), for the singleton `conexus-router`
        system service -- the ONLY router implementation now that the
        Python one (`agent-mcp-router.service`) was retired. `null`
        (the default) omits the service entirely, same "build
        elsewhere, pass the package in" pattern as
        `conexusLauncherPackage` above.
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
  # service environment.

  config = lib.mkIf cfg.enable {
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

    # ── Router (conexus-router) ─────────────────────────────────────
    # `null` by default (see `conexusRouterPackage`'s own option doc)
    # -- the unit is simply omitted until a caller builds nix/conexus.nix
    # and sets the option, exactly like `conexus@` below.
    systemd.services.conexus-router = lib.mkIf (cfg.conexusRouterPackage != null) {
      description = "CoNexus router (URL-keyed, system-mode systemctl)";
      wantedBy = [ "multi-user.target" ];
      after = [ "network.target" "ollama.service" ];
      environment = {
        # `--host`/bind-interface has no CLI flag on `conexus-router`
        # (mirrors the retired Python router's own CLI, which didn't
        # have one either) — env-var only, same as the Python router.
        # Defaults to loopback (cfg.routerHost = "127.0.0.1"), the safe
        # production posture: the router sits behind an nginx-on-loopback
        # reverse proxy that owns rate-limiting, XFF sanitization, and
        # TLS. The VM configs override cfg.routerHost to "0.0.0.0"
        # because qemu user-mode hostfwd delivers packets to the guest's
        # primary IP (not loopback), so the VM must bind the wildcard to
        # be reachable on the host-forwarded port. See nix/vm.nix.
        AGENT_MCP_ROUTER_HOST = cfg.routerHost;
        # `--default-workspace` has no CLI-flag equivalent on
        # `conexus-router` (env-var-only -- see rust/conexus-router/src/
        # main.rs's own `default_workspace_parent()` doc). Without this,
        # `conexus-router` falls back to `$HOME/.local/share/agent-mcp/projects`
        # under the `agent-mcp` system user's HOME (`cfg.stateDir`),
        # NOT `${cfg.stateDir}/projects` where the tmpfiles rule above
        # actually creates the workspace parent.
        AGENT_MCP_DEFAULT_WORKSPACE = "${cfg.stateDir}/projects";
        # Crucial: VM has no per-user systemd instance — router
        # has to drive the system bus directly.
        AGENT_MCP_SYSTEMCTL_MODE = "system";
      };
      serviceConfig = {
        Type = "simple";
        User = cfg.user;
        Group = cfg.group;
        # Needs to call `systemctl start conexus@…` against the
        # system bus, which requires root or polkit grants. Root
        # is the path of least friction inside a single-purpose VM.
        AmbientCapabilities = [ ];
        ExecStart =
          "${cfg.conexusRouterPackage}/bin/conexus-router "
          + "--port ${toString cfg.routerPort} "
          + "--projects-file ${cfg.stateDir}/projects.local.json "
          + "--sock-dir ${cfg.runtimeDir} "
          + "--dashboard-dir ${pkgs'.agentMcpDashboard}/share/agent-mcp-dashboard "
          + "--external-url ${lib.escapeShellArg cfg.externalUrl} "
          + "--idle-sec 14400";
        Restart = "on-failure";
        RestartSec = 10;
      } // systemHardening;
    };

    # Polkit rule so the router's unprivileged user can drive
    # conexus@*.service via systemctl without sudo prompts.
    security.polkit.enable = true;
    security.polkit.extraConfig = ''
      polkit.addRule(function(action, subject) {
        if (action.id == "org.freedesktop.systemd1.manage-units" &&
            subject.user == "${cfg.user}") {
          var unit = action.lookup("unit");
          if (unit && unit.indexOf("conexus@") == 0) {
            return polkit.Result.YES;
          }
        }
      });
    '';

    # `conexus@<name>.service` — the CoNexus Rust backend template,
    # and the ONLY per-project backend implementation now that the
    # Python one (`agent-mcp@<name>.service`) was retired together
    # with the rest of the Python source tree.
    #
    # `null` by default (see `conexusLauncherPackage`'s option doc) —
    # the template is omitted entirely until a caller (the flake, or
    # `nix/vm.nix` for VM tests) actually builds `nix/conexus.nix` and
    # sets the option, since this module has no direct access to the
    # `crane` flake input.
    systemd.services."conexus@" = lib.mkIf (cfg.conexusLauncherPackage != null) {
      description = "CoNexus backend — project %i (UDS)";
      after = [ "network.target" ];
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
        #
        # SC-3: the flip side of preservation is that systemd will NOT
        # wipe ``/run/agent-mcp/%i/`` when a project is deleted (only on
        # reboot, since it's tmpfs). The router's own project-deletion
        # handler owns that cleanup — it rmtree's
        # ``$AGENT_MCP_SOCK_DIR/<name>/`` after stopping the unit so the
        # ``forwarding_hmac`` key doesn't linger for a now-gone project.
        RuntimeDirectoryPreserve = "yes";
        Environment = [
          "AGENT_MCP_PROJECTS_FILE=${cfg.stateDir}/projects.local.json"
          "AGENT_MCP_SOCK_DIR=${cfg.runtimeDir}"
        ];
        # F015 v4: generate the per-project HMAC key in the unit
        # ExecStartPre (not the router) so EVERY path that starts
        # the unit guarantees the file exists. Owning key generation
        # in the unit ExecStartPre makes the file a unit-lifecycle
        # invariant: present whenever the unit is starting, regardless
        # of who triggered the start (manual, on-failure restart, the
        # router's lazy-spawn).
        # F015 v6: coreutils does NOT ship `sh` — the original v4
        # interpolation (``${pkgs.coreutils}/bin/sh``) failed with
        # ``status=203/EXEC`` on every backend start. ``runtimeShell``
        # resolves to the bash/dash/POSIX shell appropriate for the
        # platform. ``head`` and ``chmod`` ARE in coreutils.
        ExecStartPre = [
          "${pkgs.runtimeShell} -c 'test -f \"$RUNTIME_DIRECTORY/forwarding_hmac\" || { ${pkgs.coreutils}/bin/head -c 32 /dev/urandom > \"$RUNTIME_DIRECTORY/forwarding_hmac\" && ${pkgs.coreutils}/bin/chmod 600 \"$RUNTIME_DIRECTORY/forwarding_hmac\"; }'"
          "${pkgs.coreutils}/bin/rm -f ${cfg.runtimeDir}/%i/backend.sock"
        ];
        ExecStart = "${cfg.conexusLauncherPackage}/bin/conexus-launcher %i";
        Restart = "on-failure";
        RestartSec = 5;
        TimeoutStopSec = 10;
      } // systemHardening;
    };

    # First-boot project bootstrap retired — see the autoProject
    # NOTE above. Operators create projects via the dashboard
    # `POST /api/router/projects` (which requires session-cookie
    # auth) after logging in.
  };
}
