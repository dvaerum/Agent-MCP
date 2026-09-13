#!/usr/bin/env bash
# Regression guard: the home-manager module's per-project backend
# service declares the env vars its OWN implementation actually needs —
# and, now that the implementation changed, does NOT carry over an env
# var whose need was specific to the retired one.
#
# Background (Python-backend era)
# --------------------------------
#
# PR #223 fixed `agent-mcp-router.service` by setting
# `AGENT_MCP_ROUTER_DB=${config.xdg.dataHome}/agent-mcp/router.db`. The
# same drift then bit the per-project backend template
# (`agent-mcp@<project>.service`), which never set the env var at all.
#
# Real reproduction on the deployed system (2026-06-24):
#
#     $ curl -b cookies '.../api/<project>/all-data' \
#         -H 'Accept: application/vnd.agent-mcp.v1+json'
#     -> 401 {"detail":{"error":"login_required", ...}}
#
#     $ journalctl --user -u 'agent-mcp@<project>.service'
#     agent_mcp.app.deps - WARNING - operator-session resolution failed
#       for session '...'; treating as anonymous
#
# Root cause: the Python backend's `_resolve_session_user` lazily
# imported `..router.identity` and opened the SAME sqlite router.db the
# router process used, to resolve a forwarded operator-session cookie
# directly against it. Without `AGENT_MCP_ROUTER_DB` set, that open hit
# the `/var/lib/agent-mcp/router.db` default, which user-mode units
# cannot read.
#
# Retirement note — this requirement does not carry over to conexus@
# -----------------------------------------------------------------------
#
# The Python backend (`agent-mcp@<name>.service`) was retired together
# with the rest of the Python implementation; `conexus@<name>.service`
# (Rust) is the sole per-project backend now, and it does NOT open
# router.db at all — see `home-manager-module.nix`'s own comment on the
# `conexus@` unit: "conexus-backend doesn't touch the router.db at all
# ... matching Python's own documented behavior for this seam" (Wave 3
# already deleted the parallel `--system-token-out` plumbing; the
# forwarding-HMAC signature the router signs into its proxied requests is
# the ONLY remaining router->backend auth channel). So the invariant this
# check now pins is the opposite of its original one: `conexus@` must NOT
# carry an `AGENT_MCP_ROUTER_DB` entry that would be dead weight
# suggesting a code path that doesn't exist in this implementation.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
HM_MODULE="$REPO_ROOT/nix/home-manager-module.nix"

TEXT="$(cat "$HM_MODULE")"

# _extract_backend_service_block(): return the raw nix source of the
# `"conexus@" = lib.mkIf ... { ... };` entry inside
# `systemd.user.services`, tracked by brace depth (the block contains
# its own nested `{ ... }`, so a naive "next sibling key" string
# search is not a safe anchor).
extract_backend_service_block() {
  awk '
  BEGIN { RS = "\x01"; }
  {
    marker = "\"conexus@\" = lib.mkIf"
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

# _extract_backend_environment_block(): return the contents of the
# backend template's `Environment = [ ... ]` list, or "" if it has
# none (which is the current, correct shape for `conexus@` — see
# header comment).
extract_backend_environment_block() {
  local block="$1"
  if ! grep -qF 'Environment = [' <<<"$block"; then
    echo ""
    return
  fi
  awk -v b="$block" '
  BEGIN {
    env_idx = index(b, "Environment = [")
    rest = substr(b, env_idx)
    end_idx = index(rest, "];")
    print substr(rest, 1, end_idx + 1)
  }'
}

fail=0

# test_backend_service_block_is_conexus:
# Sanity anchor: the per-project backend template is `conexus@`.
#
# If this ever stops matching, every other assertion in this check is
# silently vacuous (the extraction helper would raise an error
# first, but this gives a clearer failure message for the common
# case of the marker string drifting).
if ! grep -qF '"conexus@" = lib.mkIf' <<<"$TEXT"; then
  echo 'FAIL: expected the per-project backend template to be named' \
    '"conexus@" in nix/home-manager-module.nix' >&2
  exit 1
fi

BLOCK="$(extract_backend_service_block "$TEXT")"
if [ "$BLOCK" = "___MARKER_NOT_FOUND___" ]; then
  echo "FAIL: could not find '\"conexus@\" = lib.mkIf' in nix/home-manager-module.nix" >&2
  exit 1
fi
ENV_BLOCK="$(extract_backend_environment_block "$BLOCK")"

# test_backend_does_not_set_AGENT_MCP_ROUTER_DB:
# `conexus@` must NOT set `AGENT_MCP_ROUTER_DB`.
#
# Unlike the retired Python backend, `conexus-backend` never opens
# router.db directly (see header comment) — the forwarding-HMAC
# signature is its only router-trust channel. Setting this var here
# would be dead weight at best, and at worst a signal that someone
# is trying to re-add a router.db-opening code path to the backend
# without updating the auth architecture doc alongside it.
if grep -qF 'AGENT_MCP_ROUTER_DB' <<<"$ENV_BLOCK"; then
  echo 'FAIL: conexus@ backend template sets AGENT_MCP_ROUTER_DB, but' \
    'conexus-backend does not open router.db (forwarding-HMAC is' \
    'its only router-trust channel) — either this is dead' \
    'configuration, or the backend gained a new router.db-opening' \
    'code path that this check (and the module'"'"'s own comment on' \
    'the conexus@ unit) needs updating to reflect.' >&2
  fail=1
fi

# test_backend_environment_has_no_var_lib_defaults:
# Defense in depth: no env var in the user-mode backend template may
# point under /var/lib/*. User-mode systemd cannot read or write
# there (the per-user systemd manager runs as the operator, not
# root).
var_lib_matches=$(grep -oP '"AGENT_MCP_[A-Z_]+=/var/lib[^"]*"' <<<"$ENV_BLOCK" || true)
if [ -n "$var_lib_matches" ]; then
  echo "FAIL: User-mode backend template env vars must not point under /var/lib:" >&2
  echo "$var_lib_matches" >&2
  fail=1
fi

if [ "$fail" -ne 0 ]; then
  exit 1
fi

echo "PASS: home-manager-module-backend-env"
