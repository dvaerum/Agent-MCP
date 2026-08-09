{ pkgs
, src
, modulePkgs ? null
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
            externalUrl = "https://example.invalid";
            defaultWorkspaceParent = "/home/test/.local/share/agent-mcp/projects";
          };
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
