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

## A second, worse bottleneck: the shared host's Ollama backend has `-np 1`

CPU/RAM contention (above) degrades gracefully — more workers just get
slower. The host's Ollama `llama-server` (used by RAG/embedding tests)
does not: it's launched with `-np 1`, hard single-request concurrency,
so every concurrent test suite's embedding calls pile into one strict
FIFO queue regardless of how many pytest workers each suite has.

Observed running 4 fix agents' full suites concurrently (pentest-all
round 21, alongside the live vm-dev VM's own RAG traffic and a
completeness-review agent also hitting the same Ollama instance): the
connection count to `:11434` fluctuated between 15 and 72 concurrent
callers, and individual test suites sat blocked in `epoll_wait` on an
established Ollama connection for minutes at a time with zero CPU
movement — genuinely progressing, not hung, just queued. Total elapsed
time for all 4 suites to finish was well over 2 hours; a normal solo
full-suite run on this repo is ~10-15 minutes.

**This means concurrent full-suite runs can be net SLOWER than running
them one at a time**, not just less courteous — unlike the CPU/RAM
case where more concurrency at least keeps making progress, N suites
fighting over one Ollama request slot make each other wait rather than
share throughput. If you know ahead of time that several fix agents'
suites will all exercise RAG/embedding tests, prefer running their
full-suite gates **sequentially** (one worktree's suite completes and
opens its PR before the next starts) rather than dispatching all of
them in parallel and letting them queue.

How to tell this apart from a genuine hang while it's happening:
`ss -tnp | grep :11434` on the suspect PID — an `ESTAB` connection to
the local Ollama port that keeps existing (or gets replaced by a fresh
one on a new ephemeral port) across repeated checks is the queue, not
a hang. `ps aux | grep ollama` should also still show the
`llama-server` process burning real CPU — if it's also idle, that's a
different, actual problem.
