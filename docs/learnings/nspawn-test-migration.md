# Migrating a `pkgs.testers.nixosTest` from QEMU to nspawn

## Final state (all 4 VM suites)

| Suite | Backend | Why |
|---|---|---|
| `multi-tenant.nix` | nspawn (#744) | Pilot — no hardening assertions, no multi-node |
| `no-auto-cleanup.nix` | nspawn (#745) | Same shape as the pilot |
| `event-driven-coord.nix` | nspawn (#746) | Same shape as the pilot |
| `single-tenant.nix` | **QEMU (unchanged)** | Asserts systemd hardening directives at runtime — see below |

3 of 4 moved to `containers.machine` using nixpkgs' native nspawn
test-driver support (`nixos/lib/testing/nodes.nix`, present at the
repo's pinned rev `e5bdc4a41d4c072fe1e3787eaa0320a384741d44` — no input
bump needed). `single-tenant.nix` is a deliberate, permanent holdout,
not a not-yet-done item — see "Why `multi-tenant.nix` and not the
others" below; that reasoning applies unchanged to all three
migrations, not just the first.

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

## What the pilot alone did — and did NOT — prove

`multi-tenant.nix` never actually triggers `agent_mcp/router/app.py`'s
`_ensure()` (the `systemctl start agent-mcp@<name>` lazy-spawn path) —
the dashboard deep-link assertion is served straight from the static
`index.html` without touching the per-project backend. That was
already true under the QEMU version (see the module's own docstring),
and stays true under nspawn. The pilot alone proved nspawn can boot
full systemd + D-Bus + polkit and serve the router's HTTP surface
under nested Nix-sandbox virtualization — it did NOT independently
exercise the polkit-authorized `systemctl start` against the
`agent-mcp@` template unit, because nothing in that one test suite
does.

**The other two migrations closed that gap.** `event-driven-coord.nix`
calls `systemctl start agent-mcp@coord-test.service` directly (and
later `restart`), and `no-auto-cleanup.nix` drives the router's real
`_ensure()` lazy-spawn path over HTTP (its own comment: "thus fires
the lazy-spawn — for an AUTHORIZED caller"). Both went green on real
GitHub Actions CI across 2+ independent runs — the polkit-authorized,
`DynamicUser`-adjacent `systemctl start` against a template unit
**does work nested inside both the Nix build sandbox and an nspawn
container**. That was the open empirical question this whole spike
existed to answer, and it's now answered for this repo's actual usage
pattern, not just in the abstract.

## Why `single-tenant.nix` stays on QEMU

Unlike the other three, `single-tenant.nix` asserts systemd hardening
directives at runtime (`systemctl show -p PrivateDevices` /
`ProtectKernelLogs` / `RemoveIPC` / `CapabilityBoundingSet` / `UMask`,
sourced from `nix/hardening.nix`'s shared subset) — this is the
regression guard that catches a future edit to that file silently
weakening production hardening. The nixpkgs manual's "Virtual machines
vs. containers" section documents several hardening options as not
guaranteed under nspawn's shared-kernel model. Migrating this one
would risk the guard passing without the kernel actually enforcing
what it claims to — so it isn't a "not yet migrated," it's a
deliberate, permanent exception. If nspawn's hardening-directive
fidelity is ever independently verified (not assumed), this doc is
the place to record that and revisit the decision.

## Local + CI results

- `multi-tenant.nix`: 3 consecutive `nix build --rebuild` runs, all
  green locally, ~5-7s test-script time each (vs. QEMU's multi-minute
  VM boot).
- `no-auto-cleanup.nix`: 2 consecutive local runs green; CI green
  across the merge run.
- `event-driven-coord.nix`: 2 consecutive local runs green (~16s
  test-script time); CI green across the merge run.

No flakiness observed on any of the three, locally or on GitHub
Actions, across the full migration.
