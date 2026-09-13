{ pkgs, lib, src }:

# Production package set for the agent-mcp home-manager module.
#
# The Python implementation (`agent_mcp/app`, `agent_mcp/router`,
# `agent_mcp/cli.py`, etc.) was deleted wholesale once the Rust
# `rust/` workspace (conexus-backend / conexus-router / conexus-
# daemon-agent, packaged separately in `nix/conexus.nix`) reached
# functional completeness and became what actually runs in
# production. This file used to also build `agentMcpPy` and the
# Python-coupled wrappers around it (`agentMcpBackendWrapper`,
# `agentMcpRouterWrapper`, `agentMcpLauncher`, the daemon-agent
# runner/wrapper pair) plus two Python-router-only assets
# (`readmeHtml`, `installerTemplate`) -- all retired together with
# the Python source tree itself. See `nix/conexus.nix` for their
# Rust replacements and `nix/home-manager-module.nix` for how the
# systemd units now consume them unconditionally (no more
# `router.impl` A/B flip).
#
# What's left here:
#
#   agentMcpDashboard                 — the Next.js static export,
#                                        served by the router. Always
#                                        was independent of the Python
#                                        tree (buildNpmPackage over
#                                        agent_mcp/dashboard/, which
#                                        this repo's Python deletion
#                                        never touched).
#   agentMcpDaemonAgentPrecompactHook — Claude Code PreCompact hook
#                                       for daemon agents. A standalone
#                                       bash script substituted via
#                                       `runCommand`; never depended on
#                                       the Python interpreter or tree.

let
  # ── Dashboard version ─────────────────────────────────────────────
  # Read from the dashboard's own package.json at evaluation time so a
  # version bump there doesn't need a mirror edit here. This used to
  # read pyproject.toml's `version = "X.Y.Z"` (the single source of
  # truth while the Python implementation existed); package.json is
  # the direct replacement now that Python packaging is gone entirely
  # (Phase F: prancy-napping-pie) -- same "one file, everything else
  # derives" shape, just relocated to the one manifest this package
  # set still has.
  version = (builtins.fromJSON (builtins.readFile "${src}/agent_mcp/dashboard/package.json")).version;

  # ── Dashboard static export ──────────────────────────────────────
  # Next.js 15 project with `output: 'export'`. The router serves the
  # `out/` directory at /agent-mcp/__dashboard/.
  #
  # Phase 4 (prancy-napping-pie): we deliberately do NOT set
  # `ASSET_PREFIX` here. The dashboard's `next.config.ts` now defaults
  # the assetPrefix to a literal sentinel string
  # (`__AGENT_MCP_ASSET_PREFIX__`); the router substitutes the
  # configured runtime prefix on serve. One build artifact serves
  # every deployment URL — no rebuild needed when the operator points
  # the router at a different prefix.
  agentMcpDashboard = pkgs.buildNpmPackage {
    pname = "agent-mcp-dashboard";
    inherit version;
    src = "${src}/agent_mcp/dashboard";
    # Re-set whenever the dashboard's package-lock.json changes
    # upstream (rare). On hash mismatch, nix prints the correct
    # value; paste it here. Updated 2026-09-08: next 15.5.23 -> 15.5.25
    # + npm audit fix (dashboard-npm-audit-critical-rce security fix).
    npmDepsHash = "sha256-EPA1am+uB/3zVzcBV0lmOj62uFLyH9hwoahK/l3o210=";
    NEXT_PUBLIC_AUTO_CONNECT = "false";
    NEXT_PUBLIC_DEFAULT_SERVER_HOST = "";
    NEXT_PUBLIC_DEFAULT_SERVER_PORT = "";
    # Product version shown in the sidebar footer. Sourced from the
    # dashboard's own package.json (via the `version` let-binding
    # above) so this build's baked-in number always matches what a
    # plain `npm run build` from the dashboard dir would also read.
    # See dashboard/next.config.ts resolveVersion().
    NEXT_PUBLIC_AGENT_MCP_VERSION = version;
    installPhase = ''
      runHook preInstall
      mkdir -p $out/share
      cp -r out $out/share/agent-mcp-dashboard
      runHook postInstall
    '';
    dontFixup = true;
  };

  agentMcpDaemonAgentPrecompactHook = pkgs.runCommand "agent-mcp-daemon-agent-precompact-hook" {} ''
    mkdir -p $out/bin
    substitute ${./agent-mcp-daemon-agent-precompact-hook.sh.in} \
      $out/bin/agent-mcp-daemon-agent-precompact-hook \
      --replace-fail @bash@ ${pkgs.bash} \
      --replace-fail @curl@ ${pkgs.curl} \
      --replace-fail @jq@ ${pkgs.jq}
    chmod +x $out/bin/agent-mcp-daemon-agent-precompact-hook
  '';

in {
  inherit
    agentMcpDashboard
    agentMcpDaemonAgentPrecompactHook;
}
