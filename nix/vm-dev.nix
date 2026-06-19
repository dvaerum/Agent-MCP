{ config, lib, pkgs, modulesPath, src ? null, ... }:
# Path B interactive sandbox VM — boots the multi-tenant stack like
# nix/vm.nix but with three differences tailored for dashboard E2E
# work and live in-VM diagnostics:
#
#   1. forwardPorts maps host:18080 → guest:1337 (the router port)
#      by default. 18080 is also the literal sentinel that
#      nix/run-vm-dev.sh rewrites at launch time when
#      AGENT_MCP_VM_DEV_HOST_PORT is set, so a developer whose host
#      already binds :18080 (e.g. SeaweedFS) can pick another port
#      without rebuilding the VM derivation. The standard vm.nix
#      uses host:5454 — this VM exists in parallel so a dev can
#      have both running without port collisions, and so the
#      Firefox-MCP smoke script in the prancy-napping-pie plan can
#      target the documented localhost:18080 URL verbatim.
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
#   3. **DEV-MODE SSH IS WIDE OPEN.** OpenSSH is enabled with root
#      login, empty passwords, and password auth all permitted, plus
#      a host:18222 → guest:22 forward (sentinel rewritten at launch
#      via AGENT_MCP_VM_DEV_SSH_PORT, same pattern as the dashboard
#      port). This exists so we can read `systemctl status`,
#      `journalctl -u …`, /run/agent-mcp/, /var/lib/agent-mcp/, and
#      /var/log/journal/ inside the VM when a per-project backend
#      misbehaves (cf. verify-all P006: backend UDS spawn timeout).
#      This MUST NOT be copied into nix/vm.nix (the production VM).
#      A boot-time systemd banner and a motd shout this restriction
#      so the next maintainer cargo-culting from this file notices.
#
# Everything else (Ollama, systemd shape, agent-mcp services) is
# inherited from the regular multi-tenant module. We import vm.nix
# directly and patch the divergent attrs via lib.mkForce.

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
  # host:18080 by default — orthogonal to the host:5454 forward in
  # the default multi-tenant VM. The dev can run both simultaneously.
  # nix/run-vm-dev.sh rewrites the literals "18080" and "18222" in the
  # generated qemu hostfwd rules at launch time when
  # AGENT_MCP_VM_DEV_HOST_PORT / AGENT_MCP_VM_DEV_SSH_PORT are set, so
  # callers can pick different host ports without rebuilding the VM
  # derivation. Keep these values in sync with the sentinel sed
  # patterns in run-vm-dev.sh if you ever change them.
  virtualisation.forwardPorts = lib.mkForce [
    {
      from = "host";
      host.address = "127.0.0.1";
      host.port = 18080;
      guest.port = 1337;
    }
    {
      # SSH access for live in-VM diagnostics — DEV ONLY.
      from = "host";
      host.address = "127.0.0.1";
      host.port = 18222;
      guest.port = 22;
    }
  ];

  # === DEV-MODE SSH ===========================================
  # OpenSSH wide open: root login, empty passwords, password auth.
  # This is acceptable ONLY because:
  #   - the SSH port is forwarded on 127.0.0.1 (loopback only), not
  #     any routable interface, so no off-host attacker can reach it;
  #   - this module is nix/vm-dev.nix, never imported by production
  #     (nix/vm.nix) — see the file-header comment block;
  #   - a boot-time banner + motd shout the dev-mode warning so this
  #     can't quietly migrate into production.
  # nix/vm.nix hard-disables openssh; mkForce here so this module's
  # dev-only override actually wins.
  services.openssh = {
    enable = lib.mkForce true;
    settings = {
      # PermitRootLogin is a free-form string ("yes"/"no"/"prohibit-password"/…).
      PermitRootLogin = "yes";
      # PermitEmptyPasswords / PasswordAuthentication are booleans in the
      # NixOS module (translated to yes/no in sshd_config).
      PermitEmptyPasswords = true;
      PasswordAuthentication = true;
    };
  };
  # nix/vm.nix already pins root.password = "root" and
  # root.hashedPassword = lib.mkForce null. Both are mkForce-priority,
  # so use mkOverride 49 (one tighter than mkForce's 50) to flip both
  # to empty for dev-mode passwordless login.
  users.users.root.password = lib.mkOverride 49 "";
  users.users.root.hashedPassword = lib.mkOverride 49 "";

  # sshd uses PAM (UsePAM yes), and pam_unix without `nullok` rejects
  # empty passwords even when the account has one. NixOS exposes
  # `allowNullPassword` per PAM service to add the `nullok` flag — set
  # it on sshd and login so the empty-password dev-mode actually works.
  security.pam.services.sshd.allowNullPassword = true;
  security.pam.services.login.allowNullPassword = true;

  # Diagnostic tooling pre-installed so a freshly SSH'd-in maintainer
  # can immediately strace a hung backend, lsof its UDS, jq through
  # its journal, etc. — without nix-shell round-trips inside the VM.
  environment.systemPackages = with pkgs; [
    strace
    lsof
    htop
    ripgrep
    vim
    less
    tree
    jq
    curl
    procps
  ];

  # Loud motd so anyone who logs in sees the warning before they do
  # anything destructive.
  users.motd = ''

    ============================================================
    !!  vm-dev — DEVELOPMENT SANDBOX ONLY                     !!
    !!  SSH IS OPEN: root login + empty passwords permitted.  !!
    !!  Do NOT copy this configuration into nix/vm.nix.       !!
    ============================================================

  '';

  # Console banner unit — runs early so the warning lands in the
  # qemu serial console before any service drowns it out. Type=oneshot
  # with RemainAfterExit so the unit stays "active" and shows up in
  # `systemctl list-units` as a permanent reminder.
  systemd.services.dev-mode-banner = {
    description = "vm-dev open-SSH dev-mode warning banner";
    wantedBy = [ "multi-user.target" ];
    before = [ "sshd.service" ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      StandardOutput = "journal+console";
      StandardError = "journal+console";
    };
    script = ''
      echo ""
      echo "============================================================"
      echo "!!  vm-dev SSH IS OPEN — root login + empty passwords    !!"
      echo "!!  DEVELOPMENT ONLY — do NOT copy into nix/vm.nix       !!"
      echo "============================================================"
      echo ""
    '';
  };
  # ============================================================

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
