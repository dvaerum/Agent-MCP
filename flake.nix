{
  description = "Agent-MCP — multi-agent coordination MCP server (NixOS VM for e2e tests)";

  # Pinning to nixos-unstable keeps the dashboard's Next.js 15 + Node
  # 22 toolchain available; the 24.11 / 25.05 releases also work.
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs, ... }:
    let
      system = "x86_64-linux";
      pkgs = import nixpkgs { inherit system; };
      lib = nixpkgs.lib;

      # Re-usable builders for the Python package, the dashboard, the
      # router wrapper, etc. The two modes differ only in
      # assetPrefix; the dashboard derivation hard-bakes that prefix.
      mkPkgs = assetPrefix: import ./nix/package.nix {
        inherit pkgs lib assetPrefix;
        src = self;
      };
      pkgsMulti = mkPkgs "/agent-mcp/__dashboard";
      pkgsSingle = mkPkgs "";

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
      packages.${system} = {
        agent-mcp = pkgsMulti.agentMcpPy;
        agent-mcp-dashboard = pkgsMulti.agentMcpDashboard;
        agent-mcp-dashboard-single = pkgsSingle.agentMcpDashboard;
        agent-mcp-router = pkgsMulti.agentMcpRouter;
        vm = vmMulti;
        vm-multi = vmMulti;
        vm-single = vmSingle;
        default = runScript;
      };

      apps.${system}.default = {
        type = "app";
        program = "${runScript}/bin/agent-mcp";
      };

      nixosModules.default = ./nix/module.nix;
      nixosModules.agent-mcp = ./nix/module.nix;

      # `nix flake check` smoke test: just build the python package
      # and the dashboard. Building the full VM is too heavy for CI.
      checks.${system} = {
        agent-mcp = pkgsMulti.agentMcpPy;
        agent-mcp-dashboard = pkgsMulti.agentMcpDashboard;
      };
    };
}
