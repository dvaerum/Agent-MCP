# Restore preload fixture bundle(s) into the agent-mcp state dir on a
# FRESH vm-dev disk, before the router/backends start, so the sandbox
# comes up with real projects/agents/tasks instead of empty first-boot
# state.
#
# Bundle selection arrives on the kernel command line
# (agent_mcp_preload=<name>[,<name>...]), placed there by
# nix/run-vm-dev.sh from $AGENT_MCP_VM_DEV_PRELOAD — a pure runtime
# concern, no VM rebuild to switch datasets. Fixtures are baked into the
# image at $AGENT_MCP_FIXTURES_DIR as <name>.tar.zst: a raw tar of the
# state-dir subtree captured from a seeded VM
# (`nix run .#capture-vm-dev-fixture`).
#
# WHY raw .db files inside the tar rather than a .sql dump: RAG uses
# sqlite-vec, whose vec0 VIRTUAL tables do not round-trip through a
# `.dump`; a raw file copy preserves them exactly. If a fixture predates
# the current schema, the app's Alembic upgrade runs forward on first
# open, so a slightly-stale bundle still boots.

state_dir="${AGENT_MCP_STATE_DIR:?AGENT_MCP_STATE_DIR must be set}"
fixtures_dir="${AGENT_MCP_FIXTURES_DIR:?AGENT_MCP_FIXTURES_DIR must be set}"
marker="$state_dir/.vm-dev-preloaded"

# Parse agent_mcp_preload=<names> from the kernel cmdline. Last wins if
# somehow repeated. ConditionKernelCommandLine gates the unit, so the
# token is normally present when we run — but stay defensive.
names=""
read -ra _cmdline </proc/cmdline || true
for tok in "${_cmdline[@]}"; do
  case "$tok" in
    agent_mcp_preload=*) names="${tok#*=}" ;;
  esac
done
if [ -z "$names" ]; then
  echo "agent-mcp-vm-dev-preload: no agent_mcp_preload= on kernel cmdline; nothing to do"
  exit 0
fi

# Fresh-disk only: never clobber a persistent VM's accumulated state on
# a later reboot. Wipe the disk (or boot --ephemeral) to re-preload.
if [ -e "$marker" ]; then
  echo "agent-mcp-vm-dev-preload: already applied ($(cat "$marker" 2>/dev/null || true)); skipping"
  exit 0
fi

mkdir -p "$state_dir"

applied=""
# Bundles are comma-separated; extract each in order (later files win
# per-path). Stacking bundles that each carry router.db does NOT merge
# the project registry — capture multiple projects into ONE bundle for
# that. See nix/vm-dev/fixtures/README.md.
old_ifs="$IFS"
IFS=','
# shellcheck disable=SC2086
set -- $names
IFS="$old_ifs"
for name in "$@"; do
  bundle="$fixtures_dir/${name}.tar.zst"
  if [ ! -f "$bundle" ]; then
    echo "agent-mcp-vm-dev-preload: FAIL — bundle '$name' not found at $bundle" >&2
    avail=""
    for f in "$fixtures_dir"/*.tar.zst; do
      [ -e "$f" ] || continue
      b="$(basename "$f")"
      avail="${avail:+$avail }${b%.tar.zst}"
    done
    echo "agent-mcp-vm-dev-preload: available bundles: ${avail:-<none baked into this image>}" >&2
    exit 1
  fi
  echo "agent-mcp-vm-dev-preload: restoring '$name' -> $state_dir"
  # -p perms, --same-owner: the fixture was captured from this same VM
  # image so the archived uid/gid match the runtime agent-mcp user.
  tar -C "$state_dir" --zstd -xpf "$bundle" -p --same-owner
  applied="${applied:+$applied,}$name"
done

printf '%s\n' "$applied" >"$marker"
echo "agent-mcp-vm-dev-preload: done — applied [$applied] into $state_dir"
