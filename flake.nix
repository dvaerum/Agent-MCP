{
  description = "Agent-MCP — multi-agent coordination MCP server (packages, home-manager module, and NixOS VM for e2e tests)";

  # Pinning to nixos-unstable keeps the dashboard's Next.js 15 + Node
  # 22 toolchain available; the 25.05 / 25.11 releases also work.
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  # Rust-Nix packaging for the CoNexus migration's rust/ workspace
  # (Phase D1 step 4, prancy-napping-pie). Chosen over the zero-input
  # `rustPlatform.buildRustPackage` per the operator's own call
  # (2026-09-04): finer-grained incremental build caching as the
  # workspace grows toward the full migration's eventual size. No
  # existing Rust-Nix precedent anywhere in this org's flakes to
  # follow — `aoe-bridge/` (this repo's other Rust crate) is built via
  # bare `cargo build`, not packaged by this flake at all.
  inputs.crane.url = "github:ipetkov/crane";

  outputs = { self, nixpkgs, crane, ... }:
    let
      system = "x86_64-linux";

      # No overlays, deliberately.
      #
      # This used to carry a `tenacityTestFix` overlay
      # (`python312.override { packageOverrides = … }`) that disabled
      # tenacity's timing-sensitive `test_sleeps`, because that test
      # flaked under CI load and reddened the Nix VM builds. Removed
      # 2026-08-08 together with the python312 pin (see
      # nix/packages.nix): a package-set override rehashes the package
      # it touches, so the resulting store path no longer matches what
      # Hydra published and tenacity had to build — and therefore
      # self-test — locally. The overlay was creating the very
      # condition it was added to fix.
      #
      # On the channel's DEFAULT python (pkgs.python3) tenacity
      # substitutes pre-built from cache.nixos.org, so its suite never
      # runs here at all. Don't re-add an overlay for a flaky test in
      # a package we merely consume; check first whether the package
      # is being needlessly rebuilt.
      pkgs = import nixpkgs {
        inherit system;
      };
      lib = nixpkgs.lib;

      # ── Production package set (Phase 2) ─────────────────────────
      # The home-manager module's default package set. Mirrors the
      # nixos-developer-system deployment derivations 1:1; see
      # nix/packages.nix for the per-derivation rationale.
      productionPkgs = import ./nix/packages.nix {
        inherit pkgs lib;
        src = self;
      };

      # ── CoNexus Rust packaging (Phase D1 step 4) ──────────────────
      # Parallel package set for the rust/ Cargo workspace; see
      # nix/conexus.nix for the per-derivation rationale.
      conexusPkgs = import ./nix/conexus.nix {
        inherit pkgs lib;
        src = self;
        craneLib = crane.mkLib pkgs;
      };

      # There is deliberately no second package set here.
      #
      # Until 2026-08-09 this file also imported `nix/package.nix`, a
      # near-copy of nix/packages.nix that built its own `agentMcpPy`
      # from a SEPARATELY MAINTAINED dependency list — and the two
      # drifted (see docs/learnings/duplication-drift.md). Nothing
      # imported that copy's Python app, backend wrapper, launcher or
      # readme: it reached the outside world only as two flake outputs
      # that were themselves dead — `agent-mcp-dashboard-single`
      # (byte-identical to `agent-mcp-dashboard` since Phase 4 made
      # `assetPrefix` a serve-time substitution) and `agent-mcp-router`
      # (a wrapper around the pre-upstream vendored `nix/router.py`,
      # superseded by `agent_mcp/router/`). Both were deleted with it.
      # `tests/test_nix_single_source_of_truth.py` keeps it that way.

      # NixOS VM builder. `nix/vm.nix` used to have a "single" bare-TCP
      # single-backend shape alongside "multi" (the router shape); the
      # former was retired with the rest of the Python implementation
      # (see nix/module.nix's own module-doc comment), so there is now
      # only one derivation. `mode` is still threaded through for
      # nix/vm.nix's own function-signature compatibility with
      # nix/vm-dev.nix, which imports the same file directly.
      vmMulti = (lib.nixosSystem {
        inherit system;
        # specialArgs are the only way to thread arbitrary attrs into
        # the module function signatures. _module.args works too but
        # is more verbose and would require declaring them as options.
        specialArgs = {
          src = self;
          mode = "multi";
          craneLib = crane.mkLib pkgs;
        };
        modules = [ ./nix/vm.nix ];
      }).config.system.build.vm;

      # Path B interactive sandbox VM (feat/agent-select-dropdown).
      # Same shape as vmMulti but with host:18080 → guest:1337
      # forwardPort and a first-boot seed dataset (Admin + one live
      # + one terminated worker) for dashboard E2E acceptance via
      # Firefox-MCP. See nix/vm-dev.nix for the rationale.
      vmDev = (lib.nixosSystem {
        inherit system;
        specialArgs = {
          src = self;
        };
        modules = [ ./nix/vm-dev.nix ];
      }).config.system.build.vm;

      # Wrapper script: bind-mounts the persist dir, launches qemu.
      runScript = pkgs.runCommand "agent-mcp-vm-run" {
        nativeBuildInputs = [ pkgs.makeWrapper ];
      } ''
        mkdir -p $out/bin
        substitute ${./nix/run-vm.sh} $out/bin/agent-mcp \
          --replace-fail "@VM_MULTI@" "${vmMulti}"
        chmod +x $out/bin/agent-mcp
        # Ensure qemu + coreutils are on PATH for the run-*-vm script.
        wrapProgram $out/bin/agent-mcp \
          --prefix PATH : ${lib.makeBinPath [ pkgs.qemu pkgs.coreutils pkgs.bash ]}
      '';

      # Path B sandbox runner — parallel to runScript above, but
      # points at the host:18080 vm-dev derivation. Distinct binary
      # name so a developer can `nix run .#vm-dev` without
      # conflicting with `nix run .#` (which targets vmMulti on
      # host:5454).
      runScriptDev = pkgs.runCommand "agent-mcp-vm-dev-run" {
        nativeBuildInputs = [ pkgs.makeWrapper ];
      } ''
        mkdir -p $out/bin
        substitute ${./nix/run-vm-dev.sh} $out/bin/agent-mcp-vm-dev \
          --replace-fail "@VM_DEV@" "${vmDev}"
        chmod +x $out/bin/agent-mcp-vm-dev
        wrapProgram $out/bin/agent-mcp-vm-dev \
          --prefix PATH : ${lib.makeBinPath [ pkgs.qemu pkgs.coreutils pkgs.bash ]}
      '';

      # `nix run .#capture-vm-dev-fixture -- <name>` — reproducible
      # generator for the vm-dev preload fixtures. Boots the ephemeral
      # vm-dev runner (substituted below), seeds a demo dataset via the
      # REST API, quiesces the SQLite DBs over the dev-mode SSH, and
      # writes nix/vm-dev/fixtures/<name>.tar.zst. See
      # nix/vm-dev/fixtures/README.md.
      captureFixture = pkgs.runCommand "agent-mcp-capture-vm-dev-fixture" {
        nativeBuildInputs = [ pkgs.makeWrapper ];
      } ''
        mkdir -p $out/bin
        substitute ${./nix/capture-vm-dev-fixture.sh} $out/bin/capture-vm-dev-fixture \
          --replace-fail "@VM_DEV_RUN@" "${runScriptDev}/bin/agent-mcp-vm-dev"
        chmod +x $out/bin/capture-vm-dev-fixture
        wrapProgram $out/bin/capture-vm-dev-fixture \
          --prefix PATH : ${lib.makeBinPath [
            pkgs.bash pkgs.coreutils pkgs.curl pkgs.python3
            pkgs.openssh pkgs.sshpass pkgs.zstd
          ]}
      '';
    in {
      # ── packages ────────────────────────────────────────────────
      # The Python `agent-mcp` / `agent-mcp-router-wrapper` outputs
      # (buildPythonApplication over the now-deleted `agent_mcp/app`+
      # `agent_mcp/router` tree) were retired together with the rest
      # of the Python source tree — `conexus-backend`/`conexus-router`
      # below are their sole replacements. `agent-mcp-dashboard` was
      # always independent of the Python tree and is unaffected.
      packages.${system} = {
        # Phase 2 production set (consumed by the home-manager module).
        agent-mcp-dashboard = productionPkgs.agentMcpDashboard;
        default = conexusPkgs.conexusBackend;

        # CoNexus Rust backend (Phase D1) — wired into the
        # `conexus@<name>.service` template via
        # `conexusLauncherPackage` (both live projects run it in
        # production, Phase D1 step 6). See nix/conexus.nix.
        conexus-backend = conexusPkgs.conexusBackend;
        conexus-launcher = conexusPkgs.conexusLauncher;

        # CoNexus Rust router — the sole router implementation now
        # that the Python one (`agent-mcp-router.service`) and the
        # `router.impl` A/B flip between them were retired. Wired into
        # `homeModules.default` via `conexusRouterPackage` below.
        conexus-router = conexusPkgs.conexusRouter;
        conexus-router-wrapper = conexusPkgs.conexusRouterWrapper;

        # CoNexus reference daemon-agent (Phase F) — auto-wired via
        # `conexusDaemonAgentPackage` in the home-manager module
        # wrapper above; exposed here too so it's independently
        # buildable/cacheable like every other CoNexus binary.
        conexus-daemon-agent = conexusPkgs.conexusDaemonAgent;
        conexus-daemon-agent-wrapper = conexusPkgs.conexusDaemonAgentWrapper;

        vm = vmMulti;
        vm-multi = vmMulti;
        vm-dev = vmDev;
        vm-run = runScript;
        vm-dev-run = runScriptDev;
        capture-vm-dev-fixture = captureFixture;
      };

      apps.${system} = {
        default = {
          type = "app";
          program = "${runScript}/bin/agent-mcp";
        };
        # `nix run .#vm-dev` — Path B interactive sandbox for
        # dashboard E2E (feat/agent-select-dropdown). Forwards
        # host:18080 → guest:1337 and seeds a tiny dataset on first
        # boot. See nix/vm-dev.nix + nix/run-vm-dev.sh.
        vm-dev = {
          type = "app";
          program = "${runScriptDev}/bin/agent-mcp-vm-dev";
        };
        # Reproducible generator for the vm-dev preload fixtures.
        capture-vm-dev-fixture = {
          type = "app";
          program = "${captureFixture}/bin/capture-vm-dev-fixture";
        };
      };

      # ── home-manager module (Phase 2) ───────────────────────────
      # User-scope module exposing `services.agent-mcp.*` options.
      # See nix/README.md for the worked example.
      #
      # `homeModules`, not the legacy `homeManagerModules` -- home-manager's
      # own flake settled on `homeModules` as the class-name-based output
      # convention (matching `nixosModules`/`darwinModules`; see
      # nix-community/home-manager#6392 proposing the opposite rename and
      # #6406 reverting it back to `homeModules`). Renamed 2026-09-06, no
      # backward-compat alias kept -- this flake's only known consumer
      # (the operator's own home-manager-config deploy repo) is updated in
      # the same change.
      #
      # We wrap the bare module so that its `source` option defaults
      # to `self` — operators who import this flake's
      # `homeModules.default` don't have to repeat the fork's
      # repo path themselves.
      # `config` is needed (not just `{ ... }:`) so `conexusLauncherPackage`
      # below can build against WHATEVER package set the consumer's
      # home-manager config resolves for `services.agent-mcp.pkgs`
      # (defaults to their own `pkgs`, but is itself overridable) --
      # never this flake's own fixed x86_64-linux `pkgs`, which would
      # silently be wrong for a consumer on a different system.
      homeModules.default = { config, ... }: let
        conexusPkgsFor = import ./nix/conexus.nix {
          pkgs = config.services.agent-mcp.pkgs;
          inherit lib;
          src = self;
          craneLib = crane.mkLib config.services.agent-mcp.pkgs;
        };
      in {
        imports = [ ./nix/home-manager-module.nix ];
        services.agent-mcp.source = lib.mkDefault self;
        services.agent-mcp.conexusLauncherPackage =
          lib.mkDefault conexusPkgsFor.conexusLauncher;
        # Auto-wired -- see this option's own doc in
        # home-manager-module.nix for why a daemon-agent instance
        # carries none of the router's former port-collision risk.
        services.agent-mcp.conexusDaemonAgentPackage =
          lib.mkDefault conexusPkgsFor.conexusDaemonAgentWrapper;
        # Auto-wired too, now that conexus-router is the ONLY router
        # implementation (the Python router it used to risk racing for
        # the port was retired together with `router.impl` -- see
        # `conexusRouterPackage`'s own doc in home-manager-module.nix
        # for the before/after reasoning).
        services.agent-mcp.conexusRouterPackage =
          lib.mkDefault conexusPkgsFor.conexusRouterWrapper;
      };
      homeModules.agent-mcp = self.homeModules.default;

      # ── NixOS module (legacy, used by VM tests only) ────────────
      nixosModules.default = ./nix/module.nix;
      nixosModules.agent-mcp = ./nix/module.nix;

      # `nix flake check` smoke test. Two flavours:
      #
      #   - Build the production derivations (dashboard + the CoNexus
      #     Rust binaries). Cheap; under a minute on a warm cache.
      #   - The `pkgs.nixosTest` VM scaffolds — multi-tenant,
      #     single-tenant, no-auto-cleanup, event-driven-coord — added
      #     across Phase 3+. First run is several minutes because the
      #     test driver builds a NixOS VM, but the result is cacheable
      #     and CI runners only pay it once per nixpkgs bump.
      checks.${system} = {
        agent-mcp-dashboard = productionPkgs.agentMcpDashboard;
        # CoNexus Rust backend (Phase D1 step 4) — cheap build-only
        # check (the Python `agent-mcp`/`agent-mcp-router-wrapper`
        # checks this used to sit alongside were retired with the
        # Python source tree). The CI-gated `conexus (Rust)` job
        # already covers fmt/clippy/test/audit for the crate sources
        # directly; this check additionally proves the flake's own
        # crane wiring builds.
        conexus-backend = conexusPkgs.conexusBackend;
        # CoNexus Rust router (Phase F packaging prerequisite) — same
        # cheap build-only rationale as conexus-backend above.
        conexus-router = conexusPkgs.conexusRouter;
        # CoNexus reference daemon-agent (Phase F) — same cheap
        # build-only rationale as conexus-backend/conexus-router above.
        conexus-daemon-agent = conexusPkgs.conexusDaemonAgent;
        vm-multi-tenant = import ./nix/tests/multi-tenant.nix {
          inherit pkgs lib self;
          craneLib = crane.mkLib pkgs;
        };
        vm-single-tenant = import ./nix/tests/single-tenant.nix {
          inherit pkgs lib self;
          craneLib = crane.mkLib pkgs;
        };
        # Regression guard: the dashboard auto-terminate-idle-agents
        # loop fixed in v5.0.3. Boots the multi-tenant stack, plants
        # an "old idle" worker row, and proves the server does not
        # auto-terminate it across a 3-minute window without any
        # browser connected. See ./nix/tests/no-auto-cleanup.nix.
        vm-no-auto-cleanup = import ./nix/tests/no-auto-cleanup.nix {
          inherit pkgs lib self;
          craneLib = crane.mkLib pkgs;
        };
        # PR-2 event-coord E2E: drives wait_for_events,
        # fetch_events_since, and the toggle-flip stop_listening path
        # via curl over the multi-tenant transport, exercising the
        # full server end-to-end without a browser. See
        # ./nix/tests/event-driven-coord.nix.
        vm-event-driven-coord = import ./nix/tests/event-driven-coord.nix {
          inherit pkgs lib self;
          craneLib = crane.mkLib pkgs;
        };
      };
    };
}
