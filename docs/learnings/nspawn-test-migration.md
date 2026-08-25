# Migrating a `pkgs.testers.nixosTest` from QEMU to nspawn

`nix/tests/multi-tenant.nix` moved from `nodes.machine` (QEMU VM) to
`containers.machine` (systemd-nspawn), using nixpkgs' native nspawn
test-driver support (`nixos/lib/testing/nodes.nix`, present at the
repo's pinned rev `e5bdc4a41d4c072fe1e3787eaa0320a384741d44` — no input
bump needed). `single-tenant.nix`, `no-auto-cleanup.nix`, and
`event-driven-coord.nix` were deliberately left untouched; see below.

## The diff is smaller than expected

Only two changes were needed:

1. `nodes.machine = { ... }` → `containers.machine = { ... }`. Every
   other NixOS option (`users.users`, `systemd.services`, polkit,
   `environment.systemPackages`, `networking.firewall`) carries over
   unchanged — they're normal module options, not QEMU-specific.
2. Delete `virtualisation.memorySize`/`cores`/`diskSize`. Those three
   options live in `qemu-vm.nix` only; a container shares the host
   kernel and cgroup accounting, so there's nothing to size. Nix
   evaluation fails loudly (`option does not exist`) if you leave
   them, so this isn't something you can silently skip.

`testScript` needed zero changes — the container is exposed to the
test script under the same `machine` variable name as a node would be.

## Why `multi-tenant.nix` and not the others

Picked as the pilot because it's the only one of the 4 VM tests with
neither a hardening-directive assertion (`nix/hardening.nix` merges
`ProtectSystem=strict`-adjacent directives that need real namespace
support QEMU provides and nspawn's manual notes as NOT guaranteed —
see "Virtual machines vs. containers" in the nixpkgs manual) nor a
multi-node topology. `single-tenant.nix` stays on QEMU specifically
for this reason and should not be migrated by extension of this
result.

## What this migration does — and does NOT — prove

The test never actually triggers `agent_mcp/router/app.py`'s
`_ensure()` (the `systemctl start agent-mcp@<name>` lazy-spawn path) —
the dashboard deep-link assertion is served straight from the static
`index.html` without touching the per-project backend. That was
already true under the QEMU version (see the module's own docstring),
and stays true under nspawn. **This migration proves nspawn can boot
full systemd + D-Bus + polkit and serve the router's HTTP surface
under nested Nix-sandbox virtualization — it does not independently
exercise the polkit-authorized `systemctl start` against the
`agent-mcp@` template unit**, because nothing in this test suite does.
If a future test grows a step that actually calls `_ensure`, that
would be the first real proof of the polkit+nspawn interaction and
deserves its own scrutiny.

## Local results

3 consecutive `nix build .#checks.x86_64-linux.vm-multi-tenant
--rebuild` runs, all green, ~5-7s test-script time each (vs. QEMU's
multi-minute VM boot). No flakiness observed locally.
