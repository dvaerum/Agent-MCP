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
#   2. AGENT_MCP_BOOTSTRAP_USERNAME / _PASSWORD are seeded on the
#      router systemd unit so the first-boot identity store comes up
#      with a known operator already created (username/password both
#      `dev`). That short-circuits the Phase-1 empty-users redirect
#      to /setup — a developer opening the dashboard lands on /login
#      and can sign in immediately, then create projects via the UI.
#      No HTTP-call-at-boot bootstrap: ADR 0014 removed the legacy
#      `/__create` form-encoded endpoint, and its REST replacement
#      `POST /api/router/projects` requires a session cookie that a
#      systemd oneshot can't have. Operator seeding via env vars is
#      the only post-Phase-1+2 path that works at boot.
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

{
  imports = [
    (import ./vm.nix {
      inherit config lib pkgs modulesPath src;
      mode = "multi";
    })
  ];

  # ── First-boot operator seed ──────────────────────────────────────
  # The Phase-1 empty-users middleware redirects every non-/setup,
  # non-/login request to /setup until at least one operator exists.
  # Seed a sentinel operator (dev / dev) via the env-var bootstrap so
  # the developer can hit /login immediately. See
  # agent_mcp/router/identity.py `init_router_db` for the contract:
  # both vars must be set, both are stripped from os.environ after
  # the bootstrap fires (whether or not it actually created a user),
  # and the bootstrap no-ops when the users table is already populated.
  # Safe for dev-mode only — the loopback-only port + open SSH +
  # empty-password warnings on this VM already mark it as untrusted.
  systemd.services.agent-mcp-router.environment = {
    AGENT_MCP_BOOTSTRAP_USERNAME = "dev";
    AGENT_MCP_BOOTSTRAP_PASSWORD = "dev";
  };

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

  # NOTE: A legacy `agent-mcp-vm-dev-seed.service` used to live here.
  # It waited for `/var/lib/agent-mcp/projects/agent-select-dev/admin_token`
  # to appear and then POSTed `/api/create-agent` with that token as a
  # bearer to pre-seed two worker agents. Both halves of that contract
  # were retired in Phases 1+2: (a) the "all-powerful admin_token" file
  # no longer exists — agent-side auth is per-agent worker/manager
  # tokens minted at create-agent time, and (b) the project itself
  # never got created at boot because the upstream bootstrap unit's
  # `/__create` call was already broken (ADR 0014). The seed unit
  # timed out at 30s on every boot with `admin token not found`.
  # Retired entirely — the operator (seeded via the env-var bootstrap
  # above) creates projects + agents through the dashboard UI, which
  # is the same path a real user takes. verify-all drives that same
  # UI via Firefox-MCP, so we don't need a back-door seed.
}
