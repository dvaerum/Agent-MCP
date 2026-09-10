#!/usr/bin/env bash
# User-facing entrypoint for `nix run github:dvaerum/Agent-MCP`.
# Wraps the pre-built `run-agent-mcp-vm` script that NixOS' qemu-vm
# module emits, adding flag parsing + persist-dir bookkeeping.
#
# The flake hard-substitutes @VM_MULTI@ at build time with the
# absolute store path of the VM derivation.
#
# History: this script used to also support `--minimal`, booting a
# single-tenant agent-mcp backend directly on a TCP port with no
# router in front of it. That shape ran the Python implementation and
# was retired together with it -- the Rust `conexus-backend` binary
# only serves over a Unix domain socket (see rust/conexus-backend/src/
# main.rs's own module doc), so there is no TCP-port backend left to
# boot standalone. `--minimal` is gone; only the router + per-project
# template shape remains.
set -euo pipefail

MULTI_VM="@VM_MULTI@"

print_usage() {
  cat <<EOF
Usage: nix run github:dvaerum/Agent-MCP -- [flags]

Boots a self-contained NixOS VM running the agent-mcp deployment.
The host can reach the VM at http://localhost:5454.

On first boot the dashboard's identity store is empty, so the router
redirects to /setup where you create the first operator. Subsequent
boots land on /login. Projects are created from the dashboard UI
after sign-in.

Flags:
  --ephemeral           Use a tmpdir for VM state; nothing survives.
                        Mutually exclusive with --persist.
  --persist DIR         Persistent state directory on the host.
                        Default: \$PWD/vm-persistent-data/
  --help, -h            Print this and exit.
EOF
}

ephemeral=0
persist_dir=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ephemeral) ephemeral=1; shift ;;
    --persist)
      [[ $# -ge 2 ]] || { echo "agent-mcp: --persist needs a DIR" >&2; exit 2; }
      persist_dir="$2"; shift 2 ;;
    --help|-h) print_usage; exit 0 ;;
    --) shift; break ;;
    *) echo "agent-mcp: unknown flag: $1" >&2; print_usage >&2; exit 2 ;;
  esac
done

vm_store="$MULTI_VM"

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

echo "agent-mcp: booting multi-tenant VM"
echo "agent-mcp: dashboard will appear at http://localhost:5454/agent-mcp/"
echo "agent-mcp: first boot lands on /setup; create the first operator,"
echo "agent-mcp: then create projects from the dashboard UI."
echo "agent-mcp: state dir: $state_dir"
echo "agent-mcp: Ctrl-C to shut down"

# The qemu-vm module emits run-<hostname>-vm. The store path produced
# by `config.system.build.vm` exposes it under bin/.
exec "$vm_store/bin/run-agent-mcp-vm"
