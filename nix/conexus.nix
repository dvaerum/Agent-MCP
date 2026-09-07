{ pkgs, lib, src, craneLib }:

# Rust-Nix packaging for the CoNexus migration (Phase D1 step 4,
# prancy-napping-pie). Parallel to nix/packages.nix's Python
# derivations, but for the rust/ Cargo workspace — kept in its own
# file rather than folded into packages.nix so the crane wiring (a
# genuinely new build toolchain for this repo) stays easy to find and
# doesn't tangle with the Python derivation list.
#
# Top-level derivations:
#
#   conexusBackend   — the compiled `conexus-backend` binary
#                       (rust/conexus-backend), built via
#                       craneLib.buildPackage against the whole
#                       workspace (rusqlite's bundled sqlite3 needs a
#                       C compiler on PATH, which crane's default
#                       stdenv already provides).
#
#   conexusRouter    — the compiled `conexus-router` binary (Phase F,
#                       "safe to start now" packaging slice per the
#                       Phase F research pass, prancy-napping-pie).
#                       Mirrors conexusBackend's own buildPackage
#                       shape exactly. NOT yet wired into any
#                       home-manager option that would make it live in
#                       production — see conexusRouterWrapper below.
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
#
# And a thin wrapper for the router binary, mirroring
# nix/packages.nix's `agentMcpRouterWrapper` shape (a one-liner exec,
# not a per-instance resolver like conexusLauncher above — the router
# is a singleton reading its own CLI flags/env vars directly, with no
# `<name>` positional argument to resolve):
#
#   conexusRouterWrapper — thin writeShellScriptBin invoking
#                       `conexus-router "$@"`. Not yet consumed by any
#                       systemd unit — deploying it to replace
#                       `agent-mcp-router.service` is Phase F's
#                       genuine operator-authority cutover decision,
#                       tracked separately from this packaging work.
#
#   conexusDaemonAgent      — the compiled `conexus-daemon-agent`
#                       binary (rust/conexus-daemon-agent): the
#                       reference daemon-agent event loop, ported for
#                       implementation-language consistency (operator
#                       decision, 2026-09-06) — it's a pure MCP
#                       wire-protocol client with no functional
#                       dependency on which backend/router it talks
#                       to. Replaces the Python pair
#                       (agentMcpDaemonAgentRunner +
#                       agentMcpDaemonAgentWrapper in packages.nix)
#                       as ONE binary — no separate bash wrapper is
#                       needed since the Rust binary resolves its own
#                       token/URL/cursor paths directly.
#   conexusDaemonAgentWrapper — thin writeShellScriptBin invoking
#                       `conexus-daemon-agent "$@"`, same rationale as
#                       conexusRouterWrapper above.

let
  # rusqlite's "bundled" feature compiles sqlite3.c itself; crane's
  # default `buildPackage` stdenv already has a C compiler, so no
  # extra nativeBuildInputs are needed for that alone. `pkg-config` +
  # `openssl` are common `buildPackage` extras this workspace does NOT
  # currently need (no TLS client in conexus-backend) — added here
  # only if/when a later Phase D module needs them, not speculatively.
  commonArgs = {
    # `craneLib.cleanCargoSource` filters the tree down to `.rs`/
    # `.toml`/`Cargo.lock` only (see crane's own `filterCargoSources.nix`
    # -- an unconditional extension allowlist, unrelated to git-tracking
    # or location) -- so the two non-Rust asset directories genuinely
    # embedded via `include_str!` (`conexus-router/templates/*.html`,
    # `conexus-tools/prompts/catalog.json`, Phase F deletion-prep step 1)
    # would be silently stripped even now that they live inside `rust/`
    # itself. This is what the OLD cross-repo `postUnpack` hack (PR
    # #850/#925) was really working around -- moving the files here
    # alone doesn't fix it, since crane's filter runs on file extension
    # everywhere, not just outside the workspace. Union crane's own
    # filter with an explicit include for those two directories instead
    # of re-adding a postUnpack copy step.
    src = lib.cleanSourceWith {
      src = lib.cleanSource "${src}/rust";
      filter = path: type:
        craneLib.filterCargoSources path type
        || lib.hasInfix "/conexus-router/templates/" path
        || lib.hasInfix "/conexus-tools/prompts/" path;
    };
    strictDeps = true;
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

  conexusRouter = craneLib.buildPackage (commonArgs // {
    inherit cargoArtifacts;
    pname = "conexus-router";
    # `conexus-router` deliberately depends on conexus-auth/conexus-core
    # only (ADR-0020, enforced at Cargo.toml level) — scoping the build
    # here is the same "-p <binary crate>" pattern as conexusBackend
    # above, sharing the SAME cargoArtifacts (the two binaries pull
    # from the same workspace dependency graph, so this doesn't force
    # a second full dependency build).
    cargoExtraArgs = "-p conexus-router";
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

  # ── router wrapper ────────────────────────────────────────────────
  # Mirrors nix/packages.nix's `agentMcpRouterWrapper` shape: a bare
  # exec, no PYTHONPATH/interpreter setup needed since this is a
  # native binary, not a Python entry point. `conexus-router` resolves
  # every flag/env var itself (see rust/conexus-router/src/main.rs's
  # own `Cli` struct doc) — this wrapper exists only so the systemd
  # unit's `ExecStart` names a stable package output rather than
  # reaching into the Cargo build's own `bin/` layout directly, same
  # rationale as the Python wrapper it mirrors.
  conexusRouterWrapper = pkgs.writeShellScriptBin "conexus-router" ''
    exec ${conexusRouter}/bin/conexus-router "$@"
  '';

  conexusDaemonAgent = craneLib.buildPackage (commonArgs // {
    inherit cargoArtifacts;
    pname = "conexus-daemon-agent";
    cargoExtraArgs = "-p conexus-daemon-agent";
    doCheck = false;
  });

  # ── daemon-agent wrapper ───────────────────────────────────────────
  # Same rationale as conexusRouterWrapper above: a bare exec, no
  # per-instance path resolution needed in shell since
  # `conexus-daemon-agent` resolves its own token/cursor paths
  # directly from the instance argument (see rust/conexus-daemon-agent/
  # src/main.rs) — unlike the Python wrapper it replaces, which had to
  # do that resolution in bash before exec'ing the interpreter.
  conexusDaemonAgentWrapper = pkgs.writeShellScriptBin "conexus-daemon-agent" ''
    exec ${conexusDaemonAgent}/bin/conexus-daemon-agent "$@"
  '';

in {
  inherit conexusBackend conexusLauncher conexusRouter conexusRouterWrapper
    conexusDaemonAgent conexusDaemonAgentWrapper;
}
