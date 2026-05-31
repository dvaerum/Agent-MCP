#!/usr/bin/env bash
# User-facing entrypoint for `nix run github:dvaerum/Agent-MCP`.
# Wraps the pre-built `run-agent-mcp-vm` script that NixOS' qemu-vm
# module emits, adding flag parsing + persist-dir bookkeeping.
#
# The flake hard-substitutes @VM_MULTI@ and @VM_SINGLE@ at build
# time with the absolute store paths of the two VM derivations.
set -euo pipefail

MULTI_VM="@VM_MULTI@"
SINGLE_VM="@VM_SINGLE@"

print_usage() {
  cat <<EOF
Usage: nix run github:dvaerum/Agent-MCP -- [flags]

Boots a self-contained NixOS VM running the agent-mcp deployment.
The host can reach the VM at http://localhost:5454.

Flags:
  --minimal             Single-tenant agent-mcp backend on guest:8080
                        (instead of router + template on guest:1337).
  --ephemeral           Use a tmpdir for VM state; nothing survives.
                        Mutually exclusive with --persist.
  --persist DIR         Persistent state directory on the host.
                        Default: \$PWD/vm-persistent-data/
  --project NAME        Name of the auto-created project in multi-
                        tenant mode (default: e2e). Ignored for
                        --minimal.
  --no-auto-project     Skip the bootstrap; user POSTs
                        /agent-mcp/__create explicitly. Multi only.
  --help, -h            Print this and exit.
EOF
}

mode="multi"
ephemeral=0
persist_dir=""
project="e2e"
no_auto_project=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --minimal) mode="single"; shift ;;
    --ephemeral) ephemeral=1; shift ;;
    --persist)
      [[ $# -ge 2 ]] || { echo "agent-mcp: --persist needs a DIR" >&2; exit 2; }
      persist_dir="$2"; shift 2 ;;
    --project)
      [[ $# -ge 2 ]] || { echo "agent-mcp: --project needs a NAME" >&2; exit 2; }
      project="$2"; shift 2 ;;
    --no-auto-project) no_auto_project=1; shift ;;
    --help|-h) print_usage; exit 0 ;;
    --) shift; break ;;
    *) echo "agent-mcp: unknown flag: $1" >&2; print_usage >&2; exit 2 ;;
  esac
done

# Mode → VM selector.
if [[ "$mode" == "multi" ]]; then
  vm_store="$MULTI_VM"
else
  vm_store="$SINGLE_VM"
  if [[ "$no_auto_project" == "1" || "$project" != "e2e" ]]; then
    echo "agent-mcp: --project / --no-auto-project ignored in --minimal mode" >&2
  fi
fi

# Persist dir resolution.
if [[ "$ephemeral" == "1" && -n "$persist_dir" ]]; then
  echo "agent-mcp: --ephemeral and --persist are mutually exclusive" >&2
  exit 2
fi

cleanup=""
if [[ "$ephemeral" == "1" ]]; then
  state_dir="$(mktemp -d --tmpdir agent-mcp-vm.XXXXXXXX)"
  cleanup="$state_dir"
  trap 'rm -rf -- "$cleanup"' EXIT
else
  if [[ -z "$persist_dir" ]]; then
    persist_dir="$PWD/vm-persistent-data"
  fi
  mkdir -p -- "$persist_dir"
  state_dir="$(readlink -f -- "$persist_dir")"
fi

# The VM uses two substrates side-by-side inside `state_dir`:
#   disk.qcow2  — agent-mcp state. SQLite WAL needs real fcntl
#                 locks, so this has to be a real block device.
#   ollama/     — Ollama's model dir, bind-mounted into the guest
#                 at /var/lib/ollama via 9p. Ollama stores blobs
#                 as plain files (no SQLite) so 9p is fine, and
#                 the user can wipe disk.qcow2 without forcing
#                 a ~620 MB embedding-model redownload.
export NIX_DISK_IMAGE="$state_dir/disk.qcow2"
export AGENT_MCP_OLLAMA_DIR="$state_dir/ollama"
mkdir -p -- "$AGENT_MCP_OLLAMA_DIR"
export TMPDIR="$state_dir"
export USE_TMPDIR=1

if [[ "$mode" == "multi" ]]; then
  echo "agent-mcp: booting multi-tenant VM"
  echo "agent-mcp: dashboard will appear at http://localhost:5454/agent-mcp/__dashboard/${project}/"
else
  echo "agent-mcp: booting single-tenant VM"
  echo "agent-mcp: backend reachable at http://localhost:5454/"
fi
echo "agent-mcp: state dir: $state_dir"
echo "agent-mcp: Ctrl-C to shut down"

# The qemu-vm module emits run-<hostname>-vm. The store path produced
# by `config.system.build.vm` exposes it under bin/.
exec "$vm_store/bin/run-agent-mcp-vm"
