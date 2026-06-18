{ pkgs
, lib
, src
  # Dashboard ASSET_PREFIX kept for callers that still pass it, but
  # ignored by the build itself since Phase 4 of prancy-napping-pie:
  # the dashboard's `next.config.ts` now defaults the assetPrefix to
  # a literal sentinel string and the router substitutes the runtime
  # prefix on serve. Keeping the parameter argument-shaped (rather
  # than removing it) avoids breaking external callers in-flight.
, assetPrefix ? "/agent-mcp/__dashboard"
}:

# Centralised builders for the agent-mcp pieces the VM needs.
# Mirrors what users/dennis/agent-mcp/default.nix in
# nixos-developer-system does, minus the home-manager bits (those
# move into nix/module.nix as NixOS services).

let
  python = pkgs.python312;

  agentMcpPy = python.pkgs.buildPythonApplication {
    pname = "agent-mcp";
    # pyproject.toml says 2.5.0; tag dev rather than chase upstream.
    version = "2.5.0-flake";
    pyproject = true;
    inherit src;
    build-system = [ python.pkgs.setuptools ];
    dependencies = with python.pkgs; [
      anyio click openai fastapi starlette uvicorn jinja2
      python-dotenv sqlite-vec httpx mcp
      sqlalchemy alembic
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

  # buildPythonApplication does not propagate site-packages on
  # PYTHONPATH for subprocesses, so we splice it in manually.
  agentMcpPyPath =
    "${agentMcpPy}/${python.sitePackages}:"
    + "${python.pkgs.makePythonPath agentMcpPy.dependencies}";

  # Backend launcher. Hard-codes the Ollama-shaped embedding endpoint
  # the VM provides on localhost:11434. If a future invocation wants
  # the OpenAI cloud, override OPENAI_BASE_URL/OPENAI_API_KEY in the
  # systemd unit's Environment list.
  agentMcpBackendWrapper = pkgs.writeShellScriptBin "agent-mcp-backend" ''
    export OPENAI_BASE_URL="''${OPENAI_BASE_URL:-http://127.0.0.1:11434/v1}"
    export OPENAI_API_KEY="''${OPENAI_API_KEY:-ollama}"
    # v5.0.44: completion_service.completion_client() requires
    # OPENAI_MODEL when OPENAI_API_KEY is set. Match the chat model
    # the VM ships via services.ollama.loadModels.
    export OPENAI_MODEL="''${OPENAI_MODEL:-qwen3:1.7b}"
    export AGENT_MCP_EMBEDDING_MODEL="''${AGENT_MCP_EMBEDDING_MODEL:-qwen3-embedding:0.6b}"
    export AGENT_MCP_EMBEDDING_DIMENSION="''${AGENT_MCP_EMBEDDING_DIMENSION:-1024}"
    export PYTHONPATH="${agentMcpPyPath}''${PYTHONPATH:+:$PYTHONPATH}"
    exec ${python}/bin/python -m agent_mcp.cli --transport sse "$@"
  '';

  agentMcpDashboard = pkgs.buildNpmPackage {
    pname = "agent-mcp-dashboard";
    version = "0.1.0";
    src = "${src}/agent_mcp/dashboard";
    # Intentionally no ASSET_PREFIX env (Phase 4): the build emits a
    # sentinel, the router substitutes at serve time. See nix/README.md
    # § "Asset prefix".
    # Re-set on package-lock.json drift. Nix prints the correct value
    # on hash mismatch; paste it back here. Lockfile shipped with the
    # repo as of 2026-05-31 hashes to:
    npmDepsHash = "sha256-VDyDHd90VNMIKLqSy/goQ7uj7d+2LkyS7cmYHGy8ojU=";
    NEXT_PUBLIC_AUTO_CONNECT = "false";
    NEXT_PUBLIC_DEFAULT_SERVER_HOST = "";
    NEXT_PUBLIC_DEFAULT_SERVER_PORT = "";
    installPhase = ''
      runHook preInstall
      mkdir -p $out/share
      cp -r out $out/share/agent-mcp-dashboard
      runHook postInstall
    '';
    dontFixup = true;
  };

  # Router script + aiohttp Python runtime, vendored from
  # nixos-developer-system. See nix/router.py header for the one
  # intentional patch (AGENT_MCP_SYSTEMCTL_MODE switch).
  agentMcpRouter = pkgs.writeShellScriptBin "agent-mcp-router" ''
    exec ${python.withPackages (ps: [ ps.aiohttp ])}/bin/python \
      ${./router.py} "$@"
  '';

  installerTemplate = ./installer.sh.in;

  # README rendered to HTML fragment for the router's index page.
  # The router gracefully omits the panel if AGENT_MCP_README_HTML
  # points to an empty/missing file, so this is best-effort.
  readmeHtml = pkgs.runCommand "agent-mcp-vm-readme.html" {
    nativeBuildInputs = [ pkgs.cmark ];
  } ''
    cmark --safe ${../README.md} > $out
  '';

  # Template launcher invoked by the agent-mcp@<name>.service unit.
  # Reads the same projects.local.json file the router maintains via
  # __create / __unregister.
  agentMcpLauncher = pkgs.writeShellScriptBin "agent-mcp-launcher" ''
    set -euo pipefail
    name="''${1:?usage: agent-mcp-launcher <instance>}"
    loc_file="''${AGENT_MCP_PROJECTS_FILE:-/var/lib/agent-mcp/projects.local.json}"

    path=""
    if [[ -r "$loc_file" ]]; then
      path="$(${pkgs.jq}/bin/jq -er --arg n "$name" '.[$n] // empty' "$loc_file" 2>/dev/null || true)"
    fi
    if [[ -z "$path" ]]; then
      echo "agent-mcp-launcher: unknown project '$name' (file: $loc_file)" >&2
      exit 1
    fi
    if [[ ! -d "$path" ]]; then
      echo "agent-mcp-launcher: '$name' resolves to '$path' but that dir does not exist" >&2
      exit 1
    fi

    sock_dir="''${AGENT_MCP_SOCK_DIR:-/run/agent-mcp}/$name"
    mkdir -p "$sock_dir"
    sock="$sock_dir/backend.sock"
    rm -f "$sock"
    exec ${agentMcpBackendWrapper}/bin/agent-mcp-backend \
      --uds "$sock" \
      --project-dir "$path" \
      --no-tui
  '';

in {
  inherit
    agentMcpPy
    agentMcpBackendWrapper
    agentMcpDashboard
    agentMcpRouter
    agentMcpLauncher
    installerTemplate
    readmeHtml;
}
