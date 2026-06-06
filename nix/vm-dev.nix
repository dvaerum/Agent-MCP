{ config, lib, pkgs, modulesPath, src ? null, ... }:
# Path B interactive sandbox VM — boots the multi-tenant stack like
# nix/vm.nix but with two differences tailored for dashboard E2E work:
#
#   1. forwardPorts maps host:18080 → guest:1337 (the router port).
#      The standard vm.nix uses host:5454 — this VM exists in parallel
#      so a dev can have both running without port collisions, and
#      so the Firefox-MCP smoke script in the prancy-napping-pie plan
#      can target the documented localhost:18080 URL verbatim.
#
#   2. A oneshot systemd unit seeds a "agent-select-dev" project with
#      a known agent-roster (Admin pseudo-agent + one live worker +
#      one terminated worker) on first boot. This gives the
#      <AgentSelect> dropdown meaningful content immediately so the
#      acceptance script can verify:
#         - Admin pinned at top
#         - the live worker appears
#         - the terminated worker does NOT appear
#      without manually creating agents first.
#
# Everything else (Ollama, systemd shape, agent-mcp services) is
# inherited from the regular multi-tenant module. We import vm.nix
# directly and patch the two divergent attrs via lib.mkForce.

let
  project = "agent-select-dev";
in
{
  imports = [
    (import ./vm.nix {
      inherit config lib pkgs modulesPath src;
      mode = "multi";
      autoProject = project;
    })
  ];

  # Override the host-side port forwarding so the dev sandbox lives at
  # host:18080 — orthogonal to the host:5454 forward in the default
  # multi-tenant VM. The dev can run both simultaneously.
  virtualisation.forwardPorts = lib.mkForce [
    {
      from = "host";
      host.address = "127.0.0.1";
      host.port = 18080;
      guest.port = 1337;
    }
  ];

  # First-boot seed dataset: pre-create the two worker agents the
  # acceptance script expects (one live, one terminated). Admin is
  # auto-synthesised by the router. We use the dashboard's own REST
  # endpoints so the seed dataset goes through the same code path a
  # human would — there's no risk of drifting from the canonical
  # agent-row shape. ConditionPathExists makes this idempotent across
  # reboots without an extra state file: once the marker is laid down
  # the unit no-ops.
  systemd.services.agent-mcp-vm-dev-seed = {
    description = "Seed dataset for the agent-select dev sandbox";
    after = [ "agent-mcp-router.service" "network-online.target" ];
    wants = [ "agent-mcp-router.service" "network-online.target" ];
    wantedBy = [ "multi-user.target" ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      # Marker so the unit only runs once per persistent state dir.
      # The agent-mcp services place their state under /var/lib/agent-mcp
      # which is on the qcow2 scratch disk in vm.nix.
      ConditionPathExists = "!/var/lib/agent-mcp/.vm-dev-seeded";
    };
    path = with pkgs; [ curl jq coreutils ];
    script = ''
      set -euo pipefail

      # Wait until the router answers — the agent-mcp services come
      # up in parallel with us. 60 × 1s is generous; the router is
      # usually live well before that on cold boot.
      for i in $(seq 1 60); do
        if curl --silent --fail --max-time 2 \
            "http://127.0.0.1:1337/agent-mcp/__health" >/dev/null 2>&1; then
          break
        fi
        sleep 1
      done

      # Discover the admin token from the seeded project's state.
      # The agent-mcp-router auto-creates the ${project} project (via
      # autoProject="${project}" in vm.nix) and writes the admin
      # token into the project's state dir.
      admin_token=""
      for i in $(seq 1 30); do
        if [ -f "/var/lib/agent-mcp/projects/${project}/admin_token" ]; then
          admin_token="$(cat /var/lib/agent-mcp/projects/${project}/admin_token)"
          break
        fi
        sleep 1
      done

      if [ -z "$admin_token" ]; then
        echo "agent-mcp-vm-dev-seed: admin token not found after 30s; bailing" >&2
        exit 0   # don't crash the unit; the dashboard still works without seeds
      fi

      base="http://127.0.0.1:1337/agent-mcp/app/${project}"

      # Helper that POSTs JSON to the dashboard's create-agent endpoint.
      create_agent() {
        local agent_id="$1"
        curl --silent --fail --max-time 10 \
          -X POST "$base/api/create-agent" \
          -H 'Content-Type: application/json' \
          -H 'Accept: application/vnd.agent-mcp.v1+json' \
          --data "$(jq -nc \
              --arg t "$admin_token" \
              --arg a "$agent_id" \
              '{token:$t, agent_id:$a, capabilities:[], working_directory:"/tmp"}')" \
          >/dev/null || echo "agent-mcp-vm-dev-seed: failed to create $agent_id (already exists?)" >&2
      }

      terminate_agent() {
        local agent_id="$1"
        curl --silent --fail --max-time 10 \
          -X POST "$base/api/terminate-agent" \
          -H 'Content-Type: application/json' \
          -H 'Accept: application/vnd.agent-mcp.v1+json' \
          --data "$(jq -nc \
              --arg t "$admin_token" \
              --arg a "$agent_id" \
              '{token:$t, agent_id:$a}')" \
          >/dev/null || echo "agent-mcp-vm-dev-seed: failed to terminate $agent_id" >&2
      }

      create_agent "worker-live"
      create_agent "worker-ghost"
      # Terminate the second one so the dropdown can verify it does
      # NOT appear among the live agents.
      terminate_agent "worker-ghost"

      touch /var/lib/agent-mcp/.vm-dev-seeded
      echo "agent-mcp-vm-dev-seed: seeded project=${project} live=worker-live terminated=worker-ghost"
    '';
  };
}
