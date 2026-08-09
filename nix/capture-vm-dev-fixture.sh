# Capture a vm-dev preload fixture: boot an ephemeral vm-dev, seed a
# dataset through the REAL REST API (the same path a user/verify-all
# takes), quiesce the SQLite DBs, and write a raw tar of the state-dir
# subtree to nix/vm-dev/fixtures/<name>.tar.zst.
#
# Reproducible source-of-truth for the committed fixtures: re-run after
# a schema migration that changes seeded tables. See
# nix/vm-dev/fixtures/README.md.
#
# @VM_DEV_RUN@ is substituted at build time with the vm-dev runner
# (nix/run-vm-dev.sh wrapper); this script drives it with a fresh
# ephemeral disk on capture-specific host ports so it never collides
# with an interactive `nix run .#vm-dev`.
set -euo pipefail

VM_DEV_RUN="@VM_DEV_RUN@"

name="${1:-}"
if [ -z "$name" ] || [ "$name" = "--help" ] || [ "$name" = "-h" ]; then
  cat >&2 <<EOF
Usage: nix run .#capture-vm-dev-fixture -- <name>

Boots an ephemeral vm-dev, seeds a demo dataset via the REST API,
and writes nix/vm-dev/fixtures/<name>.tar.zst (commit the result).

Requires the host LLM endpoints up (chat :11435, embeddings :11434) —
registering an agent spawns a project backend, which is gated on the
vm-dev endpoint probe. Run from the repo root.
EOF
  exit 2
fi
if ! [[ "$name" =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "capture-vm-dev-fixture: name must be [A-Za-z0-9._-] (got '$name')" >&2
  exit 2
fi

# Must run from the repo root so the output lands in the tree.
if [ ! -e flake.nix ] || [ ! -d nix/vm-dev ]; then
  echo "capture-vm-dev-fixture: run from the repo root (flake.nix + nix/vm-dev/ not found in \$PWD)" >&2
  exit 2
fi
out="nix/vm-dev/fixtures/${name}.tar.zst"

# Capture-specific ports (offset from the interactive defaults) so a
# capture can run alongside a live sandbox. Overridable if they clash.
host_port="${CAPTURE_HOST_PORT:-18090}"
ssh_port="${CAPTURE_SSH_PORT:-18232}"
base="http://127.0.0.1:${host_port}"
V='Accept: application/vnd.agent-mcp.v1+json'
CT='Content-Type: application/json'
ssh_opts=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -p "$ssh_port")
ssh_host="root@127.0.0.1"

workdir="$(mktemp -d --tmpdir capture-vm-dev.XXXXXX)"
cj="$workdir/cookies.txt"
vm_pid=""
cleanup() {
  [ -n "$vm_pid" ] && kill "$vm_pid" 2>/dev/null || true
  rm -rf -- "$workdir" 2>/dev/null || true
}
trap cleanup EXIT

echo "capture-vm-dev-fixture: booting ephemeral vm-dev (dash :$host_port, ssh :$ssh_port)"
AGENT_MCP_VM_DEV_HOST_PORT="$host_port" \
AGENT_MCP_VM_DEV_SSH_PORT="$ssh_port" \
  "$VM_DEV_RUN" --ephemeral >"$workdir/vm.log" 2>&1 &
vm_pid=$!

echo "capture-vm-dev-fixture: waiting for router readiness..."
ready=0
for _ in $(seq 1 120); do
  if curl -fsS --max-time 4 -H "$V" "$base/agent-mcp/api/router/health" 2>/dev/null | grep -q '"ok": *true'; then
    ready=1; break
  fi
  kill -0 "$vm_pid" 2>/dev/null || { echo "capture-vm-dev-fixture: VM exited early — see $workdir/vm.log" >&2; tail -20 "$workdir/vm.log" >&2 || true; exit 1; }
  sleep 5
done
[ "$ready" = 1 ] || { echo "capture-vm-dev-fixture: router never became ready" >&2; exit 1; }

# ── Seed via the real REST API (default demo dataset) ──────────────
echo "capture-vm-dev-fixture: seeding demo dataset..."
curl -fsS -c "$cj" -X POST "$base/agent-mcp/login" \
  --data-urlencode username=dev --data-urlencode password=dev -o /dev/null
api() { curl -fsS -b "$cj" -H "$V" -H "$CT" "$@"; }

slug="demo"
api -X POST "$base/agent-mcp/api/router/projects" -d "{\"name\":\"$slug\"}" -o /dev/null
for a in '{"agent_id":"alice","role":"worker"}' '{"agent_id":"bob","role":"manager"}'; do
  api -X POST "$base/agent-mcp/api/$slug/agents/register" -d "$a" -o /dev/null
done
root_id="$(api -X POST "$base/agent-mcp/api/$slug/tasks" -d '{"task_title":"Demo root task"}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["task_id"])')"
for t in "Wire the dashboard" "Write the tests"; do
  api -X POST "$base/agent-mcp/api/$slug/tasks" \
    -d "{\"task_title\":\"$t\",\"parent_task\":\"$root_id\"}" -o /dev/null
done
echo "capture-vm-dev-fixture: seeded project '$slug' (root $root_id + 2 children, alice/bob)"

# ── Quiesce + tar the state dir out over SSH ───────────────────────
echo "capture-vm-dev-fixture: quiescing DBs + capturing state dir..."
sshpass -p '' ssh "${ssh_opts[@]}" "$ssh_host" \
  "systemctl stop 'agent-mcp@*' agent-mcp-router.service 2>/dev/null; sync"
mkdir -p "$(dirname "$out")"
# Raw tar of the state-dir CONTENTS (rooted at the dir), zstd-compressed
# on the host side so the guest needs no extra tooling.
sshpass -p '' ssh "${ssh_opts[@]}" "$ssh_host" \
  "tar -C /var/lib/agent-mcp -cpf - ." | zstd -q -19 -o "$out" -f

sshpass -p '' ssh "${ssh_opts[@]}" "$ssh_host" "poweroff" 2>/dev/null || true
sleep 2

sz="$(du -h "$out" | cut -f1)"
echo "capture-vm-dev-fixture: wrote $out ($sz)"
echo "capture-vm-dev-fixture: commit it, then: AGENT_MCP_VM_DEV_PRELOAD=$name nix run .#vm-dev"
