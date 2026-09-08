{ pkgs
, src
, modulePkgs ? null
, routerImpl ? "python"
}:

# Eval-only harness for nix/home-manager-module.nix.
#
# NOT a `nix flake check` entry and not a VM: nothing here builds. It
# evaluates the home-manager module against a minimal stub of the
# home-manager options it touches, and exposes what actually ends up in
# the systemd units — including the *text* of the generated shell
# wrappers, which `writeShellScriptBin` makes readable at eval time.
#
# That last part is the point. The `services.agent-mcp.package` defect
# was invisible to any test that only compared the overridden
# attribute: the attribute changed, and the wrappers the units exec did
# not. Reading the wrapper text closes that gap — it shows the
# interpreter path and the PYTHONPATH (every dependency's store path,
# aiohttp included) the router will actually run with, so an override
# that stops at the attribute cannot pass.
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
            impl = routerImpl;
            externalUrl = "https://example.invalid";
            defaultWorkspaceParent = "/home/test/.local/share/agent-mcp/projects";
          };
          # Stub packages (never built -- this harness is eval-only) to
          # bring the `conexus-router`/`conexus@` units into existence
          # for RuntimeDirectoryPreserve coverage below; any `nullOr
          # package`-typed value satisfies the option, `pkgs.hello`
          # just needs a real `/bin/<name>` for ExecStart string
          # interpolation to resolve at eval time.
          conexusRouterPackage = pkgs.hello;
          conexusLauncherPackage = pkgs.hello;
          # One daemon-agent instance so the `agent-mcp-daemon-agent@`
          # template unit actually materializes (default `daemonAgents
          # = []` emits none) -- needed for the router.impl-aware
          # After/Wants coverage below. `tokenPath` is never read at
          # eval time (only interpolated into the wrapper's ExecStart
          # string), so a non-existent path is fine here.
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
  # What systemd will actually exec.
  routerExecStart = units."agent-mcp-router".Service.ExecStart;
  backendExecStart = units."agent-mcp@".Service.ExecStart;
  routerEnvironment = units."agent-mcp-router".Service.Environment;

  # RuntimeDirectoryPreserve regression guard (live incident,
  # 2026-09-07): `agent-mcp-router`/`conexus-router` declare a BARE
  # `RuntimeDirectory = "agent-mcp"`, a strict parent of the per-project
  # templates' `agent-mcp/%i`. Per systemd.exec(5), that bare value IS
  # the router unit's own "innermost subdirectory", so without
  # `RuntimeDirectoryPreserve = "yes"` EVERY stop of EITHER router unit
  # (a crash-loop, a redeploy, an A/B `router.impl` flip) recursively
  # removes the WHOLE `%t/agent-mcp/` tree -- including every live
  # per-project backend's own subdirectory and UDS socket, unrelated
  # units still own and are actively listening on. See each unit's own
  # inline comment in ../home-manager-module.nix for the full incident
  # writeup this test pins against a regression of.
  runtimeDirectoryPreserve = {
    agent-mcp-router = units."agent-mcp-router".Service.RuntimeDirectoryPreserve or null;
    conexus-router = units."conexus-router".Service.RuntimeDirectoryPreserve or null;
    "agent-mcp@" = units."agent-mcp@".Service.RuntimeDirectoryPreserve or null;
    "conexus@" = units."conexus@".Service.RuntimeDirectoryPreserve or null;
  };

  # `router.impl`-aware daemon-agent unit dependency (live incident,
  # 2026-09-08): the `agent-mcp-daemon-agent@` template's own `After`/
  # `Wants` used to hardcode `agent-mcp-router.service` regardless of
  # `router.impl`, so every daemon-agent activation unconditionally
  # started the Python router alongside an already-running
  # `conexus-router` -- a real, recurring crash-loop this test pins a
  # regression of. Reads back whichever router unit the ONE seeded
  # daemon-agent instance (`demo-proj--worker-1`) actually depends on.
  daemonAgentRouterDependency = let
    unit = units."agent-mcp-daemon-agent@demo-proj--worker-1".Unit;
  in {
    after = unit.After;
    wants = unit.Wants;
  };

  # Store paths installed into the profile.
  homePackages = map (p: p.outPath) cfg.home.packages;

  # The derivations themselves, keyed by the binary they provide, for
  # callers that want to BUILD rather than inspect — e.g. to show what a
  # `services.agent-mcp.pkgs` override actually puts in the closure:
  #
  #   nix build --impure --file … drvs.agent-mcp-router
  #   nix path-info -r ./result | grep aiohttp
  drvs = installed;

  # The generated wrapper scripts, verbatim.
  routerWrapperText = installed."agent-mcp-router".text;
  backendWrapperText = installed."agent-mcp-backend".text;
  launcherText = installed."agent-mcp-launcher".text;

  # Not a writeShellScriptBin (it is a substitute() runCommand), so
  # only its path is observable without building.
  daemonAgentWrapperOut = installed."agent-mcp-daemon-agent".outPath;

  dashboardOut = cfg.services.agent-mcp.dashboard.package.outPath;

  # Convenience for the operator-facing demonstration: the versions the
  # resolved set puts in the closure. The wrapper text above is the
  # authority; these just save a regex.
  pythonVersion = cfg.services.agent-mcp.pkgs.python3.version;
  aiohttpVersion = cfg.services.agent-mcp.pkgs.python3.pkgs.aiohttp.version;
}
