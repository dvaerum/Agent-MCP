#!/usr/bin/env bash
# Path B interactive sandbox for dashboard E2E development.
#
# Boots the same NixOS multi-tenant VM as `nix run .#` but forwards
# the guest router port to host:18080 (by default) instead of
# host:5454, and arranges a tiny seed dataset (Admin + one live +
# one terminated agent) so the agent dropdown has meaningful content
# immediately when Firefox-MCP drives the dashboard at
# http://localhost:18080/agent-mcp/app/<seed-project>/.
#
# The host port can be overridden at launch time by setting
# AGENT_MCP_VM_DEV_HOST_PORT — useful when something else on the
# host (e.g. SeaweedFS) already binds :18080. We rewrite the qemu
# hostfwd rule in-place on a copy of the generated run script,
# avoiding a full nix rebuild for what is purely a runtime concern.
#
# Stays alive until Ctrl-C — distinct from `nix flake check` which
# tears down after the test script exits. This is the
# "interactive dev sandbox" framing Dennis confirmed in the
# prancy-napping-pie plan's VM E2E section.
#
# The flake hard-substitutes @VM_DEV@ at build time with the absolute
# store path of the dev VM derivation (which differs from vm-multi
# only in forwardPorts and in the seed-data systemd unit).
set -euo pipefail

VM_DEV="@VM_DEV@"
PROJECT="agent-select-dev"
DEFAULT_HOST_PORT=18080

print_usage() {
  cat <<EOF
Usage: nix run .#vm-dev -- [flags]

Boots the Path B interactive dashboard sandbox. Forwards the guest
router port to host:${DEFAULT_HOST_PORT} by default. The dashboard
is at:

    http://localhost:\${HOST_PORT}/agent-mcp/app/${PROJECT}/?page=tasks

where HOST_PORT is ${DEFAULT_HOST_PORT} unless overridden.

A seed dataset (Admin + one live worker + one terminated worker) is
provisioned on first boot so the agent dropdown has meaningful
content for Firefox-MCP acceptance testing.

Environment:
  AGENT_MCP_VM_DEV_HOST_PORT
                        Override the host port qemu binds for the
                        dashboard forward. Default ${DEFAULT_HOST_PORT}.
                        Use this when something else on the host
                        already owns :${DEFAULT_HOST_PORT}.

Flags:
  --ephemeral           Use a tmpdir for VM state; nothing survives.
  --persist DIR         Persistent state directory on the host.
                        Default: \$PWD/vm-dev-persistent-data/
  --help, -h            Print this and exit.

Ctrl-C to shut down.
EOF
}

ephemeral=0
persist_dir=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ephemeral) ephemeral=1; shift ;;
    --persist)
      [[ $# -ge 2 ]] || { echo "agent-mcp-vm-dev: --persist needs a DIR" >&2; exit 2; }
      persist_dir="$2"; shift 2 ;;
    --help|-h) print_usage; exit 0 ;;
    --) shift; break ;;
    *) echo "agent-mcp-vm-dev: unknown flag: $1" >&2; print_usage >&2; exit 2 ;;
  esac
done

if [[ "$ephemeral" == "1" && -n "$persist_dir" ]]; then
  echo "agent-mcp-vm-dev: --ephemeral and --persist are mutually exclusive" >&2
  exit 2
fi

# Resolve the host port. The default matches the literal sentinel
# baked into the VM derivation's qemu hostfwd rule via
# nix/vm-dev.nix; keep both ends in sync.
host_port="${AGENT_MCP_VM_DEV_HOST_PORT:-$DEFAULT_HOST_PORT}"
if ! [[ "$host_port" =~ ^[0-9]+$ ]] || (( host_port < 1 || host_port > 65535 )); then
  echo "agent-mcp-vm-dev: AGENT_MCP_VM_DEV_HOST_PORT must be 1..65535 (got '$host_port')" >&2
  exit 2
fi

cleanup=""
if [[ "$ephemeral" == "1" ]]; then
  state_dir="$(mktemp -d --tmpdir agent-mcp-vm-dev.XXXXXXXX)"
  cleanup="$state_dir"
  trap 'rm -rf -- "$cleanup"' EXIT
else
  if [[ -z "$persist_dir" ]]; then
    persist_dir="$PWD/vm-dev-persistent-data"
  fi
  mkdir -p -- "$persist_dir"
  state_dir="$(readlink -f -- "$persist_dir")"
fi

export NIX_DISK_IMAGE="$state_dir/disk.qcow2"
export AGENT_MCP_OLLAMA_DIR="$state_dir/ollama"
mkdir -p -- "$AGENT_MCP_OLLAMA_DIR"
export TMPDIR="$state_dir"
export USE_TMPDIR=1

# Materialise a runnable qemu launcher. When the user picked a non-
# default host port, sed-substitute the sentinel hostfwd rule
# (tcp:127.0.0.1:18080-:1337) so qemu binds the override port
# instead. We refuse to launch if the sentinel isn't found —
# silently exec'ing the unrewritten store binary would bind the
# wrong port and contradict the banner.
vm_launcher="$VM_DEV/bin/run-agent-mcp-vm"
if [[ "$host_port" != "$DEFAULT_HOST_PORT" ]]; then
  rewritten="$state_dir/run-agent-mcp-vm"
  sentinel="tcp:127.0.0.1:${DEFAULT_HOST_PORT}-:1337"
  if ! grep -q -F -- "$sentinel" "$vm_launcher"; then
    echo "agent-mcp-vm-dev: cannot find hostfwd sentinel '$sentinel' in $vm_launcher" >&2
    echo "agent-mcp-vm-dev: nix/vm-dev.nix and nix/run-vm-dev.sh have drifted; refusing to launch." >&2
    exit 1
  fi
  replacement="tcp:127.0.0.1:${host_port}-:1337"
  # sed -i isn't safe across filesystems and the store is read-only,
  # so write a fresh copy under state_dir.
  sed "s|${sentinel}|${replacement}|g" "$vm_launcher" > "$rewritten"
  chmod +x "$rewritten"
  vm_launcher="$rewritten"
fi

cat <<INFO
agent-mcp-vm-dev: booting Path B interactive sandbox
agent-mcp-vm-dev: dashboard      http://localhost:${host_port}/agent-mcp/app/${PROJECT}/?page=tasks
agent-mcp-vm-dev: seed project   ${PROJECT}
agent-mcp-vm-dev: state dir      ${state_dir}
agent-mcp-vm-dev: host port      ${host_port}$( [[ "$host_port" != "$DEFAULT_HOST_PORT" ]] && echo " (override via AGENT_MCP_VM_DEV_HOST_PORT)" )
agent-mcp-vm-dev: Ctrl-C to shut down
INFO

exec "$vm_launcher"
