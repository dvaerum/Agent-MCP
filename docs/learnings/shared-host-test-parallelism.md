# Running `pytest -n auto` on a shared dev box

Notes from the pentest-all security loop, where 2-3 fix agents ran
their local test suites concurrently in separate git worktrees on the
same machine other unrelated sessions were also using.

## `-n auto` sizes to the machine, not to the work

`pyproject.toml` sets `addopts = "-n auto"` — one xdist worker per CPU
core, unconditionally. That's the right default for a solo full-suite
run: more workers finish sooner. It stops being free the moment more
than one agent runs pytest on the same box at the same time, and it is
*especially* wasteful for a small targeted subset — a 7-file debug run
still paid for 16 workers (~270MB RSS each just to boot the app stack),
because `-n auto` counts cores, not collected tests.

Measured impact of 2 concurrent full-suite invocations plus repeated
small targeted re-runs, all defaulting to `-n auto` on a 16-core box:
15-minute load average `31.36` (≈2x core count) and 18GB in swap with
236MB RAM free. That degrades every other session on the host, not
just the two doing the testing.

## Fix: override worker count per-invocation, don't touch the default

`-n auto` stays the right *default* — most invocations are solo. The
fix is at the call site, not the config:

* A **small/targeted** subset (a handful of files while iterating on a
  fix) — pass `-n 2` or `-p no:xdist` explicitly. Worker-boot overhead
  dominates at that scale anyway; parallelism buys nothing.
* A **long full-suite run competing with other concurrent work** —
  `-n 4` instead of `-n auto`. Slower wall-clock for that one run, but
  bounded memory/CPU footprint instead of claiming every core.
* The **final, one-time full-suite confirmation** before opening a
  PR — worth paying for real `-n auto`, since correctness there
  matters more than host courtesy for that one run.

Verified this trades nothing away: a full run under `-n 4` still came
back clean (3925 passed, 0 failed, ~12.5 min) with no coverage lost —
it just used a fraction of the concurrent memory a `-n auto` run would
have while sharing the box.

Don't change `addopts` itself to fix this — that would slow down every
solo run (CI included) to fix a problem that only exists when multiple
suites overlap. The override belongs on the command line of whichever
invocation is actually sharing the box.
