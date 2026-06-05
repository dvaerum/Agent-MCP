{
  description = "Agent-MCP — multi-agent coordination MCP server (packages, home-manager module, and NixOS VM for e2e tests)";

  # Pinning to nixos-unstable keeps the dashboard's Next.js 15 + Node
  # 22 toolchain available; the 25.05 / 25.11 releases also work.
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs, ... }:
    let
      system = "x86_64-linux";
      pkgs = import nixpkgs { inherit system; };
      lib = nixpkgs.lib;

      # ── Production package set (Phase 2) ─────────────────────────
      # The home-manager module's default package set. Mirrors the
      # nixos-developer-system deployment derivations 1:1; see
      # nix/packages.nix for the per-derivation rationale.
      productionPkgs = import ./nix/packages.nix {
        inherit pkgs lib;
        src = self;
      };

      # ── VM package set (older NixOS-module flake, kept for tests) ──
      # Re-usable builders for the Python package, the dashboard, the
      # router wrapper, etc. The two modes differ only in
      # assetPrefix; the dashboard derivation hard-bakes that prefix.
      mkVmPkgs = assetPrefix: import ./nix/package.nix {
        inherit pkgs lib assetPrefix;
        src = self;
      };
      vmPkgsMulti = mkVmPkgs "/agent-mcp/__dashboard";
      vmPkgsSingle = mkVmPkgs "";

      # NixOS VM builder. `mode` selects the systemd shape via
      # services.agent-mcp.mode in nix/vm.nix.
      mkVm = mode: (lib.nixosSystem {
        inherit system;
        # specialArgs are the only way to thread arbitrary attrs into
        # the module function signatures. _module.args works too but
        # is more verbose and would require declaring them as options.
        specialArgs = {
          src = self;
          inherit mode;
          autoProject = "e2e";
        };
        modules = [ ./nix/vm.nix ];
      }).config.system.build.vm;

      vmMulti = mkVm "multi";
      vmSingle = mkVm "single";

      # Wrapper script: parses flags, picks the right VM derivation,
      # bind-mounts the persist dir, launches qemu.
      runScript = pkgs.runCommand "agent-mcp-vm-run" {
        nativeBuildInputs = [ pkgs.makeWrapper ];
      } ''
        mkdir -p $out/bin
        substitute ${./nix/run-vm.sh} $out/bin/agent-mcp \
          --replace-fail "@VM_MULTI@" "${vmMulti}" \
          --replace-fail "@VM_SINGLE@" "${vmSingle}"
        chmod +x $out/bin/agent-mcp
        # Ensure qemu + coreutils are on PATH for the run-*-vm script.
        wrapProgram $out/bin/agent-mcp \
          --prefix PATH : ${lib.makeBinPath [ pkgs.qemu pkgs.coreutils pkgs.bash ]}
      '';
    in {
      # ── packages ────────────────────────────────────────────────
      # The three top-level packages the home-manager module consumes
      # (agent-mcp, agent-mcp-dashboard, agent-mcp-router-wrapper) plus
      # the legacy VM-flavoured packages and the qemu run script.
      packages.${system} = {
        # Phase 2 production set (consumed by the home-manager module).
        agent-mcp = productionPkgs.agentMcpPy;
        agent-mcp-dashboard = productionPkgs.agentMcpDashboard;
        agent-mcp-router-wrapper = productionPkgs.agentMcpRouterWrapper;
        default = productionPkgs.agentMcpPy;

        # Legacy VM/test packages. The dashboard variant with empty
        # assetPrefix supports the WIP single-tenant URL surface
        # (Phase 3 owns the toggle that selects between the two).
        agent-mcp-dashboard-single = vmPkgsSingle.agentMcpDashboard;
        agent-mcp-router = vmPkgsMulti.agentMcpRouter;
        vm = vmMulti;
        vm-multi = vmMulti;
        vm-single = vmSingle;
        vm-run = runScript;
      };

      apps.${system}.default = {
        type = "app";
        program = "${runScript}/bin/agent-mcp";
      };

      # ── home-manager module (Phase 2) ───────────────────────────
      # User-scope module exposing `services.agent-mcp.*` options.
      # See nix/README.md for the worked example.
      #
      # We wrap the bare module so that its `source` option defaults
      # to `self` — operators who import this flake's
      # `homeManagerModules.default` don't have to repeat the fork's
      # repo path themselves.
      homeManagerModules.default = { ... }: {
        imports = [ ./nix/home-manager-module.nix ];
        services.agent-mcp.source = lib.mkDefault self;
      };
      homeManagerModules.agent-mcp = self.homeManagerModules.default;

      # ── NixOS module (legacy, used by VM tests only) ────────────
      nixosModules.default = ./nix/module.nix;
      nixosModules.agent-mcp = ./nix/module.nix;

      # `nix flake check` smoke test. Two flavours:
      #
      #   - Build the three production derivations (agent-mcp,
      #     dashboard, router wrapper). Cheap; under a minute on a
      #     warm cache.
      #   - The two `pkgs.nixosTest` VM scaffolds — multi-tenant +
      #     single-tenant — added in Phase 3. First run is 10-15 min
      #     because the test driver builds a NixOS VM, but the result
      #     is cacheable and CI runners only pay it once per nixpkgs
      #     bump.
      checks.${system} = {
        agent-mcp = productionPkgs.agentMcpPy;
        agent-mcp-dashboard = productionPkgs.agentMcpDashboard;
        agent-mcp-router-wrapper = productionPkgs.agentMcpRouterWrapper;
        vm-multi-tenant = import ./nix/tests/multi-tenant.nix {
          inherit pkgs lib self;
        };
        vm-single-tenant = import ./nix/tests/single-tenant.nix {
          inherit pkgs lib self;
        };
        # Regression guard: the dashboard auto-terminate-idle-agents
        # loop fixed in v5.0.3. Boots the multi-tenant stack, plants
        # an "old idle" worker row, and proves the server does not
        # auto-terminate it across a 3-minute window without any
        # browser connected. See ./nix/tests/no-auto-cleanup.nix.
        vm-no-auto-cleanup = import ./nix/tests/no-auto-cleanup.nix {
          inherit pkgs lib self;
        };
        # PR-2 event-coord E2E: drives wait_for_events,
        # fetch_events_since, and the toggle-flip stop_listening path
        # via curl over the multi-tenant transport, exercising the
        # full server end-to-end without a browser. See
        # ./nix/tests/event-driven-coord.nix.
        vm-event-driven-coord = import ./nix/tests/event-driven-coord.nix {
          inherit pkgs lib self;
        };
      };
    };
}
