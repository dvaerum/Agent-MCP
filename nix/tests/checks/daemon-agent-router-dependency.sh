#!/usr/bin/env bash
# Regression guard: the daemon-agent unit depends on the router it will
# actually talk to.
#
# Background (live incident, 2026-09-08)
# ---------------------------------------
#
# `agent-mcp-daemon-agent@<instance>.service`'s `After`/`Wants` used
# to hardcode `agent-mcp-router.service` unconditionally, regardless of
# `services.agent-mcp.router.impl` (an A/B option that has since been
# removed together with the rest of the Python implementation). Every
# daemon-agent activation -- including one on a project already flipped
# to `router.impl = "rust"` -- therefore `Want`ed -- and so started --
# the Python router alongside an already-running `conexus-router`, both
# competing for the same port. Confirmed live: the Python router
# crash-looped (`address already in use`) after every subsequent
# `home-manager switch`, requiring a manual `systemctl --user stop
# agent-mcp-router` each time.
#
# Retirement note
# ----------------
#
# `router.impl` and the Python router unit (`agent-mcp-router`) were
# retired together with the rest of the Python implementation;
# `conexus-router` is now the ONLY router, so the daemon-agent
# template's `After`/`Wants` are unconditionally
# `conexus-router.service` -- no more per-project `router.impl`
# branching to get wrong. This test keeps that unconditional dependency
# pinned to the right unit name (a plain string typo here would silently
# degrade to "waits on nothing that actually exists", which systemd
# treats as immediately satisfied -- the daemon-agent would race the
# router's own startup rather than fail loudly).
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
in (harness { pkgs = base; src = "$REPO_ROOT"; }).daemonAgentRouterDependency
EOF
)

stderr_file="$(mktemp)"
trap 'rm -f "$stderr_file"' EXIT

json=$(NIX_CONFIG="experimental-features = nix-command flakes" \
  timeout 1800 nix eval --impure --json --expr "$DRIVER" 2>"$stderr_file") \
  || { echo "FAIL: nix eval failed:" >&2; cat "$stderr_file" >&2; exit 1; }

# test_daemon_agent_depends_on_conexus_router:
# Every daemon-agent instance waits on and starts `conexus-router`.
#
# Unconditional now that `conexus-router` is the only router
# implementation -- see the module doc above for the live incident
# this pins a regression of.
after=$(jq -c '.after' <<<"$json")
wants=$(jq -c '.wants' <<<"$json")

if [ "$after" != '["conexus-router.service"]' ]; then
  echo "FAIL: dep.after expected [\"conexus-router.service\"], got: $after" >&2
  exit 1
fi

if [ "$wants" != '["conexus-router.service"]' ]; then
  echo "FAIL: dep.wants expected [\"conexus-router.service\"], got: $wants" >&2
  exit 1
fi

echo "PASS: daemon-agent-router-dependency"
