#!/usr/bin/env bash
# Path B interactive sandbox for dashboard E2E development.
#
# Boots the same NixOS multi-tenant VM as `nix run .#` but forwards
# the guest router port to host:18080 (by default) instead of
# host:5454, and pre-seeds a sentinel operator (dev / dev) via the
# env-var bootstrap (AGENT_MCP_BOOTSTRAP_USERNAME/_PASSWORD wired on
# agent-mcp-router.service in nix/vm-dev.nix) so Firefox-MCP can hit
# /login immediately and drive the dashboard at
# http://localhost:18080/agent-mcp/ without first having to walk
# through /setup.
#
# Projects are NOT auto-created — the legacy bootstrap-via-/__create
# path was retired with ADR 0014. The operator (or a driving script)
# creates projects from the dashboard UI after sign-in.
#
# The host port can be overridden at launch time by setting
# AGENT_MCP_VM_DEV_HOST_PORT — useful when something else on the
# host (e.g. SeaweedFS) already binds :18080. We rewrite the qemu
# hostfwd rule in-place on a copy of the generated run script,
# avoiding a full nix rebuild for what is purely a runtime concern.
#
# A guest:22 → host:18222 forward (overridable via
# AGENT_MCP_VM_DEV_SSH_PORT) gives the dev SSH access into the
# running VM for live diagnostics — `systemctl status`,
# `journalctl -u`, /run/agent-mcp/, /var/lib/agent-mcp/, etc.
# The VM is configured with empty-password root login (loopback only,
# DEV-MODE — see nix/vm-dev.nix).
#
# Stays alive until Ctrl-C — distinct from `nix flake check` which
# tears down after the test script exits. This is the
# "interactive dev sandbox" framing Dennis confirmed in the
# prancy-napping-pie plan's VM E2E section.
#
# This VM runs in EXTERNAL LLM MODE (nix/vm-dev.nix passes
# llm = "external" to nix/vm.nix): no ollama inside the guest, no
# model weights preloaded, 2 GB of guest RAM instead of 4 GB. The
# price is that the chat + embedding endpoints must already be
# running on the HOST — the guest reaches them via qemu user-mode's
# 10.0.2.2 host alias. A boot-time probe unit hard-fails, naming the
# URLs, rather than letting a backend run with dead embeddings.
#
# The flake hard-substitutes @VM_DEV@ at build time with the absolute
# store path of the dev VM derivation (which differs from vm-multi
# in forwardPorts, the env-var operator seed on the router unit, the
# external LLM wiring, and the dev-mode SSH stack).
set -euo pipefail

VM_DEV="@VM_DEV@"
DEFAULT_HOST_PORT=18080
DEFAULT_SSH_PORT=18222

print_usage() {
  cat <<EOF
Usage: nix run .#vm-dev -- [flags]

Boots the Path B interactive dashboard sandbox. Forwards the guest
router port to host:${DEFAULT_HOST_PORT} by default. The dashboard
is at:

    http://localhost:\${HOST_PORT}/agent-mcp/

where HOST_PORT is ${DEFAULT_HOST_PORT} unless overridden.

A sentinel operator (username \`dev\`, password \`dev\`) is seeded on
first boot via the env-var bootstrap, so /login is reachable
immediately. Projects are created through the dashboard UI after
sign-in.

Environment:
  AGENT_MCP_VM_DEV_HOST_PORT
                        Override the host port qemu binds for the
                        dashboard forward. Default ${DEFAULT_HOST_PORT}.
                        Use this when something else on the host
                        already owns :${DEFAULT_HOST_PORT}.
  AGENT_MCP_VM_DEV_SSH_PORT
                        Override the host port qemu binds for the
                        guest SSH forward. Default ${DEFAULT_SSH_PORT}.
                        SSH into the VM with:
                            ssh root@localhost -p \${SSH_PORT}
                        (DEV-MODE: root + empty password — loopback only.)
  AGENT_MCP_VM_DEV_PRELOAD
                        Name(s) of preload fixture bundle(s) to restore
                        into /var/lib/agent-mcp before the router starts,
                        so the VM comes up with projects/agents/tasks
                        already present instead of an empty first-boot
                        state. Comma-separate to stack several bundles.
                        Bundles live in nix/vm-dev/fixtures/<name>.tar.zst
                        (capture new ones with
                        \`nix run .#capture-vm-dev-fixture\`). Passed to
                        the guest via the kernel cmdline (no rebuild).
                        Only applied on a FRESH disk — see the in-guest
                        agent-mcp-vm-dev-preload.service. Default: none.

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

ssh_port="${AGENT_MCP_VM_DEV_SSH_PORT:-$DEFAULT_SSH_PORT}"
if ! [[ "$ssh_port" =~ ^[0-9]+$ ]] || (( ssh_port < 1 || ssh_port > 65535 )); then
  echo "agent-mcp-vm-dev: AGENT_MCP_VM_DEV_SSH_PORT must be 1..65535 (got '$ssh_port')" >&2
  exit 2
fi

if (( ssh_port == host_port )); then
  echo "agent-mcp-vm-dev: AGENT_MCP_VM_DEV_SSH_PORT and AGENT_MCP_VM_DEV_HOST_PORT must differ (both = $ssh_port)" >&2
  exit 2
fi

# ── Preload fixture selection (runtime, no rebuild) ────────────────
# AGENT_MCP_VM_DEV_PRELOAD names one or more fixture bundles baked into
# the VM image (nix/vm-dev/fixtures/<name>.tar.zst → /etc/agent-mcp-vm-dev/
# fixtures/ in the guest). We hand the selection to the guest on the
# kernel command line: the NixOS qemu-vm run-script appends
# $QEMU_KERNEL_PARAMS to -append, and the in-guest
# agent-mcp-vm-dev-preload.service reads the token from /proc/cmdline.
# This keeps bundle selection a pure runtime concern (same rationale as
# the hostfwd port sentinels above) — no VM rebuild to switch datasets.
preload="${AGENT_MCP_VM_DEV_PRELOAD:-}"
if [[ -n "$preload" ]]; then
  # Restrict to a safe token: bundle names are [A-Za-z0-9._-], comma-
  # separated. Anything else could smuggle extra kernel params.
  if ! [[ "$preload" =~ ^[A-Za-z0-9._-]+(,[A-Za-z0-9._-]+)*$ ]]; then
    echo "agent-mcp-vm-dev: AGENT_MCP_VM_DEV_PRELOAD must be comma-separated bundle names [A-Za-z0-9._-] (got '$preload')" >&2
    exit 2
  fi
  export QEMU_KERNEL_PARAMS="${QEMU_KERNEL_PARAMS:+$QEMU_KERNEL_PARAMS }agent_mcp_preload=${preload}"
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
# Unused while nix/vm-dev.nix runs with llm = "external" (no in-guest
# ollama ⇒ no 9p model share referencing this var). Kept — and the dir
# still created — so flipping vm-dev.nix back to llm = "internal"
# doesn't fail the launch on an unbound var under `set -u`.
export AGENT_MCP_OLLAMA_DIR="$state_dir/ollama"
mkdir -p -- "$AGENT_MCP_OLLAMA_DIR"
export TMPDIR="$state_dir"
export USE_TMPDIR=1

# Materialise a runnable qemu launcher. When the user picked a non-
# default host port for either the dashboard or SSH, sed-substitute
# the sentinel hostfwd rule(s) (tcp:127.0.0.1:18080-:1337 for the
# dashboard, tcp:127.0.0.1:18222-:22 for SSH) so qemu binds the
# override port(s) instead. We refuse to launch if a sentinel isn't
# found — silently exec'ing the unrewritten store binary would bind
# the wrong port and contradict the banner.
vm_launcher="$VM_DEV/bin/run-agent-mcp-vm"
needs_rewrite=0
[[ "$host_port" != "$DEFAULT_HOST_PORT" ]] && needs_rewrite=1
[[ "$ssh_port"  != "$DEFAULT_SSH_PORT"  ]] && needs_rewrite=1

if (( needs_rewrite == 1 )); then
  rewritten="$state_dir/run-agent-mcp-vm"
  cp -- "$vm_launcher" "$rewritten"
  chmod +w "$rewritten"

  rewrite_sentinel() {
    local sentinel="$1" replacement="$2" label="$3"
    if ! grep -q -F -- "$sentinel" "$rewritten"; then
      echo "agent-mcp-vm-dev: cannot find $label hostfwd sentinel '$sentinel' in $vm_launcher" >&2
      echo "agent-mcp-vm-dev: nix/vm-dev.nix and nix/run-vm-dev.sh have drifted; refusing to launch." >&2
      exit 1
    fi
    # In-place edit on the writable copy under state_dir.
    sed -i "s|${sentinel}|${replacement}|g" "$rewritten"
  }

  if [[ "$host_port" != "$DEFAULT_HOST_PORT" ]]; then
    rewrite_sentinel \
      "tcp:127.0.0.1:${DEFAULT_HOST_PORT}-:1337" \
      "tcp:127.0.0.1:${host_port}-:1337" \
      "dashboard"
  fi
  if [[ "$ssh_port" != "$DEFAULT_SSH_PORT" ]]; then
    rewrite_sentinel \
      "tcp:127.0.0.1:${DEFAULT_SSH_PORT}-:22" \
      "tcp:127.0.0.1:${ssh_port}-:22" \
      "ssh"
  fi

  chmod +x "$rewritten"
  vm_launcher="$rewritten"
fi

cat <<INFO
agent-mcp-vm-dev: booting Path B interactive sandbox
agent-mcp-vm-dev: dashboard      http://localhost:${host_port}/agent-mcp/
agent-mcp-vm-dev: ssh access     ssh root@localhost -p ${ssh_port}  (no password — DEV-MODE)
agent-mcp-vm-dev: operator       dev / dev  (seeded via env-var bootstrap)
agent-mcp-vm-dev: llm mode       external — 2 GB guest, endpoints on the HOST:
agent-mcp-vm-dev:                  chat       127.0.0.1:11435  (guest: 10.0.2.2)
agent-mcp-vm-dev:                  embeddings 127.0.0.1:11434  (guest: 10.0.2.2)
agent-mcp-vm-dev:                start them before booting — the guest's
agent-mcp-vm-dev:                agent-mcp-llm-endpoint-check.service refuses
agent-mcp-vm-dev:                to let the backend run against dead endpoints.
agent-mcp-vm-dev: preload       ${preload:-none}$( [[ -n "$preload" ]] && echo "  (restored into /var/lib/agent-mcp on a fresh disk)" )
agent-mcp-vm-dev: state dir      ${state_dir}
agent-mcp-vm-dev: host port      ${host_port}$( [[ "$host_port" != "$DEFAULT_HOST_PORT" ]] && echo " (override via AGENT_MCP_VM_DEV_HOST_PORT)" )
agent-mcp-vm-dev: ssh port       ${ssh_port}$( [[ "$ssh_port" != "$DEFAULT_SSH_PORT" ]] && echo " (override via AGENT_MCP_VM_DEV_SSH_PORT)" )
agent-mcp-vm-dev: Ctrl-C to shut down
INFO

exec "$vm_launcher"
