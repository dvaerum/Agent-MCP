{ config, lib, pkgs, modulesPath, src ? null, mode ? "multi", autoProject ? "e2e", ... }:
# NixOS configuration consumed by `lib.nixosSystem`. The flake builds
# one derivation per mode (`multi`, `single`). Storage is layered:
#
#   - agent-mcp state    → /var/lib/agent-mcp on the qcow2 scratch
#     disk (real ext4; SQLite WAL needs fcntl locks 9p can't fake).
#   - Ollama model blobs → /var/lib/ollama bind-mounted via 9p from
#     `$AGENT_MCP_OLLAMA_DIR` on the host. Plain files, no SQLite,
#     so 9p works fine and the user can wipe disk.qcow2 without
#     redownloading the ~620 MB embedding model.
#
# `src` comes from the flake; passed via `specialArgs`.

let
  inVmHostPort =
    if mode == "multi" then 1337 else 8080;

  realAutoProject =
    if mode == "multi" then autoProject else null;
in
{
  imports = [
    (modulesPath + "/profiles/qemu-guest.nix")
    (modulesPath + "/profiles/minimal.nix")
    (modulesPath + "/virtualisation/qemu-vm.nix")
    ./module.nix
  ];

  # qemu-vm.nix handles fileSystems."/" and bootloader.

  system.stateVersion = "24.11";

  services.getty.autologinUser = "root";
  users.users.root.password = "root";
  users.users.root.hashedPassword = lib.mkForce null;
  services.openssh.enable = false;

  networking.firewall.enable = false;
  networking.useDHCP = false;
  networking.interfaces.eth0.useDHCP = true;
  networking.hostName = "agent-mcp";

  # ── Ollama (local embedding endpoint) ──────────────────────────
  # qwen3-embedding:0.6b is ~620 MB; first-boot download lands in
  # the host-bound /var/lib/ollama and survives qcow2 deletion.
  services.ollama = {
    enable = true;
    host = "127.0.0.1";
    port = 11434;
    loadModels = [ "qwen3-embedding:0.6b" ];
  };

  # Override DynamicUser=yes — systemd's per-service state-dir
  # bind-mount of /var/lib/private/ollama onto /var/lib/ollama
  # collides with our 9p mountpoint (Device or resource busy).
  # Use a static system user instead so ollama writes straight to
  # the 9p share.
  users.users.ollama = {
    isSystemUser = true;
    group = "ollama";
    home = "/var/lib/ollama";
  };
  users.groups.ollama = { };
  systemd.services.ollama.serviceConfig = {
    # security_model=none on the 9p share means raw host UIDs cross
    # the boundary. The host dir is owned by uid 1000 (qemu launcher
    # user); inside the guest the `ollama` user (uid 994) can see
    # but can't write — and the in-guest VFS perm check happens
    # before 9p ever sees the syscall. Run ollama as root + restore
    # CAP_DAC_OVERRIDE so the VFS check is bypassed; qemu still
    # executes the host-side syscall as uid 1000 (which owns the
    # dir) so things actually land. Acceptable in this single-
    # purpose e2e-test sandbox VM.
    DynamicUser = lib.mkForce false;
    User = lib.mkForce "root";
    Group = lib.mkForce "root";
    StateDirectory = lib.mkForce "";
    # Drop nearly all the hardening — it both bind-mounts on top of
    # /var/lib/ollama (collides with our 9p mount) and strips the
    # caps that let root override the in-guest VFS perm check.
    AmbientCapabilities = lib.mkForce [ "CAP_DAC_OVERRIDE" "CAP_DAC_READ_SEARCH" "CAP_CHOWN" "CAP_FOWNER" ];
    CapabilityBoundingSet = lib.mkForce [ "CAP_DAC_OVERRIDE" "CAP_DAC_READ_SEARCH" "CAP_CHOWN" "CAP_FOWNER" "CAP_NET_BIND_SERVICE" ];
    PrivateUsers = lib.mkForce false;
    PrivateTmp = lib.mkForce false;
    ProtectSystem = lib.mkForce false;
    ProtectHome = lib.mkForce false;
    ReadWritePaths = lib.mkForce [ ];
    UMask = lib.mkForce "0000";
  };

  services.agent-mcp = {
    enable = true;
    mode = mode;
    src = src;
    autoProject = realAutoProject;
    externalUrl = "http://localhost:5454";
    # /var/lib lives on the qcow2 disk, which the wrapper places in
    # the user's persist dir so it survives between runs.
    stateDir = "/var/lib/agent-mcp";
  };

  environment.systemPackages = with pkgs; [ curl jq htop vim ];

  virtualisation = {
    memorySize = 4096;
    cores = 2;
    diskSize = 8192;
    graphics = false;
    forwardPorts = [
      { from = "host"; host.address = "127.0.0.1"; host.port = 5454;
        guest.port = inVmHostPort; }
    ];
    # 9p source path is a shell var the wrapper sets just before
    # exec'ing this VM's run-script; "$AGENT_MCP_OLLAMA_DIR" is the
    # host-side directory (typically ./vm-persistent-data/ollama/).
    # security_model=none avoids the xattr-based UID translation that
    # made mapped-xattr break ollama's downloads — we don't actually
    # need UID mapping here (the bind-mount + chmod 0777 inside the
    # guest sorts permissions).
    sharedDirectories.ollama-models = {
      source = "\"$AGENT_MCP_OLLAMA_DIR\"";
      target = "/var/lib/ollama";
      securityModel = "none";
    };
  };
}
