#!/usr/bin/env bash
# Path B interactive sandbox for dashboard E2E development.
#
# Boots the same NixOS multi-tenant VM as `nix run .#` but forwards
# the guest router port to host:18080 instead of host:5454, and
# arranges a tiny seed dataset (Admin + one live + one terminated
# agent) so the agent dropdown has meaningful content immediately
# when Firefox-MCP drives the dashboard at
# http://localhost:18080/agent-mcp/app/<seed-project>/.
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

print_usage() {
  cat <<EOF
Usage: nix run .#vm-dev -- [flags]

Boots the Path B interactive dashboard sandbox. Forwards the guest
router port to host:18080. The dashboard is at:

    http://localhost:18080/agent-mcp/app/${PROJECT}/?page=tasks

A seed dataset (Admin + one live worker + one terminated worker) is
provisioned on first boot so the agent dropdown has meaningful
content for Firefox-MCP acceptance testing.

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

cat <<INFO
agent-mcp-vm-dev: booting Path B interactive sandbox
agent-mcp-vm-dev: dashboard      http://localhost:18080/agent-mcp/app/${PROJECT}/?page=tasks
agent-mcp-vm-dev: seed project   ${PROJECT}
agent-mcp-vm-dev: state dir      ${state_dir}
agent-mcp-vm-dev: Ctrl-C to shut down
INFO

exec "$VM_DEV/bin/run-agent-mcp-vm"
