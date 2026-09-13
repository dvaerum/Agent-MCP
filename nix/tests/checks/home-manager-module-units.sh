#!/usr/bin/env bash
# Regression guard: the home-manager module's per-project backend
# template (`conexus@<name>.service`) must generate the
# `forwarding_hmac` key file in an ExecStartPre, the same way the NixOS
# module does (PRs #214, #216, #217).
#
# Background
# ----------
#
# PRs #214/#216/#217 ported the per-project HMAC key generation from the
# router (Python) into the systemd unit's ExecStartPre. The rationale,
# quoted from PR #214:
#
#     Old: router generates key, writes to disk, invokes systemctl start.
#          Self-heal on cache hit covers the router-driven restart, but
#          systemd's `Restart=on-failure` runs without the router.
#
#     New: systemd unit's ExecStartPre owns key generation. EVERY path
#          that starts the unit — manual `systemctl start`, on-failure
#          restart, boot-time activation — guarantees the file is on disk
#          before the backend's `--forwarding-hmac-in` validator runs.
#
# That fix landed in `nix/module.nix` (system-mode NixOS module). The
# home-manager template (`nix/home-manager-module.nix`) was never
# updated to match — the same drift pattern as PR #223 (the router-DB
# env-var port). Deployed home-manager systems hit:
#
#     agent-mcp-launcher: Error: Invalid value for '--forwarding-hmac-in':
#       File '/run/user/1000/agent-mcp/<name>/forwarding_hmac' does not exist.
#     systemd: agent-mcp@<name>.service: Main process exited,
#       code=exited, status=2/INVALIDARGUMENT
#     systemd: Scheduled restart job, restart counter is at 630.
#
# These checks pin the home-manager template's ExecStartPre to the same
# shape as the NixOS module, adapted for user-scope:
#
# - The ExecStartPre block must include a script that creates
#   `forwarding_hmac` from `/dev/urandom` if missing.
# - The script must use `pkgs.runtimeShell` (PR #216 fix: coreutils
#   does NOT ship `sh`, the unit fails 203/EXEC otherwise).
# - The script must write exactly 32 raw bytes (PR #217 fix: don't
#   `.strip()`, the bytes are binary and any whitespace at the
#   boundary is data).
# - The file mode must be 0600.
# - The unit must declare `RuntimeDirectory=agent-mcp/%i` so the
#   parent dir exists with the right owner/mode.
# - The existing socket-removal ExecStartPre must still be present (the
#   pre-existing defensive cleanup).
#
# Retirement note: the per-project backend template was `agent-mcp@`
# (Python) when these fixes landed; it is `conexus@` (Rust) now that
# the Python implementation was retired, but the ExecStartPre shape these
# checks pin is unchanged -- the forwarding-HMAC contract is implementation-
# agnostic (both backends read the same file via `--forwarding-hmac-in`).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
HM_MODULE="$REPO_ROOT/nix/home-manager-module.nix"

TEXT="$(cat "$HM_MODULE")"

# _extract_backend_template_block(): return the raw nix source of the
# `"conexus@" = lib.mkIf ... { ... };` systemd user service block,
# tracked by brace depth (the block contains its own nested
# `{ ... }`, so a naive "next sibling key" string search is not a safe
# anchor).
extract_backend_template_block() {
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

BLOCK="$(extract_backend_template_block "$TEXT")"
if [ "$BLOCK" = "___MARKER_NOT_FOUND___" ]; then
  echo "FAIL: could not find '\"conexus@\" = lib.mkIf' in nix/home-manager-module.nix" >&2
  exit 1
fi

fail=0

# test_backend_template_declares_runtime_directory:
# The per-project backend unit must declare
# `RuntimeDirectory=agent-mcp/%i` so systemd creates
# `$XDG_RUNTIME_DIR/agent-mcp/<name>/` with the right owner/mode
# before any ExecStartPre runs.
if ! grep -qP 'RuntimeDirectory\s*=\s*"agent-mcp/%i"' <<<"$BLOCK"; then
  echo 'FAIL: home-manager-module.nix: "conexus@" service must set' \
    'RuntimeDirectory = "agent-mcp/%i" so the parent dir exists' \
    'before ExecStartPre tries to write forwarding_hmac into it.' >&2
  fail=1
fi

# test_backend_template_generates_forwarding_hmac:
# The per-project backend unit's ExecStartPre must include a script
# that creates `forwarding_hmac` from /dev/urandom when missing.
#
# The NixOS module ships exactly this; the home-manager template
# must mirror it. The drift caused real deploys to crash-loop with
# `--forwarding-hmac-in` pointing at a non-existent file.
if ! grep -qF 'forwarding_hmac' <<<"$BLOCK"; then
  echo 'FAIL: home-manager-module.nix: "conexus@" service must contain an' \
    'ExecStartPre that generates the forwarding_hmac key file. The' \
    'NixOS module gained this in PR #214 (F015 v4); the home-manager' \
    'template was never updated, causing backend crash-loop with' \
    "Error: Invalid value for '--forwarding-hmac-in': File '...'" \
    'does not exist.' >&2
  fail=1
fi

# The generator must read from /dev/urandom (head -c 32). Match the
# head invocation that produces 32 bytes.
if ! grep -qP 'head\s+-c\s+32\s+/dev/urandom' <<<"$BLOCK"; then
  echo 'FAIL: home-manager-module.nix: forwarding_hmac generator must use' \
    '`head -c 32 /dev/urandom` (32 raw bytes). The NixOS module'"'"'s' \
    'pattern is the reference; bytes are binary and must NOT be' \
    'stripped or transformed (see PR #217).' >&2
  fail=1
fi

# The mode must be 0600 — set via chmod after creation.
if ! grep -qP 'chmod\s+600' <<<"$BLOCK"; then
  echo 'FAIL: home-manager-module.nix: forwarding_hmac must be chmod 600' \
    'after creation; the key is sensitive material.' >&2
  fail=1
fi

# test_backend_template_uses_runtime_shell_for_execstartpre:
# The ExecStartPre script that generates forwarding_hmac must use
# `pkgs.runtimeShell`, not `${pkgs.coreutils}/bin/sh`.
#
# PR #216 (F015 v6): coreutils does NOT ship `sh`. The original
# F015 v4 used `${pkgs.coreutils}/bin/sh` and the unit failed every
# start with `status=203/EXEC`. The fix is `pkgs.runtimeShell`.
hmac_lines="$(grep -F 'forwarding_hmac' <<<"$BLOCK" || true)"
if [ -z "$hmac_lines" ]; then
  echo "FAIL: forwarding_hmac line not found (other check catches this)" >&2
  fail=1
else
  if ! grep -qF 'pkgs.runtimeShell' <<<"$hmac_lines"; then
    echo 'FAIL: home-manager-module.nix: the ExecStartPre that generates' \
      'forwarding_hmac must invoke `${pkgs.runtimeShell}`. Using' \
      '`${pkgs.coreutils}/bin/sh` fails with 203/EXEC because coreutils' \
      'does not ship sh (PR #216 / F015 v6).' >&2
    fail=1
  fi
fi

# Defense in depth: the bad pattern from F015 v4 must NOT appear
# in any non-comment line.
bad_lines="$(grep -F '${pkgs.coreutils}/bin/sh' <<<"$BLOCK" | grep -vP '^\s*#' || true)"
if [ -n "$bad_lines" ]; then
  echo 'FAIL: home-manager-module.nix: ${pkgs.coreutils}/bin/sh in the' \
    '"conexus@" service breaks every start with status=203/EXEC.' \
    'Use ${pkgs.runtimeShell} instead (PR #216 / F015 v6).' \
    "Bad lines: $bad_lines" >&2
  fail=1
fi

# test_backend_template_generator_is_idempotent:
# The forwarding_hmac generator must be a no-op if the file already
# exists (the router caches the bytes in memory; rotating the key on
# every restart would break the cache invariant — see commit
# 862e594).
#
# The pattern is `test -f <path> || { generate; }` — only create
# when missing.
if ! grep -qP 'test\s+-f\s+[^|]*forwarding_hmac' <<<"$BLOCK"; then
  echo 'FAIL: home-manager-module.nix: forwarding_hmac generator must be' \
    'idempotent — guard with `test -f <path>/forwarding_hmac ||' \
    '{ generate; }`. Without the guard, every restart rotates the' \
    "key and invalidates the router's in-memory cache." >&2
  fail=1
fi

# test_backend_template_keeps_socket_cleanup:
# The pre-existing ExecStartPre that removes a stale backend.sock
# must still be present alongside the new forwarding_hmac generator.
#
# Adding the HMAC generator must not regress the socket cleanup that
# existed before this fix.
if ! grep -qP 'rm\s+-f[^"]*backend\.sock' <<<"$BLOCK"; then
  echo 'FAIL: home-manager-module.nix: "conexus@" service must still' \
    'remove a stale backend.sock in ExecStartPre. The HMAC-generator' \
    'addition must not regress the existing socket cleanup.' >&2
  fail=1
fi

if [ "$fail" -ne 0 ]; then
  exit 1
fi

echo "PASS: home-manager-module-units"
