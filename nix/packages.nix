{ pkgs, lib, src }:

# Production package set for the agent-mcp home-manager module
# (Phase 2 of the router-upstream plan, prancy-napping-pie).
#
# Three top-level derivations:
#
#   agentMcpPy            — the Python application (buildPythonApplication).
#   agentMcpDashboard     — the Next.js static export, served by the router.
#   agentMcpRouterWrapper — thin writeShellScriptBin invoking
#                           `python -m agent_mcp.cli router`. The router
#                           source moved upstream in Phase 1a, so this
#                           is now a one-liner wrapper rather than a
#                           vendored script.
#
# Plus the supporting wrappers the home-manager module needs:
#
#   agentMcpBackendWrapper        — invokes `python -m agent_mcp.cli` for
#                                   the per-project backend (SSE transport).
#   agentMcpLauncher              — bash launcher invoked by the
#                                   agent-mcp@<name>.service template; resolves
#                                   <name> → workspace path via the router's
#                                   projects.local.json, then exec's the
#                                   backend with --uds.
#   agentMcpDaemonAgentRunner     — Python event-loop body (long-poll
#                                   wait_for_events, log each event,
#                                   persist cursor).
#   agentMcpDaemonAgentWrapper    — bash wrapper invoked by the
#                                   agent-mcp-daemon-agent@<instance>.service
#                                   template. Splits %i into
#                                   <project>--<agent_id>, reads bearer
#                                   from ~/.config/agent-mcp/tokens/,
#                                   exec's the Python runner.
#   agentMcpDaemonAgentPrecompactHook — Claude Code PreCompact hook for
#                                       daemon agents.
#   readmeHtml                    — CommonMark-rendered README used by the
#                                   router's index page.
#   installerTemplate             — path to the .mcp.json merge installer
#                                   template (installer.sh.in moved upstream
#                                   to agent_mcp/router/ in Phase 1a).
#
# Ports the derivations from
# nixos-developer-system/users/dennis/agent-mcp/default.nix verbatim,
# adjusting paths so the source comes from `src` (the fork) instead of
# the `agent-mcp` flake input.

let
  python = pkgs.python312;

  agentMcpPy = python.pkgs.buildPythonApplication {
    pname = "agent-mcp";
    # Read the version from pyproject.toml at evaluation time so a
    # version bump in pyproject doesn't need to be mirrored here.
    # The version field lives on a single `version = "X.Y.Z"` line.
    version =
      let
        py = builtins.readFile "${src}/pyproject.toml";
        m = builtins.match ".*\nversion = \"([^\"]+)\".*" py;
      in
        if m == null then "0.0.0-unknown" else builtins.head m;
    pyproject = true;
    inherit src;
    build-system = [ python.pkgs.setuptools ];
    dependencies = with python.pkgs; [
      anyio click openai fastapi starlette uvicorn jinja2
      python-dotenv sqlite-vec httpx mcp
      sqlalchemy alembic aiohttp requests
      # Router identity store (Phase 1 PR B, prancy-napping-pie).
      argon2-cffi
      # OIDC SSO client (Phase 3 Wave 3, prancy-napping-pie). Authlib
      # powers the authorization-code + PKCE flow and the id_token
      # signature validation on the /agent-mcp/sso/* routes.
      authlib
    ];
    # Upstream tests need a writable HOME, an OPENAI_API_KEY, and at
    # least one network-hitting fixture. Run them in CI, not here.
    doCheck = false;
  };

  # buildPythonApplication does not put the app's site-packages on
  # PYTHONPATH for subprocesses, so we splice it in manually.
  agentMcpPyPath =
    "${agentMcpPy}/${python.sitePackages}:"
    + "${python.pkgs.makePythonPath agentMcpPy.dependencies}";

  # ── Backend launcher ─────────────────────────────────────────────
  # Thin wrapper that exec's `python -m agent_mcp.cli server` via the
  # fork's CLI group. v5.0.53: the Ollama-shaped OpenAI-compatible
  # defaults (OPENAI_BASE_URL / OPENAI_API_KEY / OPENAI_MODEL /
  # AGENT_MCP_EMBEDDING_MODEL / AGENT_MCP_EMBEDDING_DIMENSION) are now
  # seeded by core.config when OPENAI_API_KEY is unset, so we no longer
  # set them here. Callers who want to point this backend at the
  # OpenAI cloud (or a different Ollama model) simply pre-export
  # OPENAI_API_KEY (or any of the other vars) via the systemd unit's
  # Environment list — core.config's `setdefault` honours pre-set
  # values.
  agentMcpBackendWrapper = pkgs.writeShellScriptBin "agent-mcp-backend" ''
    export PYTHONPATH="${agentMcpPyPath}''${PYTHONPATH:+:$PYTHONPATH}"
    exec ${python}/bin/python -m agent_mcp.cli server --transport sse "$@"
  '';

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
    # Version mirrors the Python package; bumping pyproject also
    # bumps the dashboard derivation in lockstep.
    version = agentMcpPy.version;
    src = "${src}/agent_mcp/dashboard";
    # Re-set whenever the dashboard's package-lock.json changes
    # upstream (rare). On hash mismatch, nix prints the correct
    # value; paste it here. Updated 2026-07-02 for the next
    # 15.3.4 -> 15.5.20 security bump + npm audit fix lockfile changes.
    npmDepsHash = "sha256-R1UVHjh4Vcc6Q6lzJy6Z+v9OYdX5OiwLd6r+0PDoPH8=";
    NEXT_PUBLIC_AUTO_CONNECT = "false";
    NEXT_PUBLIC_DEFAULT_SERVER_HOST = "";
    NEXT_PUBLIC_DEFAULT_SERVER_PORT = "";
    # Product version shown in the sidebar footer. Sourced from pyproject
    # (via agentMcpPy.version) so the sandboxed build — which can't see the
    # repo-root pyproject.toml — still bakes the right number. See
    # dashboard/next.config.ts resolveVersion().
    NEXT_PUBLIC_AGENT_MCP_VERSION = agentMcpPy.version;
    installPhase = ''
      runHook preInstall
      mkdir -p $out/share
      cp -r out $out/share/agent-mcp-dashboard
      runHook postInstall
    '';
    dontFixup = true;
  };

  # ── Router wrapper ────────────────────────────────────────────────
  # The router source moved upstream in Phase 1a (PR #84): it's now
  # at agent_mcp/router/ inside the Python package, invoked via the
  # `router` subcommand on the fork's CLI group. This wrapper just
  # sets PYTHONPATH and exec's it; the aiohttp runtime dep is pulled
  # in by agentMcpPy above.
  agentMcpRouterWrapper = pkgs.writeShellScriptBin "agent-mcp-router" ''
    export PYTHONPATH="${agentMcpPyPath}''${PYTHONPATH:+:$PYTHONPATH}"
    exec ${python}/bin/python -m agent_mcp.cli router "$@"
  '';

  # ── systemd template launcher ─────────────────────────────────────
  # The agent-mcp@<name>.service template invokes this with %i. The
  # launcher resolves <name> → project path by reading the single
  # CLI-managed JSON file, then exec's the backend wrapper with
  # `--uds <path>/.../backend.sock`.
  agentMcpLauncher = pkgs.writeShellScriptBin "agent-mcp-launcher" ''
    set -euo pipefail
    name="''${1:?usage: agent-mcp-launcher <instance>}"
    # Project-file location lookup order:
    #   1. AGENT_MCP_PROJECTS_FILE (explicit override; how the VM
    #      module and other system-mode deploys set it).
    #   2. $XDG_CONFIG_HOME/agent-mcp/projects.local.json (home-manager
    #      / user-mode deploys).
    #   3. $HOME/.config/agent-mcp/projects.local.json (fallback).
    # The AGENT_MCP_PROJECTS_FILE env var contract was the OLD
    # vendored launcher's behavior; PR #88 (router upstreaming)
    # dropped it, breaking VM e2e for system-mode deploys.
    # Restored 2026-06-16 after the regression surfaced via VM smoke.
    if [[ -n "''${AGENT_MCP_PROJECTS_FILE:-}" ]]; then
      loc_file="$AGENT_MCP_PROJECTS_FILE"
    else
      cfg_dir="''${XDG_CONFIG_HOME:-$HOME/.config}/agent-mcp"
      loc_file="$cfg_dir/projects.local.json"
    fi

    # The projects file has two valid shapes:
    #   * Legacy: {"<name>": "<workspace_path>"}
    #     (washing-brothers and any other pre-PR-1 entry).
    #   * Nested: {"<name>": {"workspace": "<path>", "aliases": [...]}}
    #     (anything written by agent_mcp.router.project_registry).
    # The jq below handles both: if the value is an object, extract
    # `.workspace`; otherwise use it as-is.
    path=""
    if [[ -r "$loc_file" ]]; then
      path="$(${pkgs.jq}/bin/jq -er --arg n "$name" '
        .[$n] | if type == "object" then .workspace else . end // empty
      ' "$loc_file" 2>/dev/null || true)"
    fi

    if [[ -z "$path" ]]; then
      echo "agent-mcp-launcher: unknown project '$name'" >&2
      echo "  searched: $loc_file" >&2
      exit 1
    fi
    if [[ ! -d "$path" ]]; then
      echo "agent-mcp-launcher: '$name' resolves to '$path' but that dir does not exist" >&2
      exit 1
    fi

    # Production deploys typically run with XDG_RUNTIME_DIR set
    # (/run/user/<uid>); the router and launcher both anchor their
    # socket paths there. When the deploy overrides this — e.g. the
    # VM test sets AGENT_MCP_SOCK_DIR=/run/agent-mcp and uses
    # systemd's RuntimeDirectory to materialise the dir — fall back
    # to AGENT_MCP_SOCK_DIR so launcher and router agree on the
    # sock path under both deployment shapes.
    sock_root="''${AGENT_MCP_SOCK_DIR:-''${XDG_RUNTIME_DIR}/agent-mcp}"
    sock="$sock_root/$name/backend.sock"
    # retire-system-token Wave 2/3: per-project HMAC key the router
    # signs the forwarding header with. F015 v4 moved key generation
    # from the router into the systemd unit's ExecStartPre (see
    # ``nix/module.nix`` — the ``agent-mcp@`` template); the router
    # only READS the file. See package.nix for the longer explanation.
    # Wave 3 deleted the parallel ``--system-token-out`` plumbing —
    # the forwarding HMAC is the only remaining router→backend auth
    # channel.
    forwarding_hmac_in="$sock_root/$name/forwarding_hmac"
    mkdir -p "$(dirname "$sock")"
    exec ${agentMcpBackendWrapper}/bin/agent-mcp-backend \
      --uds "$sock" \
      --project-dir "$path" \
      --forwarding-hmac-in "$forwarding_hmac_in" \
      --no-tui
  '';

  # ── Daemon-agent reference wiring ─────────────────────────────────
  # Per-agent always-on event loop. See docs/EVENT_DRIVEN_AGENT_LOOP.md
  # for the operator-facing story. Two scripts get substituted into
  # the store:
  #
  #   agentMcpDaemonAgentRunner   — Python event-loop body.
  #   agentMcpDaemonAgentWrapper  — Bash wrapper invoked by the systemd
  #                                 template.
  #
  # The Nix-substituted `.sh.in` files use `@var@` markers so the
  # store-path locations of pkgs.bash / pkgs.python312 / the runner
  # script / pkgs.curl / pkgs.jq are baked in at build time.

  agentMcpDaemonAgentRunner = pkgs.runCommand "agent-mcp-daemon-agent-runner.py" {} ''
    cp ${./agent-mcp-daemon-agent-runner.py} $out
    chmod +x $out
  '';

  agentMcpDaemonAgentWrapper = routerPort: pkgs.runCommand "agent-mcp-daemon-agent" {
    nativeBuildInputs = [ pkgs.makeWrapper ];
  } ''
    mkdir -p $out/bin
    substitute ${./agent-mcp-daemon-agent.sh.in} $out/bin/agent-mcp-daemon-agent \
      --replace-fail @bash@ ${pkgs.bash} \
      --replace-fail @python@ ${python} \
      --replace-fail @runner@ ${agentMcpDaemonAgentRunner} \
      --replace-fail @router_port@ ${toString routerPort}
    chmod +x $out/bin/agent-mcp-daemon-agent
  '';

  agentMcpDaemonAgentPrecompactHook = pkgs.runCommand "agent-mcp-daemon-agent-precompact-hook" {} ''
    mkdir -p $out/bin
    substitute ${./agent-mcp-daemon-agent-precompact-hook.sh.in} \
      $out/bin/agent-mcp-daemon-agent-precompact-hook \
      --replace-fail @bash@ ${pkgs.bash} \
      --replace-fail @curl@ ${pkgs.curl} \
      --replace-fail @jq@ ${pkgs.jq}
    chmod +x $out/bin/agent-mcp-daemon-agent-precompact-hook
  '';

  # ── README, rendered to HTML at build time ───────────────────────
  # Used by the router as the body of the index page's "How to use"
  # <details> block. cmark is a small CommonMark renderer; the
  # produced file is a fragment (no <html>/<body>), which is what
  # the router's _INDEX_STYLE block expects.
  readmeHtml = pkgs.runCommand "agent-mcp-readme.html" {
    nativeBuildInputs = [ pkgs.cmark ];
  } ''
    cmark --safe ${./README.md} > $out
  '';

  # installer.sh.in moved upstream to agent_mcp/router/ in Phase 1a
  # (PR #84). The router reads its path from
  # AGENT_MCP_INSTALLER_TEMPLATE; we point it at the upstreamed copy.
  installerTemplate = "${src}/agent_mcp/router/installer.sh.in";

in {
  inherit
    agentMcpPy
    agentMcpDashboard
    agentMcpRouterWrapper
    agentMcpBackendWrapper
    agentMcpLauncher
    agentMcpDaemonAgentRunner
    agentMcpDaemonAgentWrapper
    agentMcpDaemonAgentPrecompactHook
    readmeHtml
    installerTemplate;
}
