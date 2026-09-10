{ pkgs
, src
, modulePkgs ? null
}:

# Eval-only harness for nix/home-manager-module.nix.
#
# NOT a `nix flake check` entry and not a VM: nothing here builds. It
# evaluates the home-manager module against a minimal stub of the
# home-manager options it touches, and exposes what actually ends up in
# the systemd units.
#
# Everything here is read back out of the module's own outputs — the
# systemd units and `home.packages` — rather than re-importing
# nix/packages.nix. A harness with its own copy of the import would
# happily keep passing while the module drifted away from it.
#
# Consumed by tests/test_nix_module_package_set.py. Run by hand with:
#
#   nix eval --impure --json --file nix/tests/eval-home-manager-module.nix \
#     --arg pkgs 'import <nixpkgs> {}' --arg src ./.

let
  lib = pkgs.lib;

  # The slice of home-manager's option surface this module writes to.
  # Deliberately minimal: a full home-manager eval would drag in the
  # whole module tree for no extra coverage of the thing under test.
  homeManagerStub = { lib, ... }: {
    options = {
      assertions = lib.mkOption {
        type = lib.types.listOf lib.types.unspecified;
        default = [ ];
      };
      warnings = lib.mkOption {
        type = lib.types.listOf lib.types.str;
        default = [ ];
      };
      home.packages = lib.mkOption {
        type = lib.types.listOf lib.types.package;
        default = [ ];
      };
      xdg.dataHome = lib.mkOption {
        type = lib.types.str;
        default = "/home/test/.local/share";
      };
      systemd.user.services = lib.mkOption {
        type = lib.types.attrsOf lib.types.unspecified;
        default = { };
      };
    };
  };

  evaluated = lib.evalModules {
    specialArgs = { inherit pkgs; };
    modules = [
      homeManagerStub
      ../home-manager-module.nix
      {
        services.agent-mcp = {
          enable = true;
          source = src;
          router = {
            externalUrl = "https://example.invalid";
            defaultWorkspaceParent = "/home/test/.local/share/agent-mcp/projects";
          };
          # Stub packages (never built -- this harness is eval-only) so
          # every unit that's `lib.mkIf (cfg.conexusXPackage != null)`
          # actually materializes for the coverage below; any
          # `nullOr package`-typed value satisfies the option,
          # `pkgs.hello` just needs a real `/bin/<name>` for ExecStart
          # string interpolation to resolve at eval time.
          conexusRouterPackage = pkgs.hello;
          conexusLauncherPackage = pkgs.hello;
          conexusDaemonAgentPackage = pkgs.hello;
          # One daemon-agent instance so the `agent-mcp-daemon-agent@`
          # template unit actually materializes (default `daemonAgents
          # = []` emits none). `tokenPath` is never read at eval time
          # (only interpolated into the wrapper's ExecStart string), so
          # a non-existent path is fine here.
          daemonAgents = [
            {
              project = "demo-proj";
              agentId = "worker-1";
              tokenPath = "/home/test/.config/agent-mcp/tokens/demo-proj--worker-1.token";
            }
          ];
        } // lib.optionalAttrs (modulePkgs != null) { pkgs = modulePkgs; };
      }
    ];
  };

  cfg = evaluated.config;
  units = cfg.systemd.user.services;

  # The derivations the module installs into the profile, keyed by the
  # binary name they provide.
  installed = lib.listToAttrs
    (map (p: lib.nameValuePair p.name p) cfg.home.packages);

in {
  # What systemd will actually exec. `conexus-router`/`conexus@` are
  # the ONLY router/backend implementation now that the Python one and
  # the `router.impl` A/B flip between them were retired.
  routerExecStart = units."conexus-router".Service.ExecStart;
  backendExecStart = units."conexus@".Service.ExecStart;
  routerEnvironment = units."conexus-router".Service.Environment;

  # RuntimeDirectoryPreserve regression guard (live incident,
  # 2026-09-07): `conexus-router` declares a BARE
  # `RuntimeDirectory = "agent-mcp"`, a strict parent of `conexus@`'s
  # own `agent-mcp/%i`. Per systemd.exec(5), that bare value IS the
  # router unit's own "innermost subdirectory", so without
  # `RuntimeDirectoryPreserve = "yes"` EVERY stop of the router unit
  # (a crash-loop, a redeploy) recursively removes the WHOLE
  # `%t/agent-mcp/` tree -- including every live per-project backend's
  # own subdirectory and UDS socket, unrelated units still own and are
  # actively listening on. See each unit's own inline comment in
  # ../home-manager-module.nix for the full incident writeup this test
  # pins against a regression of.
  runtimeDirectoryPreserve = {
    conexus-router = units."conexus-router".Service.RuntimeDirectoryPreserve or null;
    "conexus@" = units."conexus@".Service.RuntimeDirectoryPreserve or null;
  };

  # The daemon-agent template's `After`/`Wants` used to be
  # `router.impl`-aware (hardcoding `agent-mcp-router.service` unless
  # `impl == "rust"`) -- a live incident (2026-09-08) where every
  # daemon-agent activation unconditionally started the Python router
  # alongside an already-running `conexus-router`. Now that
  # `conexus-router` is the only router, this is unconditional; this
  # test pins that the dependency still points at the right unit name.
  daemonAgentRouterDependency = let
    unit = units."agent-mcp-daemon-agent@demo-proj--worker-1".Unit;
  in {
    after = unit.After;
    wants = unit.Wants;
  };

  # Store paths installed into the profile.
  homePackages = map (p: p.outPath) cfg.home.packages;

  # The derivations themselves, keyed by the binary they provide, for
  # callers that want to BUILD rather than inspect.
  drvs = installed;

  dashboardOut = cfg.services.agent-mcp.dashboard.package.outPath;
}
