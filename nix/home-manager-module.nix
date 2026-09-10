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
#   - A thin always-on router (conexus-router.service, the CoNexus Rust
#     router — see `conexusRouterPackage` below) on 127.0.0.1:<port>
#     (default 1337) does URL-path routing + activity tracking, calls
#     systemctl --user start/stop for lazy spawn + idle shutdown, and
#     serves the Next.js static dashboard at /agent-mcp/__dashboard/<name>/.
#   - Per-project backends (conexus@<name>.service template — see
#     `conexusLauncherPackage` below) are lazy-started by the router on
#     first MCP request and idle-stopped after
#     services.agent-mcp.router.idleSec seconds.
#
# The Python implementation (agent-mcp@<name>.service /
# agent-mcp-router.service, and the `router.impl` A/B flip between
# them and their Rust counterparts) was retired once the Python source
# tree was deleted wholesale and the Rust `rust/` workspace reached
# functional completeness — see git history around the packages.nix
# and this file's own `router.impl` removal for the cutover.
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

  # EVERY derivation packages.nix still builds (the dashboard and the
  # daemon-agent PreCompact hook — the Python application, its
  # interpreter, and the PYTHONPATH-coupled wrappers around it were
  # retired together with the Python source tree) comes out of this
  # one import. That is deliberate — see the `pkgs` option below.
  # Nothing may be spliced onto the result afterwards; an attribute
  # added with `//` here would be read by nothing, because
  # packages.nix has already closed over its own package set by the
  # time it returns. (That was the `services.agent-mcp.package` bug,
  # back when this import also built the Python tree: a silently
  # ineffective override. The option is gone — see the
  # mkRemovedOptionModule in `imports` — and
  # tests/test_nix_module_package_set.py keeps this single-import
  # shape from regressing.)
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

  # `conexusDaemonAgentPackage` is the only daemon-agent implementation
  # now that the Python pair (`agentMcpDaemonAgentRunner`/`Wrapper` in
  # packages.nix) was deleted with the rest of the Python tree. `null`
  # is still a valid value (the option keeps `nullOr package` rather
  # than becoming a no-default required option) — mirroring the exact
  # same "silently omit the derived unit(s)" idiom `conexusLauncherPackage`
  # and `conexusRouterPackage` already use for their own units, rather
  # than throwing at eval time for a consumer who hasn't wired a
  # package in yet. See each per-instance unit's own `lib.mkIf` below.
  #
  # The Rust binary resolves its own token/URL/cursor paths from the
  # `<project>--<agent_id>` instance argument directly (see rust/
  # conexus-daemon-agent/src/main.rs), so unlike the old Python pair
  # this needs no per-invocation `cfg.router.port` substitution baked
  # into the wrapper itself -- `--router-port` is passed as a real CLI
  # flag at ExecStart time instead (see the unit definition below).
  daemonAgentWrapper = cfg.conexusDaemonAgentPackage;

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
        `conexus@<name>.service` user template -- the ONLY per-project
        backend implementation now that the Python one was retired.
        `null` (the default) omits the template entirely -- this
        module has no direct access to the `crane` flake input, so the
        flake's own `homeModules.default` wrapper sets this from its
        already-built `conexusPkgs`, the same "build elsewhere, pass
        the package in" pattern `source` already uses one option up.
        A `home-manager switch` with this left `null` and no override
        leaves the profile with no per-project backend at all --
        there is no other implementation left to fall back to.
      '';
    };

    conexusDaemonAgentPackage = lib.mkOption {
      type = lib.types.nullOr lib.types.package;
      default = null;
      description = ''
        The CoNexus Rust reference daemon-agent binary
        (`nix/conexus.nix`'s `conexusDaemonAgentWrapper`), for
        `agent-mcp-daemon-agent@<instance>.service` -- the ONLY
        daemon-agent implementation now that the Python pair
        (`agentMcpDaemonAgentRunner`/`Wrapper`) was retired with the
        rest of the Python tree.

        `null` (the default) omits every `daemonAgents` entry's unit
        entirely -- the same "silently omit the derived unit" idiom
        `conexusLauncherPackage`/`conexusRouterPackage` already use,
        rather than a no-default required option that would fail
        eval for a consumer who hasn't wired a package in yet. Set by
        the flake's own `homeModules.default` wrapper (same
        auto-wired pattern as `conexusLauncherPackage`) -- a
        daemon-agent instance is a per-agent client process with no
        port to bind and no singleton to race, so building/switching
        this in carries none of the router's former production-outage
        risk (see `conexusRouterPackage`'s own doc for that history).
      '';
    };

    conexusRouterPackage = lib.mkOption {
      type = lib.types.nullOr lib.types.package;
      default = null;
      description = ''
        The CoNexus Rust router wrapper (`nix/conexus.nix`'s
        `conexusRouterWrapper`), for the singleton `conexus-router`
        user service -- the ONLY router implementation now that the
        Python one (`agent-mcp-router.service`) and the `router.impl`
        A/B flip between them were retired. `null` (the default)
        omits the service entirely -- a `home-manager switch` with
        this left `null` and no override leaves the profile with no
        router at all (mirrors `conexusLauncherPackage`'s own
        silently-no-backend outcome one option up; neither option
        carries an assertion forcing it non-null, for the same
        reason).

        Auto-wired by the flake's own `homeModules.default` wrapper
        (`lib.mkDefault`, same pattern as `conexusLauncherPackage`/
        `conexusDaemonAgentPackage`) -- this was DELIBERATELY not the
        case while the Python router was still the live A/B default:
        `conexus-router` is a SINGLETON binding the same port
        `agent-mcp-router` used to (`AGENT_MCP_ROUTER_PORT`, default
        1337), so auto-defaulting this while a Python router could
        still be the active `Install.WantedBy` target risked a port
        race on the very next `home-manager switch`. That risk no
        longer exists: the Python router doesn't exist to race against
        (it and `router.impl` were deleted together), so there is
        exactly one router implementation for this option to enable,
        and auto-wiring it now matches `conexusLauncherPackage`'s own
        rationale exactly -- the flake building the ONLY Rust
        implementation and handing it to the module is no longer a
        cutover decision, it's just how the module gets its one
        router binary, same as the backend launcher.
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
        Package set the dashboard (Next.js static export) and the
        daemon-agent PreCompact hook are built from in packages.nix —
        and, via the flake's own `homeModules.default` wrapper, ALSO
        the package set + toolchain (`crane.mkLib`) the CoNexus Rust
        binaries (`conexusLauncherPackage`/`conexusRouterPackage`/
        `conexusDaemonAgentPackage`) are built from, since that
        wrapper threads this same option straight into its own
        `nix/conexus.nix` import. Defaults to the `pkgs` home-manager
        itself is configured with, which is what you want unless you
        have a specific reason otherwise.

        **Why you would set it — the stable-channel security lag.**
        NixOS *stable* branches do not take routine backports the way
        nixos-unstable does, so a host tracking e.g. nixos-26.05 can
        lag behind on a dependency with a known advisory (previously:
        the Python router's aiohttp; today the equivalent risk surface
        is whatever crates.io advisories land against the `rust/`
        workspace's dependency graph, or the dashboard's npm tree).
        Pointing this option at a fresher package set rebuilds
        agent-mcp's own derivations — and, through the flake wrapper,
        the Rust toolchain they compile against — against it; the rest
        of the home-manager profile stays on the stable channel:

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

        It must be a whole package set, not a single derivation — a
        derivation from one channel paired with a toolchain/stdenv from
        another does not merely mix closures, it can fail to build or
        import. (That mismatch is also why `services.agent-mcp.package`
        was removed, back when this option's own derivation was the
        Python application specifically.)
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
              (--project-dir on conexus@<name>.service). The
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

    # No `conexusLauncherPackage`/`conexusRouterPackage != null`
    # assertion here -- consistent with how those two options already
    # worked before `router.impl` existed (and still work now that
    # it's gone): leaving either `null` silently omits its unit
    # (`lib.mkIf` below), the same "no assertion, just no unit" shape
    # this module has always used for an optional package producing an
    # optional unit. There is no longer a second implementation for a
    # missing package to fall back to, so an operator who never sets
    # `conexusRouterPackage` gets no router, not a Python one.
    home.packages =
      lib.optional (daemonAgentWrapper != null) daemonAgentWrapper  # invoked by agent-mcp-daemon-agent@.service via %i
      ++ [
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
    #   "conexus@"               — per-project backend template
    #                              (started lazily by the router),
    #                              omitted when `conexusLauncherPackage`
    #                              is null.
    #   "conexus-router"         — always-on router (URL-keyed,
    #                              idle-stop), omitted when
    #                              `conexusRouterPackage` is null.
    #   "agent-mcp-daemon-agent@<instance>"
    #                            — per-instance daemon-agent runner,
    #                              one entry per cfg.daemonAgents
    #                              element, each WantedBy default.target
    #                              so home-manager actually symlinks
    #                              it from default.target.wants/;
    #                              omitted when `conexusDaemonAgentPackage`
    #                              is null.
    #
    # The Python-backed "agent-mcp@" / "agent-mcp-router" units (and
    # the `router.impl` option that picked between them and these
    # Rust ones) were retired once the Python source tree was deleted;
    # `conexus@`/`conexus-router` are now unconditional -- there is no
    # other implementation left for `Install.WantedBy` to pick between.
    systemd.user.services = {
      # `conexus@<name>.service` — the CoNexus Rust backend user
      # template, and the ONLY per-project backend implementation now
      # that the Python one (`agent-mcp@<name>.service`) was retired
      # together with the rest of the Python source tree.
      #
      # `RuntimeDirectory = "agent-mcp/%i"` (not `conexus/%i`) is a
      # historical holdover from when this path was shared with the
      # since-deleted `agent-mcp@` template so a `backend_impl` flip
      # was a same-path process swap (Decision #1, 2026-09-04,
      # operator) — kept as-is rather than renamed, since renaming now
      # would just be sock-path churn for every live deployment with
      # zero behavioural benefit. See `nix/module.nix`'s parallel
      # system-mode template for the fuller history.
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
          # RuntimeDirectoryPreserve=yes ported from nix/module.nix's
          # already-correct system-mode template -- without it, this
          # unit's own stop/restart is free to tear down its own leaf
          # of the shared %t/agent-mcp/ tree on every cycle rather than
          # just on uninstall (same class of bug as `conexus-router`'s
          # own RuntimeDirectoryPreserve incident below, one level up
          # the tree).
          RuntimeDirectoryPreserve = "yes";
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

      # `conexus-router` — the CoNexus Rust router, and the ONLY router
      # implementation now that the Python one
      # (`agent-mcp-router.service`) and the `router.impl` A/B flip
      # between them were retired together with the rest of the Python
      # source tree. Most of Python's env-var-only config surface is a
      # real CLI flag on `conexus-router` (see its own `Cli` struct doc
      # in rust/conexus-router/src/main.rs) -- passed as flags below
      # rather than duplicated as env vars; `AGENT_MCP_README_HTML`/
      # `AGENT_MCP_INSTALLER_TEMPLATE` are deliberately omitted --
      # `conexus-router` parses `--readme-html`/`--installer-template`
      # but doesn't consume them yet (the `client_config`/`installer`
      # routes they'd feed stay explicitly, permanently deferred per
      # the migration plan's own PR23-step-6 research finding), and
      # neither `readmeHtml` nor `installerTemplate` exist in
      # packages.nix any more (they only ever served the now-deleted
      # Python router's index page).
      #
      # `null` by default (see `conexusRouterPackage`'s own option doc)
      # -- omitted until the flake's `homeModules.default` wrapper
      # auto-wires it from `conexusPkgs.conexusRouterWrapper`, the same
      # pattern `conexusLauncherPackage` already uses one unit up.
      "conexus-router" = lib.mkIf (cfg.conexusRouterPackage != null) {
        Unit = {
          Description = "CoNexus router (URL-keyed, idle-stop)";
          After = [ "ollama.service" ];
        };
        Service = {
          Type = "simple";
          Environment = [
            # Router DB lives under XDG_DATA_HOME (default
            # ~/.local/share/agent-mcp/router.db) -- user-mode units
            # cannot write to conexus-router's own compiled-in
            # `/var/lib/agent-mcp/router.db` default (that path is the
            # NixOS system-mode module's user, not this one's).
            "AGENT_MCP_ROUTER_DB=${config.xdg.dataHome}/agent-mcp/router.db"
            # `--default-workspace` has no CLI-flag equivalent on
            # `conexus-router` (env-var-only, mirroring the retired
            # Python router's own `AGENT_MCP_DEFAULT_WORKSPACE` --
            # see rust/conexus-router/src/main.rs's own
            # `default_workspace_parent()` doc). Without this,
            # `conexus-router` falls back to `$HOME/.local/share/agent-mcp/projects`,
            # NOT `cfg.router.defaultWorkspaceParent` -- a real gap this
            # unit had from the day `conexus-router` was first wired in
            # here, masked while `router.impl` still defaulted to
            # "python" (whose own unit DID set this).
            "AGENT_MCP_DEFAULT_WORKSPACE=${cfg.router.defaultWorkspaceParent}"
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
          # RuntimeDirectoryPreserve=yes is NOT optional here (live
          # incident, 2026-09-07): `RuntimeDirectory=agent-mcp` is a
          # BARE, single-component value -- per systemd.exec(5)'s
          # RuntimeDirectory= section, that bare path IS this unit's
          # "innermost subdirectory", so on every stop (a crash-loop
          # restart, a redeploy) systemd rm's the ENTIRE %t/agent-mcp/
          # tree by default -- including every live per-project
          # `conexus@%i` subdirectory and UDS socket unrelated units
          # still own and are actively listening on. Confirmed live:
          # `conexus-router` restarting (during this very cutover's
          # home-manager switch, back when the retired Python router
          # unit could also crash-loop against the same port) wiped
          # %t/agent-mcp/ on each cycle, so `ss` kept showing
          # per-project sockets LISTEN (the kernel remembers the bind)
          # while every new connect() to the now-unlinked path failed --
          # indistinguishable from "backend not ready" at every layer
          # above this. `=yes` makes this unit's OWN stop leave the
          # tree alone, matching nix/module.nix's system-mode template
          # (which already carries the equivalent guard on ITS units).
          RuntimeDirectory = "agent-mcp";
          RuntimeDirectoryMode = "0700";
          RuntimeDirectoryPreserve = "yes";
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
          # Defense-in-depth ceiling on the SIGTERM → exit window,
          # ported from the retired Python router unit's identical
          # directive (see its own removed comment, and the 2026-06-04
          # 08:57 production stall it was added to guard against) --
          # kept at the same value so a router restart behaves
          # identically regardless of implementation.
          TimeoutStopSec = 15;
        } // hardening;
        # Unconditional now that this is the only router implementation
        # -- the `router.impl` A/B flip this WantedBy used to be
        # conditioned on was retired together with the Python router.
        Install.WantedBy = [ "default.target" ];
      };
    } // lib.listToAttrs (map (a: {
      name = "agent-mcp-daemon-agent@${daemonAgentInstanceName a}";
      # Omitted when `conexusDaemonAgentPackage` is null -- see that
      # option's own doc for why this mirrors `conexus@`/`conexus-router`'s
      # "no package, no unit" idiom rather than a no-default required
      # option.
      value = lib.mkIf (cfg.conexusDaemonAgentPackage != null) {
        Unit = {
          Description = "Agent-MCP daemon agent — ${daemonAgentInstanceName a} (event-driven wait_for_events loop)";
          After = [ "conexus-router.service" ];
          Wants = [ "conexus-router.service" ];
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
          # The Rust binary resolves its own token/URL/cursor paths
          # from the instance argument PLUS an explicit `--router-port`
          # flag (no per-build @router_port@ substitution needed, unlike
          # the retired Python wrapper it replaces).
          ExecStart = "${daemonAgentWrapper}/bin/conexus-daemon-agent --router-port ${toString cfg.router.port} ${daemonAgentInstanceName a}";
          Restart = "on-failure";
          RestartSec = 10;
        } // hardening;
        Install.WantedBy = [ "default.target" ];
      };
    }) cfg.daemonAgents);
  };
}
