#!/usr/bin/env bash
# Regression guard: the home-manager module's router service must
# declare every env var / CLI flag the router needs to run under a
# non-root user.
#
# Background: from at least 2026-06-23 onward, real home-manager deploys
# hit a restart-loop on the (then-Python) router service because
# `nix/home-manager-module.nix` did not set `AGENT_MCP_ROUTER_DB`.
# The Python default was `/var/lib/agent-mcp/router.db`; that path is
# unwritable by a user-mode systemd unit running as `dennis`, so the
# router's schema-migration step raised
# `PermissionError: [Errno 13] Permission denied: '/var/lib/agent-mcp'`
# on every start.
#
# Retirement note
# ----------------
#
# The Python router (`agent-mcp-router.service`) was retired together
# with the rest of the Python implementation; `conexus-router` (Rust)
# is now the only router. Most of the config surface this check used to
# find as `Environment` entries is now a real CLI flag on
# `conexus-router` (see its own `Cli` struct doc in
# `rust/conexus-router/src/main.rs`) — `--projects-file`,
# `--sock-dir`, `--dashboard-dir`, `--external-url`, `--idle-sec`,
# `--port`. A handful have NO CLI-flag equivalent (env-var-only on the
# Rust router, mirroring the Python router's own env-var-only knobs) and
# must still be set via `Environment`: `AGENT_MCP_ROUTER_DB`,
# `AGENT_MCP_ROUTER_HOST`, `AGENT_MCP_DEFAULT_WORKSPACE`.
#
# This check parses the home-manager module's router-service
# `Environment` block for the env-var-only knobs and its `ExecStart`
# for the flag-shaped ones, anchoring on the `services.agent-mcp.router.*`
# config -> unit mapping so the same class of regression (a new
# router-startup input added upstream, and nobody remembering to wire it
# into the user-scope unit) can't sneak back in.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
HM_MODULE="$REPO_ROOT/nix/home-manager-module.nix"

TEXT="$(cat "$HM_MODULE")"

# _extract_router_service_block(): return the raw nix source of the
# `"conexus-router" = { ... };` entry inside `systemd.user.services`.
# The block runs to the matching close-brace of its own attrset
# (tracked by brace depth, since the router block itself contains
# nested `{ ... }` and the naive "next sibling key" anchor the old
# Python-router version of this helper used doesn't hold once
# CLI-flag construction adds its own `let ... in` block before
# `ExecStart`).
extract_router_service_block() {
  awk '
  BEGIN { RS = "\x01"; }
  {
    marker = "\"conexus-router\" = lib.mkIf"
    start = index($0, marker)
    if (start == 0) { print "___MARKER_NOT_FOUND___"; exit 1 }
    brace_start = index(substr($0, start), "{") + start - 1
    depth = 1
    i = brace_start + 1
    n = length($0)
    while (depth > 0 && i <= n) {
      c = substr($0, i, 1)
      if (c == "{") depth++
      else if (c == "}") depth--
      i++
    }
    print substr($0, brace_start, i - brace_start)
  }' <<< "$1"
}

# _extract_router_environment_block(): return the contents of the
# router service's `Environment = [ ... ]` list, raw nix source.
# Includes the conditional `++ lib.optionals` additions so SSO env
# vars are matched too.
extract_router_environment_block() {
  local block="$1"
  # The list extends until the matching `];` followed by an optional
  # `++ lib.optionals [ ... ]` chain the module uses for SSO vars.
  # Walk forward up to the start of the next attribute in the Service
  # block (a stable anchor that follows the Environment chain in the
  # current module).
  awk -v b="$block" '
  BEGIN {
    env_idx = index(b, "Environment = [")
    rest = substr(b, env_idx)
    end_idx = index(rest, "RuntimeDirectory = ")
    print substr(rest, 1, end_idx - 1)
  }'
}

# _extract_router_exec_start(): return the router service's
# `ExecStart = ...;` raw nix source, where the flag-shaped config
# surface (`--port`, `--projects-file`, etc.) lives.
extract_router_exec_start() {
  local block="$1"
  awk -v b="$block" '
  BEGIN {
    start = index(b, "ExecStart =")
    rest = substr(b, start)
    end_idx = index(rest, "Restart = ")
    print substr(rest, 1, end_idx - 1)
  }'
}

ROUTER_BLOCK="$(extract_router_service_block "$TEXT")"
if [ "$ROUTER_BLOCK" = "___MARKER_NOT_FOUND___" ]; then
  echo "FAIL: could not find '\"conexus-router\" = lib.mkIf' in nix/home-manager-module.nix" >&2
  exit 1
fi
ENV_BLOCK="$(extract_router_environment_block "$ROUTER_BLOCK")"
EXEC_START="$(extract_router_exec_start "$ROUTER_BLOCK")"

fail=0

# test_router_environment_sets_AGENT_MCP_ROUTER_DB:
# The router service must set `AGENT_MCP_ROUTER_DB` to an
# XDG_DATA_HOME path. Without this, user-mode systemd falls back to
# conexus-router's own compiled-in `/var/lib/agent-mcp/router.db`
# default, which it can't write, and the router restart-loops
# forever (see header comment).
if ! grep -qF 'AGENT_MCP_ROUTER_DB' <<<"$ENV_BLOCK"; then
  echo "FAIL: home-manager-module.nix must set AGENT_MCP_ROUTER_DB on " \
    "conexus-router; the compiled-in default " \
    "(/var/lib/agent-mcp/router.db) is unwritable by user-mode units." >&2
  fail=1
else
  value=$(grep -oP '"AGENT_MCP_ROUTER_DB=\K[^"]+' <<<"$ENV_BLOCK" | head -1)
  if [ -z "$value" ]; then
    echo "FAIL: AGENT_MCP_ROUTER_DB must be set via a quoted " \
      '"AGENT_MCP_ROUTER_DB=<path>" entry in the Environment list.' >&2
    fail=1
  else
    if grep -qF '/var/lib' <<<"$value"; then
      echo "FAIL: AGENT_MCP_ROUTER_DB must not point under /var/lib (got '$value'); " \
        "user-mode units cannot write there." >&2
      fail=1
    fi
    # XDG_DATA_HOME defaults to ~/.local/share; the home-manager idiom
    # is `${config.xdg.dataHome}/...`. Accept that, or an explicit
    # ~/.local/share interpolation via %h.
    if ! grep -qF 'xdg.dataHome' <<<"$value" && ! grep -qF '%h/.local/share' <<<"$value"; then
      echo "FAIL: AGENT_MCP_ROUTER_DB='$value' should resolve under " \
        'XDG_DATA_HOME (use ${config.xdg.dataHome}/agent-mcp/router.db ' \
        'or %h/.local/share/agent-mcp/router.db).' >&2
      fail=1
    fi
  fi
fi

# test_router_environment_has_no_var_lib_defaults:
# Defense in depth: no env var in the user-mode router unit may
# point under /var/lib/*. User-mode systemd cannot write there.
var_lib_matches=$(grep -oP '"AGENT_MCP_[A-Z_]+=/var/lib[^"]*"' <<<"$ENV_BLOCK" || true)
if [ -n "$var_lib_matches" ]; then
  echo "FAIL: User-mode router unit env vars must not point under /var/lib: " >&2
  echo "$var_lib_matches" >&2
  fail=1
fi

# test_router_environment_declares_required_var, parametrized over:
#   AGENT_MCP_ROUTER_DB, AGENT_MCP_DEFAULT_WORKSPACE
# Env vars `conexus-router` reads at startup that have NO CLI-flag
# equivalent (env-var-only, per its own `Cli` struct doc in
# rust/conexus-router/src/main.rs). Each MUST be set in the
# home-manager router unit so the user-mode service has the values it
# needs without falling back to a root-only path or the wrong
# default.
for var_name in "AGENT_MCP_ROUTER_DB" "AGENT_MCP_DEFAULT_WORKSPACE"; do
  if ! grep -qF -- "$var_name" <<<"$ENV_BLOCK"; then
    echo "FAIL: home-manager-module.nix: conexus-router Environment must set " \
      "$var_name (the router reads it at startup, with no CLI-flag " \
      "equivalent; missing it makes the unit fall back to a path/value " \
      "user-mode cannot satisfy)." >&2
    fail=1
  fi
done

# test_router_exec_start_passes_required_flag, parametrized over:
#   --port --projects-file --sock-dir --dashboard-dir --external-url --idle-sec
# Config surface that IS a real CLI flag on `conexus-router` (see its
# own `Cli` struct doc). Each MUST appear in the router unit's
# ExecStart.
for flag_name in "--port" "--projects-file" "--sock-dir" "--dashboard-dir" "--external-url" "--idle-sec"; do
  if ! grep -qF -- "$flag_name" <<<"$EXEC_START"; then
    echo "FAIL: home-manager-module.nix: conexus-router ExecStart must pass " \
      "$flag_name (real CLI flag on conexus-router; missing it " \
      "makes the unit fall back to conexus-router's own compiled-in " \
      "default)." >&2
    fail=1
  fi
done

if [ "$fail" -ne 0 ]; then
  exit 1
fi

echo "PASS: home-manager-module-env"
