#!/usr/bin/env bash
# Regression guard: the home-manager module builds *everything it still
# builds itself* from one package set, and nothing is spliced onto the
# result post-hoc.
#
# Background
# ----------
#
# `services.agent-mcp.package` let an operator swap the agent-mcp Python
# derivation. It did nothing. The module applied it by splicing the
# attribute onto the result of `nix/packages.nix`:
#
#     resolvedPkgs =
#       if cfg.package == null then pkgs'
#       else pkgs' // { agentMcpPy = cfg.package; };
#
# By the time `packages.nix` returned, it had already built
# `agentMcpRouterWrapper`, `agentMcpBackendWrapper`,
# `agentMcpLauncher` and the daemon-agent wrapper around its *own*
# `agentMcpPy` and `python` — each wrapper baked in
# `${python}/bin/python` and a PYTHONPATH computed from the internal
# tree. Replacing the attribute afterwards changed something nothing
# downstream read, so every systemd unit kept exec'ing the
# internally-built tree. The knob lied, silently, for as long as it
# existed.
#
# The option is gone (`mkRemovedOptionModule`) and
# `services.agent-mcp.pkgs` — a whole package SET — took its place.
#
# Retirement note (this check, current shape)
# ------------------------------------------------
#
# The Python implementation this bug was originally about — `agentMcpPy`,
# its interpreter, and the PYTHONPATH-coupled wrappers around it — was
# deleted wholesale together with the rest of the Python source tree; the
# Rust `rust/` workspace (packaged via `nix/conexus.nix`, wired in as
# `conexusLauncherPackage`/`conexusRouterPackage`/
# `conexusDaemonAgentPackage`) replaced it. Those three options are
# externally-supplied packages (`nullOr package`) this module never
# builds itself — they do NOT move with `services.agent-mcp.pkgs` from
# THIS module's own point of view (the flake's own `homeModules.default`
# wrapper is what threads `cfg.pkgs` into building them, one layer up —
# see `nix/tests/checks/single-source-of-truth.sh`-adjacent coverage for
# that wiring). What `cfg.pkgs` still governs *inside this module* is
# `nix/packages.nix`'s remaining two derivations: the dashboard
# (Next.js/npm) and the daemon-agent PreCompact hook (bash/curl/jq).
#
# So the guard now has three tiers:
#
# * source-structural checks, which run everywhere, pin the shape: one
#   import of `packages.nix`, fed from `cfg.pkgs`, with nothing
#   spliced onto the result, and no re-introduced Python coupling;
# * a real `nix eval` (skipped where nix is unavailable) proves
#   `cfg.pkgs` actually reaches the dashboard build's output path, not
#   just an unused attribute;
# * the same eval proves the conexus units are correctly INDEPENDENT of
#   `cfg.pkgs` in this module's own scope — the new, deliberate shape,
#   not a regression of the old bug.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
NIX_DIR="$REPO_ROOT/nix"
MODULE="$NIX_DIR/home-manager-module.nix"
PACKAGES="$NIX_DIR/packages.nix"
HARNESS="$NIX_DIR/tests/eval-home-manager-module.nix"

fail=0

# _code(): Nix source with comments stripped.
#
# The module deliberately *documents* the broken override shape it
# replaced, so these guards have to look at code rather than prose —
# otherwise explaining the bug would trip the check for it.
#
# Mirrors the Python regex r"(?m)(?:(?<=\s)|^)#.*$" -> "": strip from a
# '#' that is either at line-start or preceded by whitespace, to EOL,
# while leaving that leading whitespace/start untouched.
strip_comments() {
  sed -E 's/(^|[[:space:]])#.*/\1/' "$1"
}

MODULE_CODE="$(strip_comments "$MODULE")"
PACKAGES_CODE="$(strip_comments "$PACKAGES")"

# _braced_block(): return the `{ ... }` block that follows `opener`.
braced_block() {
  local text="$1" opener="$2"
  awk -v opener="$opener" '
  BEGIN { RS = "\x01"; }
  {
    start = index($0, opener)
    if (start == 0) { print "___OPENER_NOT_FOUND___"; exit 1 }
    brace_start = start + length(opener) - 1
    # opener already ends in "{" for our call sites below.
    depth = 1
    i = brace_start + 1
    n = length($0)
    while (depth > 0 && i <= n) {
      c = substr($0, i, 1)
      if (c == "{") depth++
      else if (c == "}") depth--
      i++
    }
    print substr($0, brace_start + 1, i - brace_start - 2)
  }' <<< "$text"
}

# ── Source-structural guards ──────────────────────────────────────────

# test_packages_nix_is_imported_once_from_the_option:
# The module's single `packages.nix` import is fed from `cfg.pkgs`.
#
# This is what makes `services.agent-mcp.pkgs` mean anything: the
# package set goes *in*, before any derivation is built, rather than
# being patched onto the results.
import_count=$(grep -o 'import \./packages\.nix' <<<"$MODULE_CODE" | wc -l)
if [ "$import_count" -ne 1 ]; then
  echo "FAIL: nix/home-manager-module.nix must import nix/packages.nix exactly " \
    "once — a second import is a second package set, and the two would " \
    "diverge exactly the way \`package\` diverged from the wrappers. " \
    "Found $import_count occurrence(s)." >&2
  fail=1
else
  import_block="$(braced_block "$MODULE_CODE" "import ./packages.nix {")"

  if ! grep -qF 'pkgs = cfg.pkgs;' <<<"$import_block"; then
    echo "FAIL: the packages.nix import must take its package set from " \
      "\`cfg.pkgs\` (the services.agent-mcp.pkgs option), not from the " \
      "module argument — otherwise the option is inert. Import args were:" >&2
    echo "$import_block" >&2
    fail=1
  fi

  if ! grep -qF 'lib = cfg.pkgs.lib;' <<<"$import_block"; then
    echo "FAIL: \`lib\` must come from the same set as \`pkgs\`, so the import is " \
      "single-sourced rather than half consumer-set and half " \
      "home-manager's extended lib. Import args were:" >&2
    echo "$import_block" >&2
    fail=1
  fi

  if grep -qF 'inherit pkgs' <<<"$import_block"; then
    echo "FAIL: \`inherit pkgs\` in the packages.nix import pins the build to the " \
      "consumer's channel and makes services.agent-mcp.pkgs a no-op." >&2
    fail=1
  fi
fi

# test_nothing_is_spliced_onto_the_built_package_set:
# No `pkgs' // { ... }` — that override shape cannot work.
#
# `packages.nix` closes over its own package set while building its
# derivations, so any attribute added to its return value afterwards
# is read by nothing.
if grep -qP "pkgs'\s*//" <<<"$MODULE_CODE"; then
  echo "FAIL: an attribute spliced onto the result of nix/packages.nix is " \
    "invisible to anything already built inside it. Thread the " \
    "change through the import instead (see the \`pkgs\` option)." >&2
  fail=1
fi

if grep -qF 'resolvedPkgs' <<<"$MODULE_CODE"; then
  echo "FAIL: \`resolvedPkgs\` was the name of the ineffective override branch; " \
    "its return means the post-hoc splice is back." >&2
  fail=1
fi

# test_single_derivation_package_option_stays_removed:
# `services.agent-mcp.package` must not come back.
#
# A lone derivation cannot carry the interpreter or the site-packages
# layout a Python wrapper would have needed — the reason the option
# could only ever be a no-op or a broken mixed closure back when this
# module built a Python application. Operators get a migration
# message pointing at `services.agent-mcp.pkgs` instead.
if ! grep -qF 'mkRemovedOptionModule [ "services" "agent-mcp" "package" ]' <<<"$MODULE_CODE"; then
  echo "FAIL: the removal shim is what turns a stale \`services.agent-mcp.package " \
    "= ...\` in a consumer's config into an actionable eval error instead " \
    "of a silent no-op. Keep it." >&2
  fail=1
fi

# Top-level options sit at four spaces; `dashboard.package` at six.
if grep -qP '^    package = lib\.mkOption' <<<"$MODULE_CODE"; then
  echo "FAIL: services.agent-mcp.package is removed on purpose. If a " \
    "per-derivation override is genuinely needed, it has to thread " \
    "through nix/packages.nix together with whatever else it's " \
    "coupled to." >&2
  fail=1
fi

# test_packages_nix_has_no_python_coupling:
# packages.nix must not regain a Python interpreter/application.
#
# The whole class of bug this file guards against (an override that
# reaches an attribute but not the wrappers built around it) only
# exists when packages.nix builds something with an interpreter +
# PYTHONPATH baked into sibling wrappers. Pin the current, simpler
# shape — a Next.js dashboard and a bash/curl/jq PreCompact hook,
# nothing Python-coupled — so a future PR can't silently reintroduce
# that whole risk surface without this check noticing.
for needle in "buildPythonApplication" "agentMcpPy" "PYTHONPATH" "python.pkgs" "pkgs.python3"; do
  if grep -qF "$needle" <<<"$PACKAGES_CODE"; then
    echo "FAIL: packages.nix contains '$needle' — the Python implementation " \
      "was retired; a package needing an interpreter/PYTHONPATH " \
      "reintroduces the exact wrapper-override risk this check " \
      "exists to catch. If this is intentional (a NEW Python-coupled " \
      "derivation), this check needs a deliberate update alongside it, " \
      "not a silent pass." >&2
    fail=1
  fi
done

if [ "$fail" -ne 0 ]; then
  exit 1
fi

# ── Real evaluation: cfg.pkgs must reach the dashboard, and ONLY that ──

if ! command -v nix >/dev/null 2>&1; then
  echo "SKIP: nix is not available on PATH (real-evaluation tier skipped)"
  echo "PASS: module-package-set (structural tier only)"
  exit 0
fi

HARNESS_DRIVER=$(cat <<EOF
let
  repo = builtins.getFlake "$REPO_ROOT";
  base = repo.inputs.nixpkgs.legacyPackages.\${builtins.currentSystem};
  harness = import $HARNESS;
  run = modulePkgs: harness { pkgs = base; src = "$REPO_ROOT"; inherit modulePkgs; };
  pick = h: {
    inherit (h) routerExecStart backendExecStart dashboardOut homePackages;
  };
  # Same nixpkgs, different default nodejs: a minimal, offline stand-in
  # for "a package set from another channel" that the dashboard's
  # buildNpmPackage actually depends on (unlike python3, which nothing
  # left in packages.nix reads any more). Pick whichever non-default
  # nodejs_NN attribute the pinned nixpkgs still carries -- major LTS
  # lines get removed on EOL (nodejs_20 was, 2026-04-30), so hardcoding
  # one specific attr name here would eventually bit-rot this fixture.
  altNodejs = base.nodejs_22 or base.nodejs_18 or base.nodejs_24;
in {
  # Option left unset — must be byte-identical to the explicit default.
  unset = pick (run null);
  explicit = pick (run base);
  swapped = pick (run (base.extend (_final: prev: { nodejs = altNodejs; })));
}
EOF
)

stderr_file="$(mktemp)"
trap 'rm -f "$stderr_file"' EXIT

module_eval=$(NIX_CONFIG="experimental-features = nix-command flakes" \
  timeout 1800 nix eval --impure --json --expr "$HARNESS_DRIVER" 2>"$stderr_file") \
  || { echo "FAIL: nix eval failed:" >&2; cat "$stderr_file" >&2; exit 1; }

# test_unset_option_is_exactly_todays_behaviour:
# Leaving `services.agent-mcp.pkgs` unset changes nothing.
#
# The option is additive: its default is the module's own `pkgs`, so
# an unset config and an explicitly-passed consumer set must produce
# identical store paths.
if ! jq -e '.unset == .explicit' <<<"$module_eval" >/dev/null; then
  echo "FAIL: leaving services.agent-mcp.pkgs unset must be byte-identical to " \
    "explicitly passing the module's own pkgs; got a difference:" >&2
  jq '{unset, explicit}' <<<"$module_eval" >&2
  exit 1
fi

# test_override_reaches_the_dashboard:
# The dashboard's store path follows `services.agent-mcp.pkgs`.
#
# This is the assertion the old `package` option would have failed
# for the Python application it used to build; the dashboard is the
# one remaining derivation in packages.nix this module's own `pkgs`
# option still governs, so it carries the guard now.
unset_dashboard=$(jq -r '.unset.dashboardOut' <<<"$module_eval")
swapped_dashboard=$(jq -r '.swapped.dashboardOut' <<<"$module_eval")
if [ "$unset_dashboard" = "$swapped_dashboard" ]; then
  echo "FAIL: swapping nodejs in the configured package set did not change " \
    "the dashboard's store path — services.agent-mcp.pkgs stopped " \
    "reaching agentMcpDashboard's buildNpmPackage call" >&2
  exit 1
fi

home_packages_count=$(jq '.unset.homePackages | length' <<<"$module_eval")
if [ "$home_packages_count" -eq 0 ]; then
  echo "FAIL: sanity: the fixture's daemonAgents entry should still install " \
    "at least the stub conexus-daemon-agent + the PreCompact hook" >&2
  exit 1
fi

# test_backend_exec_start_is_independent_of_pkgs_in_this_module:
# `conexus@`'s ExecStart does NOT move with `cfg.pkgs`.
#
# Deliberate, not a regression: `conexusLauncherPackage` is an
# externally-supplied package this module never builds itself (see
# its own doc in home-manager-module.nix) — the flake's own
# `homeModules.default` wrapper is what threads `cfg.pkgs` into
# building it, one layer above this module. Pin that boundary so a
# future change doesn't quietly start rebuilding the Rust binary
# inside this module (which would need `crane`, an input this module
# deliberately has no access to). `conexus-router`'s own ExecStart is
# NOT checked here — unlike the backend, it legitimately embeds
# `cfg.dashboard.package`'s store path via `--dashboard-dir`, so it
# IS expected to move with `cfg.pkgs` (see the next check).
unset_backend_exec=$(jq -r '.unset.backendExecStart' <<<"$module_eval")
swapped_backend_exec=$(jq -r '.swapped.backendExecStart' <<<"$module_eval")
if [ "$unset_backend_exec" != "$swapped_backend_exec" ]; then
  echo "FAIL: backendExecStart changed when only \`services.agent-mcp.pkgs\` " \
    "moved — conexus@ should be entirely determined by " \
    "conexusLauncherPackage (stubbed identically in both harness " \
    "runs), not by \`cfg.pkgs\`." >&2
  exit 1
fi

# test_router_exec_start_tracks_pkgs_via_the_dashboard_only:
# `conexus-router`'s ExecStart moves with `cfg.pkgs` ONLY through
# the `--dashboard-dir` flag's dashboard store path — the router
# binary itself (`conexusRouterPackage`, stubbed identically in both
# harness runs) does not move.
unset_router_exec=$(jq -r '.unset.routerExecStart' <<<"$module_eval")
swapped_router_exec=$(jq -r '.swapped.routerExecStart' <<<"$module_eval")
unset_router_binary="${unset_router_exec%% *}"
swapped_router_binary="${swapped_router_exec%% *}"

if [ "$unset_router_binary" != "$swapped_router_binary" ]; then
  echo "FAIL: the conexus-router BINARY path changed when only " \
    "\`services.agent-mcp.pkgs\` moved — it should be entirely " \
    "determined by conexusRouterPackage, not by \`cfg.pkgs\`." >&2
  exit 1
fi

if [ "$unset_router_exec" = "$swapped_router_exec" ]; then
  echo "FAIL: conexus-router's ExecStart did not change at all when " \
    "\`cfg.pkgs\` moved — it should embed cfg.dashboard.package's " \
    "store path via --dashboard-dir, which DOES track \`cfg.pkgs\` " \
    "(see test_override_reaches_the_dashboard)." >&2
  exit 1
fi

echo "PASS: module-package-set"
