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
# Phase 3 adds the `services.agent-mcp.multiTenant` toggle (default
# true). When set to false, the operator additionally declares
# `services.agent-mcp.singleProject = { name, workspace }`; the
# module seeds a one-entry projects.local.json via ExecStartPre on
# the router unit and passes --single-tenant / --single-workspace
# to the router so its write endpoints 410 and W1 redirects fire
# (decision #1 + #9; ADR-0008).

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

  # ── Shared systemd hardening (defense-in-depth) ───────────────────
  # The SAFE sandboxing subset, factored into nix/hardening.nix so the
  # system-mode module (nix/module.nix) shares the exact same set — see
  # that file for the per-directive rationale and the list of
  # deliberately-omitted directives (MemoryDenyWriteExecute /
  # SystemCallFilter / ProtectSystem=strict) that break CPython + the
  # sqlite-vec native extension or the $HOME-RW these user-scope units
  # need. Merged into every Service block via `// hardening`.
  #
  # User-scope note: these units write the router SQLite DB under
  # XDG_DATA_HOME and read projects.local.json under XDG_CONFIG_HOME, so
  # the shared set stays clear of ProtectHome / ProtectSystem=strict; a
  # home-blocking sandbox would crash-loop the units.
  hardening = import ./hardening.nix;

  # ── Single-tenant ExecStartPre seed ───────────────────────────────
  # When the module is configured for N=1 (`multiTenant = false` +
  # `singleProject = {…}`), we seed ~/.config/agent-mcp/projects.local.json
  # with the single declared entry before the router starts. The
  # router's registry reads this file on every request, so without the
  # seed the launcher couldn't resolve <name> → workspace path and the
  # backend would never start.
  #
  # The seed is idempotent and conservative: if a file already exists
  # AND already contains the declared project under the declared
  # name, we leave it alone (operators may have hand-extended the
  # file, or it may be a successful seed from a previous boot). When
  # in doubt we overwrite — single-tenant mode is the operator's
  # explicit declaration that the file should contain exactly one
  # entry.
  singleProjectSeedScript = lib.mkIf (!cfg.multiTenant) (
    pkgs.writeShellScript "agent-mcp-single-tenant-seed" ''
      set -euo pipefail
      cfg_dir="''${XDG_CONFIG_HOME:-$HOME/.config}/agent-mcp"
      mkdir -p "$cfg_dir"
      file="$cfg_dir/projects.local.json"
      desired='{"${cfg.singleProject.name}":"${cfg.singleProject.workspace}"}'
      if [[ -f "$file" ]] \
        && ${pkgs.jq}/bin/jq -e \
            --arg n "${cfg.singleProject.name}" \
            --arg w "${cfg.singleProject.workspace}" \
            '.[$n] == $w' "$file" >/dev/null 2>&1; then
        exit 0
      fi
      echo "$desired" > "$file.new"
      mv "$file.new" "$file"
    ''
  );

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

    multiTenant = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = ''
        When true (default), the router runs in multi-tenant mode:
        projects are registered at runtime via POST /agent-mcp/__create
        and the dashboard's overview lists them all.

        When false, the router runs in single-tenant mode (N=1):
        `services.agent-mcp.singleProject` declares the only project,
        the module seeds projects.local.json before the router starts,
        and the router 410s __create / __unregister / __rename plus
        302-redirects any wrong-project URL to the configured one
        (ADR-0008, plan decisions #1 + #9).
      '';
    };

    sso = {
      # Phase 3 Wave 3 (prancy-napping-pie): two SSO front-ends, exactly
      # one active at a time. OIDC runs the authorization-code + PKCE
      # flow against an external IdP; proxy-header trust lets an
      # upstream reverse proxy (nginx + oauth2-proxy, traefik +
      # forward-auth, tailscale-funnel + Tailnet identity, …) supply
      # the username via a header. The router refuses to start when
      # both are configured. The dashboard's System → SSO tab renders
      # the live config so a sysadmin can verify the deploy without
      # journalctl access.
      oidc = lib.mkOption {
        type = lib.types.nullOr (lib.types.submodule {
          options = {
            issuer = lib.mkOption {
              type = lib.types.str;
              example = "https://keycloak.example.com/realms/agent-mcp";
              description = ''
                OIDC issuer URL. The router fetches its discovery
                document at `''${issuer}/.well-known/openid-configuration`
                and binds the resulting authorize / token endpoints.
              '';
            };
            clientId = lib.mkOption {
              type = lib.types.str;
              example = "agent-mcp";
              description = "RP client identifier registered with the IdP.";
            };
            clientSecretFile = lib.mkOption {
              type = lib.types.path;
              example = "/run/secrets/agent-mcp-oidc-client-secret";
              description = ''
                Path to a chmod-0600 file holding the OIDC client
                secret. The router reads it once at startup; secret
                rotation is "edit the file, restart the unit". Matches
                the existing sops-nix secret pattern.
              '';
            };
            providerName = lib.mkOption {
              type = lib.types.str;
              default = "SSO";
              example = "Keycloak";
              description = ''
                Display name on the login page's "Sign in with ..."
                button. Purely cosmetic; the router doesn't validate
                this against the issuer.
              '';
            };
            groupMapping = lib.mkOption {
              type = lib.types.attrsOf lib.types.str;
              default = { };
              example = {
                "eng-backend" = "backend-team";
                "*" = "";
              };
              description = ''
                Map IdP-supplied group claims to agent-mcp groups.
                Each entry is `oidc_group_name = agent_mcp_group_name`.
                Unmapped claims are silently ignored.

                Special: an entry with key `"*"` enables the wildcard
                JIT escape — every unmatched group claim auto-creates
                a sanitized agent-mcp group (lowercase, dashes only)
                and the user is added.
              '';
            };
            scopes = lib.mkOption {
              type = lib.types.listOf lib.types.str;
              default = [ "openid" "profile" "email" "groups" ];
              description = ''
                OAuth2 scopes requested at the IdP. Defaults to
                openid + profile + email + groups; trim if the IdP
                rejects unknown scopes.
              '';
            };
            redirectUrl = lib.mkOption {
              type = lib.types.nullOr lib.types.str;
              default = null;
              example = "https://router.example.com/agent-mcp/sso/callback";
              description = ''
                Override the redirect URL handed to the IdP.
                Defaults to `''${externalUrl}/agent-mcp/sso/callback`
                — only set this if the IdP's registered redirect URI
                differs from the natural one.
              '';
            };
          };
        });
        default = null;
        description = ''
          OIDC SSO. When non-null, the dashboard's login page swaps
          the username/password form for a single "Sign in with
          ''${providerName}" button that initiates the OAuth2
          authorization-code + PKCE flow. Mutually exclusive with
          `sso.proxyHeader`.
        '';
      };

      proxyHeader = lib.mkOption {
        type = lib.types.nullOr (lib.types.submodule {
          options = {
            trustHeader = lib.mkOption {
              type = lib.types.str;
              default = "Remote-User";
              description = ''
                HTTP header carrying the trusted username. Common
                values: `Remote-User` (nginx + oauth2-proxy),
                `X-Forwarded-User` (traefik + forward-auth),
                `Tailscale-User-Login` (tailscale-serve).
              '';
            };
            trustedIps = lib.mkOption {
              type = lib.types.listOf lib.types.str;
              default = [ "127.0.0.1" "::1" ];
              example = [ "127.0.0.1" "::1" "10.0.0.5" ];
              description = ''
                Source IPs the router will accept the trusted header
                from. CRITICAL SAFETY RULE: any other source is
                treated as a spoof attempt and the header is silently
                ignored. Default is localhost-only — appropriate for
                deploys where the upstream proxy runs on the same
                host (the common pattern).
              '';
            };
            defaultIsSysadmin = lib.mkOption {
              type = lib.types.bool;
              default = false;
              description = ''
                When a JIT-created user lands via the proxy-header
                path, give them the sysadmin bit. Default false —
                the operator promotes users to sysadmin manually
                via the dashboard. Set true ONLY when the upstream
                proxy is your sole, well-trusted auth boundary.
              '';
            };
          };
        });
        default = null;
        description = ''
          Proxy-header trust SSO. When non-null, the router accepts
          the configured header as a session-equivalent identity
          provided the request originates from one of `trustedIps`.
          Mutually exclusive with `sso.oidc`.
        '';
      };
    };

    singleProject = lib.mkOption {
      type = lib.types.nullOr (lib.types.submodule {
        options = {
          name = lib.mkOption {
            type = lib.types.strMatching "^[a-z]([a-z0-9-]*[a-z0-9])?$";
            example = "washing-brothers";
            description = ''
              Slug for the single-tenant project. Must match the
              router's slug regex (lowercase letters / digits /
              hyphens; no underscores; no leading/trailing hyphen).
              Single-letter names are permitted.
            '';
          };
          workspace = lib.mkOption {
            type = lib.types.str;
            example = "/home/alice/code/washing-brothers";
            description = ''
              Absolute workspace path the backend will run against
              (--project-dir on agent-mcp@<name>.service). The
              directory is NOT auto-created — the operator either
              provisions it ahead of time or hands the module an
              existing repo checkout.
            '';
          };
        };
      });
      default = null;
      description = ''
        Required when `multiTenant = false`; must remain `null` when
        `multiTenant = true`. An assertion enforces the pairing so a
        half-toggled config fails at evaluation, not at runtime.
      '';
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
    # Catch the lopsided configurations at evaluation time, before
    # the user wastes a `home-manager switch` on a broken profile.
    assertions = [
      {
        assertion = cfg.multiTenant -> cfg.singleProject == null;
        message = ''
          services.agent-mcp.singleProject must be null when
          services.agent-mcp.multiTenant = true (multi-tenant mode
          discovers projects at runtime via __create; the
          singleProject option only applies to single-tenant mode).
        '';
      }
      {
        assertion = (!cfg.multiTenant) -> cfg.singleProject != null;
        message = ''
          services.agent-mcp.multiTenant = false requires
          services.agent-mcp.singleProject = { name = "<slug>";
          workspace = "<abs-path>"; }. The router refuses to start
          without a configured project in single-tenant mode (the
          dashboard would have no project to point at).
        '';
      }
      {
        # Phase 3 Wave 3 (prancy-napping-pie): OIDC + proxy-header
        # are mutually exclusive. Catch at evaluation so the
        # operator never reaches the runtime SSOConfigError.
        assertion = !(cfg.sso.oidc != null && cfg.sso.proxyHeader != null);
        message = ''
          services.agent-mcp.sso.oidc and services.agent-mcp.sso.proxyHeader
          are mutually exclusive. Pick one: OIDC (authorization-code
          flow against an external IdP) OR proxy-header trust
          (upstream proxy supplies the username). Setting both would
          create surprising precedence rules and a security footgun.
        '';
      }
    ];

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
          Environment = [
            # Backend MUST point at the same router.db as the router
            # unit below. agent_mcp.app.deps._resolve_session_user lazily
            # `from ..router import identity` and calls
            # identity.get_session(...), which opens the path returned
            # by agent_mcp.router.migrations_runner.get_router_db_path()
            # — that helper honours AGENT_MCP_ROUTER_DB and otherwise
            # falls back to /var/lib/agent-mcp/router.db (the system-mode
            # default). User-mode units cannot read that path, the open
            # raises PermissionError, the bare ``except Exception`` in
            # _resolve_session_user logs "treating as anonymous", and
            # every operator-only endpoint 401s.
            #
            # Same drift pattern as PR #223 (router unit got the var)
            # and PR #224 (forwarding_hmac ExecStartPre). The value
            # below MUST stay identical to the router unit's
            # AGENT_MCP_ROUTER_DB — both processes open the same file.
            "AGENT_MCP_ROUTER_DB=${config.xdg.dataHome}/agent-mcp/router.db"
          ];
          # F015 v4/v6/v7 port from nix/module.nix (PRs #214, #216,
          # #217). The system-mode NixOS module gained these
          # ExecStartPre lines; the home-manager template here was
          # never updated, so real user-mode deploys hit:
          #
          #   agent-mcp-launcher: Error: Invalid value for
          #     '--forwarding-hmac-in':
          #     File '/run/user/1000/agent-mcp/<name>/forwarding_hmac'
          #     does not exist.
          #   systemd: agent-mcp@<name>.service: Main process exited,
          #     code=exited, status=2/INVALIDARGUMENT
          #   systemd: Scheduled restart job, restart counter is at 630.
          #
          # The launcher passes ``--forwarding-hmac-in
          # $XDG_RUNTIME_DIR/agent-mcp/<name>/forwarding_hmac`` to the
          # backend; without an ExecStartPre to materialise that file,
          # the unit crash-loops forever.
          #
          # Rationale (per PR #214): the router (Python) used to write
          # the key, but systemd's ``Restart=on-failure`` reactivates
          # the unit without going through the router. Owning key
          # generation in the unit ExecStartPre guarantees the file
          # exists on EVERY start path (manual ``systemctl --user
          # start``, on-failure restart, login activation).
          #
          # Notes on the shell + binaries:
          # - ``pkgs.runtimeShell`` (PR #216 / F015 v6): coreutils
          #   does NOT ship ``sh``; the original v4 used
          #   ``${pkgs.coreutils}/bin/sh`` and every start failed with
          #   ``status=203/EXEC``.
          # - 32 raw bytes (PR #217 / F015 v7): bytes are binary; the
          #   router's reader does NOT ``.strip()`` them. Use ``head
          #   -c 32 /dev/urandom`` directly into the file.
          # - Idempotent (``test -f … || { … ; }``): the router caches
          #   the bytes in-memory and a re-spawn must not rotate the
          #   key — see commit 862e594 (cache + file consistency).
          # - ``$RUNTIME_DIRECTORY``: set by systemd when
          #   ``RuntimeDirectory=`` is declared; resolves to
          #   ``$XDG_RUNTIME_DIR/agent-mcp/<instance>``.
          #
          # Defensive socket cleanup retained as the second
          # ExecStartPre (was the only entry before this fix).
          ExecStartPre = [
            "${pkgs.runtimeShell} -c 'test -f \"$RUNTIME_DIRECTORY/forwarding_hmac\" || { ${pkgs.coreutils}/bin/head -c 32 /dev/urandom > \"$RUNTIME_DIRECTORY/forwarding_hmac\" && ${pkgs.coreutils}/bin/chmod 600 \"$RUNTIME_DIRECTORY/forwarding_hmac\"; }'"
            "${pkgs.coreutils}/bin/rm -f %t/agent-mcp/%i/backend.sock"
          ];
          ExecStart = "${resolvedPkgs.agentMcpLauncher}/bin/agent-mcp-launcher %i";
          Restart = "on-failure";
          RestartSec = 5;
          TimeoutStopSec = 10;
        } // hardening;
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
            # Router DB lives under XDG_DATA_HOME (default
            # ~/.local/share/agent-mcp/router.db). Without this, the
            # python default in agent_mcp.router.migrations_runner
            # (_DEFAULT_ROUTER_DB = /var/lib/agent-mcp/router.db) kicks
            # in — and user-mode systemd units cannot write there, so
            # the router restart-loops with
            # `PermissionError: [Errno 13] Permission denied:
            # '/var/lib/agent-mcp'`. The NixOS module uses
            # /var/lib/agent-mcp/ which works for its system-mode user;
            # user-mode home-manager units need an XDG path. The
            # migrations_runner already mkdirs the parent so no
            # tmpfiles equivalent is required.
            "AGENT_MCP_ROUTER_DB=${config.xdg.dataHome}/agent-mcp/router.db"
          ]
          # Phase 3 Wave 3 (prancy-napping-pie): SSO env vars are
          # appended conditionally. OIDC and proxy-header are
          # mutually exclusive (enforced via the assertion above);
          # both branches expand to [] when not configured, which
          # leaves the legacy username/password mode active.
          ++ lib.optionals (cfg.sso.oidc != null) [
            "AGENT_MCP_SSO_OIDC_ISSUER=${cfg.sso.oidc.issuer}"
            "AGENT_MCP_SSO_OIDC_CLIENT_ID=${cfg.sso.oidc.clientId}"
            "AGENT_MCP_SSO_OIDC_CLIENT_SECRET_FILE=${
              toString cfg.sso.oidc.clientSecretFile
            }"
            "AGENT_MCP_SSO_OIDC_PROVIDER_NAME=${cfg.sso.oidc.providerName}"
            "AGENT_MCP_SSO_OIDC_GROUP_MAPPING=${
              builtins.toJSON cfg.sso.oidc.groupMapping
            }"
            "AGENT_MCP_SSO_OIDC_SCOPES=${
              lib.concatStringsSep " " cfg.sso.oidc.scopes
            }"
          ]
          ++ lib.optionals (
            cfg.sso.oidc != null && cfg.sso.oidc.redirectUrl != null
          ) [
            "AGENT_MCP_SSO_OIDC_REDIRECT_URL=${cfg.sso.oidc.redirectUrl}"
          ]
          ++ lib.optionals (cfg.sso.proxyHeader != null) [
            "AGENT_MCP_SSO_PROXY_HEADER=${cfg.sso.proxyHeader.trustHeader}"
            "AGENT_MCP_SSO_PROXY_TRUSTED_IPS=${
              lib.concatStringsSep "," cfg.sso.proxyHeader.trustedIps
            }"
            "AGENT_MCP_SSO_PROXY_DEFAULT_SYSADMIN=${
              if cfg.sso.proxyHeader.defaultIsSysadmin then "true" else "false"
            }"
          ];
          # systemd creates and owns %t/agent-mcp/ at 0700 so the per-
          # project subdirs the template creates inherit a private
          # parent. (The template's own RuntimeDirectory creates each
          # %t/agent-mcp/<name>/; this just ensures the parent exists.)
          RuntimeDirectory = "agent-mcp";
          RuntimeDirectoryMode = "0700";
          # Single-tenant: seed projects.local.json with the one
          # declared project before the router starts. Multi-tenant
          # this is a no-op (no ExecStartPre is set).
          ExecStartPre = lib.mkIf (!cfg.multiTenant) [
            "${singleProjectSeedScript}"
          ];
          ExecStart =
            if cfg.multiTenant then
              "${resolvedPkgs.agentMcpRouterWrapper}/bin/agent-mcp-router"
            else
              # The wrapper does `exec python -m agent_mcp.cli router "$@"`,
              # so passing flags through it lands them on the router
              # subcommand as expected.
              "${resolvedPkgs.agentMcpRouterWrapper}/bin/agent-mcp-router "
              + "--single-tenant ${lib.escapeShellArg cfg.singleProject.name} "
              + "--single-workspace ${lib.escapeShellArg cfg.singleProject.workspace}";
          Restart = "on-failure";
          RestartSec = 10;
          # Defense-in-depth ceiling on the SIGTERM → exit window.
          # The router's own `_drain_proxy_tasks` on_shutdown hook +
          # `shutdown_timeout=3.0` on `web.run_app` close down
          # in-flight MCP Streamable-HTTP proxy connections inside
          # a few seconds; this 15 s ceiling means even if the
          # in-process drain misfires, systemd's SIGKILL window is
          # short enough that the operator sees "router restarting"
          # rather than the previous 90 s deploy outage. The default
          # (90 s, inherited from systemd) was the source of the
          # 2026-06-04 08:57 production stall — see PR <#TBD>.
          TimeoutStopSec = 15;
        } // hardening;
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
        } // hardening;
        Install.WantedBy = [ "default.target" ];
      };
    }) cfg.daemonAgents);
  };
}
