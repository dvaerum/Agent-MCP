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

  # The package set the module was instantiated with. Named so the
  # `services.agent-mcp.pkgs` option below can default to it without
  # the attribute name shadowing the module argument.
  #
  # Scope boundary: `cfg.pkgs` governs agent-mcp's OWN derivations —
  # the ones coupled to a single interpreter and to each other. The
  # generic shell utilities this file reaches for directly (`pkgs.jq`,
  # `pkgs.coreutils`, `pkgs.runtimeShell` in the unit ExecStartPre
  # lines) stay on the consumer's set on purpose: nothing about them
  # is Python-coupled, and reusing the copies already in the profile
  # keeps an override from dragging a second coreutils/bash into the
  # closure for no benefit.
  consumerPkgs = pkgs;

  # EVERY derivation this module installs comes out of this one
  # import: the Python tree, the interpreter the wrappers exec, the
  # PYTHONPATH they bake in, and the dashboard. That is deliberate —
  # see the `pkgs` option below. Nothing may be spliced onto the
  # result afterwards; an attribute added with `//` here would be
  # read by nothing, because packages.nix has already closed over its
  # own `agentMcpPy` and `python` by the time it returns. (That was
  # the `services.agent-mcp.package` bug: a silently ineffective
  # override. The option is gone — see the mkRemovedOptionModule in
  # `imports` — and tests/test_nix_module_package_set.py keeps this
  # single-import shape from regressing.)
  pkgs' = import ./packages.nix {
    pkgs = cfg.pkgs;
    # nixpkgs' `lib` from the SAME set, matching flake.nix's call
    # site. packages.nix needs it only for `lib.versionOlder`, and
    # taking it from cfg.pkgs keeps the whole import single-sourced
    # rather than half consumer-set, half home-manager's extended lib.
    lib = cfg.pkgs.lib;
    # cfg.source defaults to the fork's repo root via the flake's
    # `homeModules.default` wrapper. Operators can override
    # to pin a different source tree (e.g. for local development).
    src = cfg.source;
  };

  daemonAgentInstanceName = a: "${a.project}--${a.agentId}";

  # `conexusDaemonAgentPackage` (Phase F): the Rust binary resolves
  # its own token/URL/cursor paths from the `<project>--<agent_id>`
  # instance argument directly (see rust/conexus-daemon-agent/src/
  # main.rs), so unlike the Python pair this needs no per-invocation
  # `cfg.router.port` substitution baked into the wrapper itself --
  # `--router-port` is passed as a real CLI flag at ExecStart time
  # instead (see the unit definition below).
  daemonAgentWrapper =
    if cfg.conexusDaemonAgentPackage != null
    then cfg.conexusDaemonAgentPackage
    else pkgs'.agentMcpDaemonAgentWrapper cfg.router.port;

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
  imports = [
    # `services.agent-mcp.package` was a single-derivation override of
    # the Python tree. It never reached anything that runs.
    #
    # The module spliced it onto the *result* of nix/packages.nix
    # (`pkgs' // { agentMcpPy = cfg.package; }`), but by then
    # packages.nix had already built agentMcpRouterWrapper,
    # agentMcpBackendWrapper, agentMcpLauncher and the daemon-agent
    # wrapper around its OWN `agentMcpPy` and `python` — those
    # wrappers bake in `''${python}/bin/python` plus a PYTHONPATH
    # computed from the internal tree. Overriding the attribute
    # replaced something nothing downstream reads, so every unit kept
    # exec'ing the internally-built tree. A silent no-op.
    #
    # It also cannot be repaired in that shape. To make the wrappers
    # honour an operator-supplied derivation they would need its
    # interpreter and its site-packages layout, and a derivation
    # produced by nixpkgs' Python *application* builder carries
    # neither: `pythonModule` is absent on applications (verified
    # against nixpkgs f13ff45), so there is no way to recover the
    # python it was built with. Pairing a 3.14-built tree with the
    # consumer's 3.13 interpreter would not merely mix closures, it
    # would fail to import.
    #
    # The coherent knob is the whole package SET —
    # `services.agent-mcp.pkgs` — which moves app, interpreter,
    # wrappers and dashboard together.
    (lib.mkRemovedOptionModule [ "services" "agent-mcp" "package" ] ''
      services.agent-mcp.package has been removed: it was a silent
      no-op. The override was applied AFTER nix/packages.nix had
      already built the router / backend / launcher / daemon-agent
      wrappers against its own internal derivation, so the systemd
      units kept running the internally-built tree.

      A single derivation also cannot carry the interpreter and
      site-packages layout the wrappers need, so the option could not
      be fixed in place.

      Use `services.agent-mcp.pkgs` to build every agent-mcp
      derivation from a different package set (the supported way to
      escape a stable channel's Python security lag), and/or
      `services.agent-mcp.source` to build from a different source
      tree.
    '')
  ];

  options.services.agent-mcp = {
    enable = lib.mkEnableOption "agent-mcp (multi-tenant router + daemon agents)";

    source = lib.mkOption {
      type = lib.types.path;
      description = ''
        Path to the agent-mcp source tree. Defaults to the fork's
        repo root via the flake's `homeModules.default`
        wrapper; override to pin a different source tree.
      '';
    };

    conexusLauncherPackage = lib.mkOption {
      type = lib.types.nullOr lib.types.package;
      default = null;
      description = ''
        The CoNexus Rust backend's systemd-user-template launcher
        (`nix/conexus.nix`'s `conexusLauncher`), for the
        `conexus@<name>.service` user template (Phase D1 step 5,
        prancy-napping-pie). `null` (the default) omits the template
        entirely -- this module has no direct access to the `crane`
        flake input, so the flake's own `homeModules.default`
        wrapper sets this from its already-built `conexusPkgs`, the
        same "build elsewhere, pass the package in" pattern `source`
        already uses one option up.
      '';
    };

    conexusDaemonAgentPackage = lib.mkOption {
      type = lib.types.nullOr lib.types.package;
      default = null;
      description = ''
        The CoNexus Rust reference daemon-agent binary
        (`nix/conexus.nix`'s `conexusDaemonAgentWrapper`), for
        `agent-mcp-daemon-agent@<instance>.service` (Phase F,
        prancy-napping-pie -- ported for implementation-language
        consistency, not a functional need: this is a pure MCP
        wire-protocol client with zero dependency on whether the
        backend/router it talks to is Python or Rust).

        `null` (the default) falls back to the Python pair
        (`agentMcpDaemonAgentRunner`/`Wrapper` in `packages.nix`).
        Unlike `conexusRouterPackage` below, this IS set by the
        flake's own `homeModules.default` wrapper (same auto-wired
        pattern as `conexusLauncherPackage`) -- a daemon-agent
        instance is a per-agent client process with no port to bind
        and no singleton to race, so building/switching this in
        carries none of the router's production-outage risk.
      '';
    };

    conexusRouterPackage = lib.mkOption {
      type = lib.types.nullOr lib.types.package;
      default = null;
      description = ''
        The CoNexus Rust router wrapper (`nix/conexus.nix`'s
        `conexusRouterWrapper`), for the singleton `conexus-router`
        user service (Phase F packaging prerequisite,
        prancy-napping-pie). `null` (the default) omits the service
        entirely.

        Deliberately NOT set by the flake's own
        `homeModules.default` wrapper the way
        `conexusLauncherPackage` is -- unlike `conexus@<name>`, which
        starts on-demand per-project and only when a project's own
        `backend_impl` registry flag requests it (so building the
        launcher changes nothing for a running system), the router is
        a SINGLETON that binds the same port `agent-mcp-router`
        already listens on (`AGENT_MCP_ROUTER_PORT`, default 1337).
        Defaulting this option non-null anywhere would make the
        `conexus-router` unit's `Install.WantedBy` take effect on the
        very next `home-manager switch`, racing the still-live Python
        router for that port -- a real production-outage risk, not a
        theoretical one. Setting this option is therefore the
        deliberate act of opting into the router cutover itself; do
        so only after the operator-authority decision recorded in the
        migration plan's Phase F section, not as a side effect of
        picking up a newer agent-mcp flake input.
      '';
    };

    pkgs = lib.mkOption {
      type = lib.types.pkgs;
      default = consumerPkgs;
      defaultText = lib.literalMD
        "the `pkgs` this home-manager configuration was evaluated with";
      example = lib.literalExpression ''
        import agent-mcp.inputs.nixpkgs {
          inherit (pkgs.stdenv.hostPlatform) system;
        }
      '';
      description = ''
        Package set EVERY agent-mcp derivation is built from: the
        Python application, the interpreter its wrappers exec, the
        PYTHONPATH they bake in, the shell wrappers themselves and the
        dashboard. Defaults to the `pkgs` home-manager itself is
        configured with, which is what you want unless you have a
        specific reason otherwise.

        **Why you would set it — the stable-channel security lag.**
        agent-mcp's router is an aiohttp server exposed on whatever
        interface `router.externalUrl` fronts. NixOS *stable* branches
        do not take routine Python security backports, so a host
        tracking e.g. nixos-26.05 keeps that branch's frozen aiohttp
        (3.13.5, carrying advisories for pipelining DoS,
        `max_line_size` / `client_max_size` bypass, CRLF injection and
        WebSocket request smuggling) for the life of the release,
        while nixos-unstable already ships the fixed 3.14.3. Pointing
        this option at a fresher package set rebuilds agent-mcp — and
        ONLY agent-mcp — against it; the rest of the home-manager
        profile stays on the stable channel:

        ```nix
        services.agent-mcp.pkgs = import agent-mcp.inputs.nixpkgs {
          inherit (pkgs.stdenv.hostPlatform) system;
        };
        ```

        Note that agent-mcp's own flake pin does NOT do this for you.
        The module builds from the *consumer's* package set by
        construction, so dropping `inputs.agent-mcp.inputs.nixpkgs.follows`
        in your flake changes only what `nix build` inside agent-mcp's
        own flake produces — not your deployed closure. This option is
        the switch that does.

        It must be a whole package set, not a single derivation: the
        wrappers bake in `''${python}/bin/python` and a PYTHONPATH
        built from the same set's site-packages, so an application
        from one channel with an interpreter from another does not
        merely mix closures, it fails to import. (That is why
        `services.agent-mcp.package` was removed.)
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

      impl = lib.mkOption {
        type = lib.types.enum [ "python" "rust" ];
        default = "python";
        description = ''
          Which router implementation is the ACTIVE one -- the one
          `Install.WantedBy` names, so it's the one that actually gets
          started by a `home-manager switch`/`systemctl --user start
          default.target`. The other implementation's unit still
          exists (both `agent-mcp-router`/`conexus-router` are always
          defined when their prerequisites are met -- see
          `conexusRouterPackage`) but carries no `Install.WantedBy`,
          so it never starts on its own.

          This is the router's rollback-flip mechanism (Phase F,
          prancy-napping-pie) -- the singleton equivalent of the
          per-project `backend_impl` registry flag, since the router
          has no smaller cutover unit to canary (Guiding Principle 2:
          there is only one router). Flipping this option and running
          `home-manager switch` is the intended, one-line rollback
          path in EITHER direction: `"rust" -> "python"` if a
          `conexus-router` cutover needs reverting, or the reverse to
          try it. Both units read/write the SAME `router.db` (see
          `AGENT_MCP_ROUTER_DB` on both), so switching does not
          migrate or duplicate any state.

          `"rust"` requires `conexusRouterPackage` to be set (enforced
          by an assertion) -- flipping to Rust without ever building
          its package would otherwise silently leave NO router
          running at all after the switch.
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
        default = pkgs'.agentMcpDashboard;
        defaultText = lib.literalMD
          "the dashboard built from `services.agent-mcp.source` using `services.agent-mcp.pkgs`";
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
      {
        # Phase F (prancy-napping-pie): flipping router.impl to "rust"
        # with no conexusRouterPackage set would leave the profile
        # with NO active router unit at all after the switch (neither
        # agent-mcp-router's WantedBy, cleared by the flip, nor
        # conexus-router's, since that unit doesn't even exist without
        # the package) -- catch this at evaluation, not as a silent
        # outage discovered after `home-manager switch`.
        assertion = cfg.router.impl == "rust" -> cfg.conexusRouterPackage != null;
        message = ''
          services.agent-mcp.router.impl = "rust" requires
          services.agent-mcp.conexusRouterPackage to be set -- without
          it there is no conexus-router unit for Install.WantedBy to
          name, and this flip would leave NO router running at all.
        '';
      }
    ];

    home.packages = [
      pkgs'.agentMcpBackendWrapper             # invoked by the systemd template launcher
      pkgs'.agentMcpRouterWrapper              # invoked by agent-mcp-router.service
      pkgs'.agentMcpLauncher                   # invoked by agent-mcp@.service via %i
      daemonAgentWrapper                       # invoked by agent-mcp-daemon-agent@.service via %i
      pkgs'.agentMcpDaemonAgentPrecompactHook  # operator-installed PreCompact hook
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
          ExecStart = "${pkgs'.agentMcpLauncher}/bin/agent-mcp-launcher %i";
          Restart = "on-failure";
          RestartSec = 5;
          TimeoutStopSec = 10;
        } // hardening;
        # Not WantedBy any target — instances are started on demand by
        # the router (`systemctl --user start agent-mcp@<name>`).
      };

      # `conexus@<name>.service` — the CoNexus Rust backend user
      # template (Phase D1 step 5). Structurally identical to
      # `agent-mcp@` above (same RuntimeDirectory, same ExecStartPre
      # HMAC-key/stale-socket handling, same restart budget, same
      # hardening) except it has no `AGENT_MCP_ROUTER_DB` environment
      # entry -- conexus-backend doesn't touch the router.db at all
      # (no group-capability overlay from the per-project backend,
      # matching Python's own documented behavior for this seam) --
      # and the `ExecStart` target.
      #
      # Decision #1 (2026-09-04, operator): SHARES `agent-mcp@`'s
      # `RuntimeDirectory` (`agent-mcp/%i`, not a new `conexus/%i`) --
      # a `backend_impl` flip is a same-path process swap. See
      # `nix/module.nix`'s parallel system-mode template for the full
      # rationale on why sharing the runtime dir across two mutually-
      # exclusive unit templates is safe.
      #
      # `null` by default (see `conexusLauncherPackage`'s option doc)
      # -- omitted until the flake's `homeModules.default`
      # wrapper sets it from `conexusPkgs.conexusLauncher`.
      "conexus@" = lib.mkIf (cfg.conexusLauncherPackage != null) {
        Unit = {
          Description = "CoNexus backend — project %i (UDS)";
        };
        Service = {
          Type = "simple";
          RuntimeDirectory = "agent-mcp/%i";
          RuntimeDirectoryMode = "0700";
          ExecStartPre = [
            "${pkgs.runtimeShell} -c 'test -f \"$RUNTIME_DIRECTORY/forwarding_hmac\" || { ${pkgs.coreutils}/bin/head -c 32 /dev/urandom > \"$RUNTIME_DIRECTORY/forwarding_hmac\" && ${pkgs.coreutils}/bin/chmod 600 \"$RUNTIME_DIRECTORY/forwarding_hmac\"; }'"
            "${pkgs.coreutils}/bin/rm -f %t/agent-mcp/%i/backend.sock"
          ];
          ExecStart = "${cfg.conexusLauncherPackage}/bin/conexus-launcher %i";
          Restart = "on-failure";
          RestartSec = 5;
          TimeoutStopSec = 10;
        } // hardening;
        # Not WantedBy any target — instances are started on demand by
        # the router (`systemctl --user start conexus@<name>`).
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
            "AGENT_MCP_README_HTML=${pkgs'.readmeHtml}"
            "AGENT_MCP_INSTALLER_TEMPLATE=${pkgs'.installerTemplate}"
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
              "${pkgs'.agentMcpRouterWrapper}/bin/agent-mcp-router"
            else
              # The wrapper does `exec python -m agent_mcp.cli router "$@"`,
              # so passing flags through it lands them on the router
              # subcommand as expected.
              "${pkgs'.agentMcpRouterWrapper}/bin/agent-mcp-router "
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
        # `router.impl` (Phase F, prancy-napping-pie): only the ACTIVE
        # implementation carries `Install.WantedBy` -- see that
        # option's own doc for the rollback-flip rationale. Default
        # ("python") reproduces this unit's behavior from before this
        # option existed exactly (WantedBy always set) -- see
        # `test_unset_option_is_exactly_todays_behaviour`-style
        # coverage for this same "new option changes nothing by
        # default" contract this module already holds itself to. A
        # plain Nix `if`, not `lib.mkIf` -- whether `mkIf` resolves
        # correctly here depends on `Install.WantedBy` being a real
        # `mkOption`-typed submodule field in the CONSUMER's actual
        # home-manager, which this repo has no local copy of to check
        # directly (this module is exported for others to import, not
        # imported by this flake itself); a plain `if` sidesteps the
        # question entirely by resolving at eval time regardless of
        # how the target option is declared.
        Install.WantedBy = if cfg.router.impl == "python" then [ "default.target" ] else [ ];
      };

      # `conexus-router` — the CoNexus Rust router (Phase F packaging
      # prerequisite, prancy-napping-pie). Structurally mirrors
      # `agent-mcp-router` above (same RuntimeDirectory, same
      # single-tenant seeding, same SSO env-var construction, same
      # restart budget/hardening) except: (1) most of Python's
      # env-var-only config surface is a real CLI flag on
      # `conexus-router` (see its own `Cli` struct doc in
      # rust/conexus-router/src/main.rs) -- passed as flags here
      # rather than duplicated as env vars; (2) `AGENT_MCP_README_HTML`/
      # `AGENT_MCP_INSTALLER_TEMPLATE` are deliberately omitted --
      # `conexus-router` parses `--readme-html`/`--installer-template`
      # but doesn't consume them yet (the `client_config`/`installer`
      # routes they'd feed stay explicitly, permanently deferred per
      # the migration plan's own PR23-step-6 research finding).
      #
      # `null` by default (see `conexusRouterPackage`'s own option doc
      # for why this is NOT auto-wired the way `conexusLauncherPackage`
      # is) -- defining this unit changes nothing for any consumer
      # until they explicitly set `conexusRouterPackage`, which is
      # itself the router-cutover decision, not a side effect of
      # taking this option's mere existence.
      "conexus-router" = lib.mkIf (cfg.conexusRouterPackage != null) {
        Unit = {
          Description = "CoNexus router (URL-keyed, idle-stop)";
          After = [ "ollama.service" ];
        };
        Service = {
          Type = "simple";
          Environment = [
            # Same XDG-path rationale as `agent-mcp-router`'s own
            # `AGENT_MCP_ROUTER_DB` comment above: user-mode units
            # cannot write to conexus-router's own compiled-in
            # `/var/lib/agent-mcp/router.db` default.
            "AGENT_MCP_ROUTER_DB=${config.xdg.dataHome}/agent-mcp/router.db"
          ]
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
          RuntimeDirectory = "agent-mcp";
          RuntimeDirectoryMode = "0700";
          ExecStartPre = lib.mkIf (!cfg.multiTenant) [
            "${singleProjectSeedScript}"
          ];
          ExecStart =
            let
              commonFlags =
                "--port ${toString cfg.router.port} "
                + "--projects-file %h/.config/agent-mcp/projects.local.json "
                + "--sock-dir %t/agent-mcp "
                + "--dashboard-dir ${cfg.dashboard.package}/share/agent-mcp-dashboard "
                + "--external-url ${lib.escapeShellArg cfg.router.externalUrl} "
                + "--idle-sec ${toString cfg.router.idleSec}";
            in
            if cfg.multiTenant then
              "${cfg.conexusRouterPackage}/bin/conexus-router " + commonFlags
            else
              "${cfg.conexusRouterPackage}/bin/conexus-router " + commonFlags + " "
              + "--single-tenant ${lib.escapeShellArg cfg.singleProject.name} "
              + "--single-workspace ${lib.escapeShellArg cfg.singleProject.workspace}";
          Restart = "on-failure";
          RestartSec = 10;
          # Same defense-in-depth ceiling as `agent-mcp-router` above,
          # even though `conexus-backend`'s own proxy-drain behavior
          # hasn't hit an equivalent 90s-stall incident (none of this
          # migration's own shutdown-path tests have needed a longer
          # window) -- kept at the identical value so a router restart
          # behaves identically to an operator regardless of which
          # implementation is live.
          TimeoutStopSec = 15;
        } // hardening;
        # `router.impl` (Phase F, prancy-napping-pie): mirrors
        # `agent-mcp-router`'s own conditional WantedBy above -- only
        # the ACTIVE implementation gets started. Default ("python")
        # means this unit carries no WantedBy at all, same as before
        # `router.impl` existed (the assertion above guarantees
        # `conexusRouterPackage` is set whenever `impl == "rust"`, so
        # this can never resolve to "wanted but the unit doesn't
        # exist"). Plain Nix `if`, not `lib.mkIf` -- see the identical
        # note on `agent-mcp-router`'s own WantedBy above.
        Install.WantedBy = if cfg.router.impl == "rust" then [ "default.target" ] else [ ];
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
          # The two implementations take a different CLI shape: the
          # Rust binary resolves its own token/URL/cursor paths from
          # the instance argument PLUS an explicit `--router-port`
          # flag (no per-build @router_port@ substitution needed);
          # the Python wrapper already has the port baked in via
          # `agentMcpDaemonAgentWrapper cfg.router.port` above, so it
          # takes the instance name alone.
          ExecStart =
            if cfg.conexusDaemonAgentPackage != null
            then "${daemonAgentWrapper}/bin/conexus-daemon-agent --router-port ${toString cfg.router.port} ${daemonAgentInstanceName a}"
            else "${daemonAgentWrapper}/bin/agent-mcp-daemon-agent ${daemonAgentInstanceName a}";
          Restart = "on-failure";
          RestartSec = 10;
        } // hardening;
        Install.WantedBy = [ "default.target" ];
      };
    }) cfg.daemonAgents);
  };
}
