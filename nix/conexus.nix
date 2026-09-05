{ pkgs, lib, src, craneLib }:

# Rust-Nix packaging for the CoNexus migration (Phase D1 step 4,
# prancy-napping-pie). Parallel to nix/packages.nix's Python
# derivations, but for the rust/ Cargo workspace — kept in its own
# file rather than folded into packages.nix so the crane wiring (a
# genuinely new build toolchain for this repo) stays easy to find and
# doesn't tangle with the Python derivation list.
#
# One top-level derivation so far:
#
#   conexusBackend   — the compiled `conexus-backend` binary
#                       (rust/conexus-backend), built via
#                       craneLib.buildPackage against the whole
#                       workspace (rusqlite's bundled sqlite3 needs a
#                       C compiler on PATH, which crane's default
#                       stdenv already provides).
#
# Plus the systemd-template launcher the `conexus@<name>.service` unit
# (Phase D1 step 5) will invoke:
#
#   conexusLauncher  — bash launcher, structurally identical to
#                       nix/packages.nix's `agentMcpLauncher` (same
#                       project-registry lookup, same sock-path
#                       formula) so a `backend_impl` flip between
#                       `agent-mcp@<name>` and `conexus@<name>` is a
#                       same-path process swap (Phase D1 decision #1)
#                       — exec's `conexus-backend` instead of the
#                       Python wrapper.

let
  # rusqlite's "bundled" feature compiles sqlite3.c itself; crane's
  # default `buildPackage` stdenv already has a C compiler, so no
  # extra nativeBuildInputs are needed for that alone. `pkg-config` +
  # `openssl` are common `buildPackage` extras this workspace does NOT
  # currently need (no TLS client in conexus-backend) — added here
  # only if/when a later Phase D module needs them, not speculatively.
  commonArgs = {
    src = craneLib.cleanCargoSource "${src}/rust";
    strictDeps = true;
    # conexus-tools/src/prompts.rs reaches out of the rust/ workspace
    # via `include_str!("../../../agent_mcp/prompts/catalog.json")` (a
    # deliberate, temporary cross-language coupling per the migration
    # plan, retired only in Phase F) — cleanCargoSource above scopes
    # the build to rust/ alone, so that sibling file is missing from
    # the sandbox unless copied in at the same relative path the
    # include_str! resolves against (one directory above $sourceRoot,
    # matching rust/ and agent_mcp/ being siblings in a real checkout).
    postUnpack = ''
      mkdir -p "$sourceRoot/../agent_mcp/prompts"
      cp ${src}/agent_mcp/prompts/catalog.json "$sourceRoot/../agent_mcp/prompts/catalog.json"
    '';
    # rust/Cargo.toml is a virtual workspace manifest (no [package]
    # section of its own — see the crate list in rust/Cargo.toml), so
    # crane can't infer a name/version from it the way it can for a
    # single-crate repo; set them explicitly rather than let crane
    # fall back to a placeholder with an evaluation warning on every
    # build.
    pname = "conexus";
    version = "0.0.1";
  };

  # Two-phase crane build: `cargoArtifacts` compiles just the
  # dependency graph (cached, keyed on Cargo.lock) so an app-only code
  # change doesn't force a full dependency rebuild — the whole reason
  # this repo picked crane over `rustPlatform.buildRustPackage`.
  cargoArtifacts = craneLib.buildDepsOnly commonArgs;

  conexusBackend = craneLib.buildPackage (commonArgs // {
    inherit cargoArtifacts;
    pname = "conexus-backend";
    # cargoExtraArgs scopes the build to the one binary this flake
    # exposes today — conexus-tools/conexus-auth/conexus-db/
    # conexus-core/conexus-vec are libraries with no standalone
    # artifact of their own to install.
    cargoExtraArgs = "-p conexus-backend";
    doCheck = false;
  });

  # ── systemd template launcher ─────────────────────────────────────
  # Mirrors nix/packages.nix's `agentMcpLauncher` line-for-line for
  # the project-registry lookup + sock-path formula (Phase D1 decision
  # #1: same RuntimeDirectory/socket path as `agent-mcp@<name>`, so a
  # `backend_impl` flip is a same-path process swap) — only the final
  # `exec` target differs.
  conexusLauncher = pkgs.writeShellScriptBin "conexus-launcher" ''
    set -euo pipefail
    name="''${1:?usage: conexus-launcher <instance>}"
    if [[ -n "''${AGENT_MCP_PROJECTS_FILE:-}" ]]; then
      loc_file="$AGENT_MCP_PROJECTS_FILE"
    else
      cfg_dir="''${XDG_CONFIG_HOME:-$HOME/.config}/agent-mcp"
      loc_file="$cfg_dir/projects.local.json"
    fi

    path=""
    if [[ -r "$loc_file" ]]; then
      path="$(${pkgs.jq}/bin/jq -er --arg n "$name" '
        .[$n] | if type == "object" then .workspace else . end // empty
      ' "$loc_file" 2>/dev/null || true)"
    fi

    if [[ -z "$path" ]]; then
      echo "conexus-launcher: unknown project '$name'" >&2
      echo "  searched: $loc_file" >&2
      exit 1
    fi
    if [[ ! -d "$path" ]]; then
      echo "conexus-launcher: '$name' resolves to '$path' but that dir does not exist" >&2
      exit 1
    fi

    sock_root="''${AGENT_MCP_SOCK_DIR:-''${XDG_RUNTIME_DIR}/agent-mcp}"
    sock="$sock_root/$name/backend.sock"
    forwarding_hmac_in="$sock_root/$name/forwarding_hmac"
    mkdir -p "$(dirname "$sock")"
    exec ${conexusBackend}/bin/conexus-backend \
      --uds "$sock" \
      --project-dir "$path" \
      --forwarding-hmac-in "$forwarding_hmac_in" \
      --no-tui \
      --transport sse
  '';

in {
  inherit conexusBackend conexusLauncher;
}
