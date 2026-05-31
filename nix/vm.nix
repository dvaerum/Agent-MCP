{ config, lib, pkgs, modulesPath, src ? null, mode ? "multi", autoProject ? "e2e", ... }:
# NixOS configuration consumed by `nixos/lib/eval-config.nix`. The
# flake builds one derivation per mode (`multi`, `single`); ephemeral
# vs persistent storage is a host-side wrapper concern — both flavours
# bind a host directory at /persist via 9p, the wrapper either points
# it at the user's persistent dir or at a fresh mktemp.
#
# `src` comes from the flake; passed via the flake's `specialArgs`.

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

  # State lives on the qcow2 scratch disk (real ext4) under
  # /var/lib/agent-mcp — 9p doesn't fcntl-lock correctly so SQLite
  # WAL fails on it, which broke the original /persist-via-9p
  # design. The wrapper script keeps the qcow2 in the user's
  # persist dir so it survives reboots without us paying the
  # 9p compatibility tax.

  # ── Ollama (local embedding endpoint) ──────────────────────────
  # qwen3-embedding:0.6b is ~620 MB; downloaded on first boot.
  services.ollama = {
    enable = true;
    host = "127.0.0.1";
    port = 11434;
    loadModels = [ "qwen3-embedding:0.6b" ];
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
    # No sharedDirectories — all state lives on the qcow2 disk.
    # The wrapper script controls the qcow2 path via NIX_DISK_IMAGE.
  };
}
