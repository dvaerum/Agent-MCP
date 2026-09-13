#!/usr/bin/env bash
# Regression guard: the router units never lose RuntimeDirectoryPreserve.
#
# Background (live incident, 2026-09-07)
# ---------------------------------------
#
# `agent-mcp-router`/`conexus-router` declare a BARE, single-component
# `RuntimeDirectory = "agent-mcp"` -- a strict parent of the per-project
# templates' `agent-mcp/%i` (`agent-mcp@`/`conexus@`). Per
# `systemd.exec(5)`'s `RuntimeDirectory=` section, a bare value like
# that IS the declaring unit's own "innermost subdirectory", so on every
# STOP of *either* router unit (a crash-loop, a redeploy, an A/B
# `router.impl` flip where both router units briefly coexist) systemd
# recursively removes the WHOLE `%t/agent-mcp/` tree by default --
# including every live per-project `conexus@<project>`/
# `agent-mcp@<project>` subdirectory and UDS socket that unrelated units
# still own and are actively listening on.
#
# Confirmed live: a stray, still-enabled `agent-mcp-router` (the legacy
# Python router, meant to be inert once `router.impl = "rust"` cleared its
# `Install.WantedBy`) was crash-looping every ~10s (`address already in
# use` against the real, already-bound `conexus-router`). Each crash
# wiped `%t/agent-mcp/` out from under both live per-project backends --
# `ss` kept reporting their sockets `LISTEN` (the kernel remembers the
# `bind()`) while every new `connect()` to the now-unlinked path
# failed, indistinguishable from "backend not ready" at every layer above
# this. The dashboard's per-project pages were non-functional on BOTH live
# projects for the full duration.
#
# `RuntimeDirectoryPreserve = "yes"` on the router unit (matching what
# `nix/module.nix`'s system-mode template already carries on ITS units)
# stops a unit's own stop from ever touching the tree. The Python router
# (`agent-mcp-router`) and the per-project Python backend
# (`agent-mcp@`) were retired together with the rest of the Python
# implementation, together with the `router.impl` A/B flip that used to
# let both router units briefly coexist mid-flip -- `conexus-router`/
# `conexus@` are the only two RuntimeDirectory-declaring units left, and
# this test pins the setting on both so it can't silently regress back
# out.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
HARNESS="$REPO_ROOT/nix/tests/eval-home-manager-module.nix"

if ! command -v nix >/dev/null 2>&1; then
  echo "SKIP: nix is not available on PATH"
  exit 0
fi

DRIVER=$(cat <<EOF
let
  repo = builtins.getFlake "$REPO_ROOT";
  base = repo.inputs.nixpkgs.legacyPackages.\${builtins.currentSystem};
  harness = import $HARNESS;
in (harness { pkgs = base; src = "$REPO_ROOT"; }).runtimeDirectoryPreserve
EOF
)

stderr_file="$(mktemp)"
trap 'rm -f "$stderr_file"' EXIT

json=$(NIX_CONFIG="experimental-features = nix-command flakes" \
  timeout 1800 nix eval --impure --json --expr "$DRIVER" 2>"$stderr_file") \
  || { echo "FAIL: nix eval failed:" >&2; cat "$stderr_file" >&2; exit 1; }

# test_runtime_directory_is_preserved_across_a_stop, parametrized over
# ["conexus-router", "conexus@"] in the original:
#
# Every RuntimeDirectory-declaring unit must survive its own stop.
#
# A bare-parent `RuntimeDirectory` shared with sibling per-project
# templates makes this the ONLY thing standing between a router crash-
# loop/redeploy and silently deleting every live backend's socket.
fail=0
for unit in "conexus-router" "conexus@"; do
  value=$(jq -r --arg u "$unit" '.[$u]' <<<"$json")
  if [ "$value" != "yes" ]; then
    echo "FAIL: ${unit}.Service.RuntimeDirectoryPreserve must be \"yes\" -- " \
      "without it, this unit's own stop deletes the shared " \
      "%t/agent-mcp/ tree (including sibling units' live sockets); " \
      "see this script's header comment for the live incident this pins. " \
      "Got: $value" >&2
    fail=1
  fi
done

if [ "$fail" -ne 0 ]; then
  exit 1
fi

echo "PASS: module-runtime-directory-preserve"
