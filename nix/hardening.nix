# Shared systemd sandboxing directives (defense-in-depth).
#
# Single source of truth for the SAFE hardening subset merged into
# every agent-mcp Service block by BOTH modules:
#
#   - nix/home-manager-module.nix  (user-scope units, run under $HOME)
#   - nix/module.nix               (system-scope units, run as a
#                                    dedicated system user)
#
# "Safe" means: verified not to break CPython or the sqlite-vec native
# extension, and not to need a $HOME / state RW carve-out beyond what
# each module already grants. Merge with `// hardening`.
#
#   - NoNewPrivileges: no setuid/setgid escalation from the unit.
#   - RestrictAddressFamilies: only UNIX sockets (backend UDS + system
#     D-Bus) + IPv4/IPv6 (router loopback/TCP + ollama/OIDC egress);
#     blocks AF_PACKET, AF_NETLINK, etc.
#   - RestrictNamespaces / LockPersonality / ProtectKernelTunables /
#     ProtectKernelModules: block namespace creation, personality(2)
#     ADDR_NO_RANDOMIZE, /proc/sys + /sys writes, and module (un)load.
#   - SystemCallArchitectures=native: drop non-native syscall ABIs
#     (a common sandbox-bypass surface).
#   - RestrictSUIDSGID: block creation of setuid/setgid files.
#   - ProtectControlGroups: /sys/fs/cgroup read-only to the unit.
#   - ProtectHostname: sethostname()/setdomainname() blocked.
#   - ProtectClock: block wall-clock writes (settimeofday, adjtime).
#   - RestrictRealtime: no SCHED_FIFO/RR realtime scheduling.
#   - ProtectProc=invisible + ProcSubset=pid: hide other processes'
#     /proc entries and non-pid /proc files from the unit.
#   - PrivateTmp: private /tmp + /var/tmp (no $HOME impact; CPython
#     tempfiles and sqlite temp files land in the private tmpfs).
#
# DELIBERATELY OMITTED everywhere (do NOT "helpfully" add these — they
# crash the units, hence the signpost):
#   - MemoryDenyWriteExecute: CPython and the sqlite-vec native
#     extension need W+X / executable mappings (dlopen, ctypes
#     trampolines); enabling it SIGSEGVs the backend at import.
#   - SystemCallFilter: sqlite-vec makes syscalls outside any
#     conservative allow-list; a filter kills the backend on load.
#   - ProtectSystem="strict": stays out of the user-scope path (those
#     units need $HOME RW). system-mode (module.nix) DOES add it, but
#     only alongside explicit ReadWritePaths for its state/runtime
#     dirs — see nix/module.nix.
{
  NoNewPrivileges = true;
  RestrictAddressFamilies = [ "AF_UNIX" "AF_INET" "AF_INET6" ];
  RestrictNamespaces = true;
  LockPersonality = true;
  ProtectKernelTunables = true;
  ProtectKernelModules = true;
  SystemCallArchitectures = "native";
  RestrictSUIDSGID = true;
  ProtectControlGroups = true;
  ProtectHostname = true;
  ProtectClock = true;
  RestrictRealtime = true;
  ProtectProc = "invisible";
  ProcSubset = "pid";
  PrivateTmp = true;
}
