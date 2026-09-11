"""Regression guard: the home-manager module builds *everything it still
builds itself* from one package set, and nothing is spliced onto the
result post-hoc.

Background
----------

``services.agent-mcp.package`` let an operator swap the agent-mcp Python
derivation. It did nothing. The module applied it by splicing the
attribute onto the result of ``nix/packages.nix``::

    resolvedPkgs =
      if cfg.package == null then pkgs'
      else pkgs' // { agentMcpPy = cfg.package; };

By the time ``packages.nix`` returned, it had already built
``agentMcpRouterWrapper``, ``agentMcpBackendWrapper``,
``agentMcpLauncher`` and the daemon-agent wrapper around its *own*
``agentMcpPy`` and ``python`` — each wrapper baked in
``${python}/bin/python`` and a PYTHONPATH computed from the internal
tree. Replacing the attribute afterwards changed something nothing
downstream read, so every systemd unit kept exec'ing the
internally-built tree. The knob lied, silently, for as long as it
existed.

The option is gone (``mkRemovedOptionModule``) and
``services.agent-mcp.pkgs`` — a whole package SET — took its place.

Retirement note (this test file, current shape)
------------------------------------------------

The Python implementation this bug was originally about — ``agentMcpPy``,
its interpreter, and the PYTHONPATH-coupled wrappers around it — was
deleted wholesale together with the rest of the Python source tree; the
Rust ``rust/`` workspace (packaged via ``nix/conexus.nix``, wired in as
``conexusLauncherPackage``/``conexusRouterPackage``/
``conexusDaemonAgentPackage``) replaced it. Those three options are
externally-supplied packages (``nullOr package``) this module never
builds itself — they do NOT move with ``services.agent-mcp.pkgs`` from
THIS module's own point of view (the flake's own ``homeModules.default``
wrapper is what threads ``cfg.pkgs`` into building them, one layer up —
see ``tests/test_nix_single_source_of_truth.py``-adjacent coverage for
that wiring). What ``cfg.pkgs`` still governs *inside this module* is
``nix/packages.nix``'s remaining two derivations: the dashboard
(Next.js/npm) and the daemon-agent PreCompact hook (bash/curl/jq).

So the guard now has three tiers:

* source-structural tests, which run everywhere, pin the shape: one
  import of ``packages.nix``, fed from ``cfg.pkgs``, with nothing
  spliced onto the result, and no re-introduced Python coupling;
* a real ``nix eval`` (skipped where nix is unavailable) proves
  ``cfg.pkgs`` actually reaches the dashboard build's output path, not
  just an unused attribute;
* the same eval proves the conexus units are correctly INDEPENDENT of
  ``cfg.pkgs`` in this module's own scope — the new, deliberate shape,
  not a regression of the old bug.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
NIX_DIR = REPO_ROOT / "nix"
MODULE = NIX_DIR / "home-manager-module.nix"
PACKAGES = NIX_DIR / "packages.nix"
HARNESS = NIX_DIR / "tests" / "eval-home-manager-module.nix"


def _code(path: Path) -> str:
    """Nix source with comments stripped.

    The module deliberately *documents* the broken override shape it
    replaced, so these guards have to look at code rather than prose —
    otherwise explaining the bug would trip the test for it.
    """
    return re.sub(r"(?m)(?:(?<=\s)|^)#.*$", "", path.read_text())


def _braced_block(text: str, opener: str) -> str:
    """Return the ``{ … }`` block that follows ``opener`` in ``text``."""
    start = text.index(opener) + len(opener)
    depth = 1
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start:i]
    raise AssertionError(f"unbalanced braces after {opener!r}")


# ── Source-structural guards ──────────────────────────────────────────


def test_packages_nix_is_imported_once_from_the_option() -> None:
    """The module's single ``packages.nix`` import is fed from ``cfg.pkgs``.

    This is what makes ``services.agent-mcp.pkgs`` mean anything: the
    package set goes *in*, before any derivation is built, rather than
    being patched onto the results.
    """
    text = _code(MODULE)

    assert text.count("import ./packages.nix") == 1, (
        "nix/home-manager-module.nix must import nix/packages.nix exactly "
        "once — a second import is a second package set, and the two would "
        "diverge exactly the way `package` diverged from the wrappers."
    )

    block = _braced_block(text, "import ./packages.nix {")
    assert "pkgs = cfg.pkgs;" in block, (
        "the packages.nix import must take its package set from "
        "`cfg.pkgs` (the services.agent-mcp.pkgs option), not from the "
        "module argument — otherwise the option is inert. Import args "
        "were:\n" + block
    )
    assert "lib = cfg.pkgs.lib;" in block, (
        "`lib` must come from the same set as `pkgs`, so the import is "
        "single-sourced rather than half consumer-set and half "
        "home-manager's extended lib. Import args were:\n" + block
    )
    assert "inherit pkgs" not in block, (
        "`inherit pkgs` in the packages.nix import pins the build to the "
        "consumer's channel and makes services.agent-mcp.pkgs a no-op."
    )


def test_nothing_is_spliced_onto_the_built_package_set() -> None:
    """No ``pkgs' // { … }`` — that override shape cannot work.

    ``packages.nix`` closes over its own package set while building its
    derivations, so any attribute added to its return value afterwards
    is read by nothing.
    """
    text = _code(MODULE)

    assert re.search(r"pkgs'\s*//", text) is None, (
        "an attribute spliced onto the result of nix/packages.nix is "
        "invisible to anything already built inside it. Thread the "
        "change through the import instead (see the `pkgs` option)."
    )
    assert "resolvedPkgs" not in text, (
        "`resolvedPkgs` was the name of the ineffective override branch; "
        "its return means the post-hoc splice is back."
    )


def test_single_derivation_package_option_stays_removed() -> None:
    """``services.agent-mcp.package`` must not come back.

    A lone derivation cannot carry the interpreter or the site-packages
    layout a Python wrapper would have needed — the reason the option
    could only ever be a no-op or a broken mixed closure back when this
    module built a Python application. Operators get a migration
    message pointing at ``services.agent-mcp.pkgs`` instead.
    """
    text = _code(MODULE)

    assert 'mkRemovedOptionModule [ "services" "agent-mcp" "package" ]' in text, (
        "the removal shim is what turns a stale `services.agent-mcp.package "
        "= …` in a consumer's config into an actionable eval error instead "
        "of a silent no-op. Keep it."
    )
    # Top-level options sit at four spaces; `dashboard.package` at six.
    assert re.search(r"^    package = lib\.mkOption", text, re.MULTILINE) is None, (
        "services.agent-mcp.package is removed on purpose. If a "
        "per-derivation override is genuinely needed, it has to thread "
        "through nix/packages.nix together with whatever else it's "
        "coupled to."
    )


def test_packages_nix_has_no_python_coupling() -> None:
    """packages.nix must not regain a Python interpreter/application.

    The whole class of bug this file guards against (an override that
    reaches an attribute but not the wrappers built around it) only
    exists when packages.nix builds something with an interpreter +
    PYTHONPATH baked into sibling wrappers. Pin the current, simpler
    shape — a Next.js dashboard and a bash/curl/jq PreCompact hook,
    nothing Python-coupled — so a future PR can't silently reintroduce
    that whole risk surface without this test noticing.
    """
    text = _code(PACKAGES)

    for needle in (
        "buildPythonApplication",
        "agentMcpPy",
        "PYTHONPATH",
        "python.pkgs",
        "pkgs.python3",
    ):
        assert needle not in text, (
            f"packages.nix contains {needle!r} — the Python implementation "
            "was retired; a package needing an interpreter/PYTHONPATH "
            "reintroduces the exact wrapper-override risk this test file "
            "exists to catch. If this is intentional (a NEW Python-coupled "
            "derivation), this test needs a deliberate update alongside it, "
            "not a silent pass."
        )


# ── Real evaluation: cfg.pkgs must reach the dashboard, and ONLY that ──

_HARNESS_DRIVER = """
let
  repo = builtins.getFlake "@REPO@";
  base = repo.inputs.nixpkgs.legacyPackages.${builtins.currentSystem};
  harness = import @HARNESS@;
  run = modulePkgs: harness { pkgs = base; src = "@REPO@"; inherit modulePkgs; };
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
"""


@pytest.fixture(scope="module")
def module_eval() -> dict:
    """Evaluate the home-manager module three ways via ``nix eval``.

    Skipped where nix is unavailable — notably the CI Python matrix,
    which runs on plain ubuntu runners. The structural tests above are
    the always-on gate; this one is the deep proof.
    """
    if shutil.which("nix") is None:
        pytest.skip("nix is not available on PATH")

    proc = subprocess.run(
        [
            "nix",
            "eval",
            "--impure",
            "--json",
            "--expr",
            _HARNESS_DRIVER.replace("@REPO@", str(REPO_ROOT)).replace(
                "@HARNESS@", str(HARNESS)
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=1800,
        env={**os.environ, "NIX_CONFIG": "experimental-features = nix-command flakes"},
    )
    if proc.returncode != 0:
        pytest.fail(f"nix eval failed:\n{proc.stderr}")
    return json.loads(proc.stdout)


def test_unset_option_is_exactly_todays_behaviour(module_eval: dict) -> None:
    """Leaving ``services.agent-mcp.pkgs`` unset changes nothing.

    The option is additive: its default is the module's own ``pkgs``, so
    an unset config and an explicitly-passed consumer set must produce
    identical store paths.
    """
    assert module_eval["unset"] == module_eval["explicit"]


def test_override_reaches_the_dashboard(module_eval: dict) -> None:
    """The dashboard's store path follows ``services.agent-mcp.pkgs``.

    This is the assertion the old ``package`` option would have failed
    for the Python application it used to build; the dashboard is the
    one remaining derivation in packages.nix this module's own ``pkgs``
    option still governs, so it carries the guard now.
    """
    unset = module_eval["unset"]
    swapped = module_eval["swapped"]

    assert unset["dashboardOut"] != swapped["dashboardOut"], (
        "swapping nodejs in the configured package set did not change "
        "the dashboard's store path — services.agent-mcp.pkgs stopped "
        "reaching agentMcpDashboard's buildNpmPackage call"
    )
    assert sorted(unset["homePackages"]) != [] , (
        "sanity: the fixture's daemonAgents entry should still install "
        "at least the stub conexus-daemon-agent + the PreCompact hook"
    )


def test_backend_exec_start_is_independent_of_pkgs_in_this_module(
    module_eval: dict,
) -> None:
    """`conexus@`'s ExecStart does NOT move with ``cfg.pkgs``.

    Deliberate, not a regression: `conexusLauncherPackage` is an
    externally-supplied package this module never builds itself (see
    its own doc in home-manager-module.nix) — the flake's own
    `homeModules.default` wrapper is what threads `cfg.pkgs` into
    building it, one layer above this module. Pin that boundary so a
    future change doesn't quietly start rebuilding the Rust binary
    inside this module (which would need `crane`, an input this module
    deliberately has no access to). `conexus-router`'s own ExecStart is
    NOT checked here — unlike the backend, it legitimately embeds
    `cfg.dashboard.package`'s store path via `--dashboard-dir`, so it
    IS expected to move with `cfg.pkgs` (see the next test).
    """
    unset = module_eval["unset"]
    swapped = module_eval["swapped"]

    assert unset["backendExecStart"] == swapped["backendExecStart"], (
        "backendExecStart changed when only `services.agent-mcp.pkgs` "
        "moved — conexus@ should be entirely determined by "
        "conexusLauncherPackage (stubbed identically in both harness "
        "runs), not by `cfg.pkgs`."
    )


def test_router_exec_start_tracks_pkgs_via_the_dashboard_only(
    module_eval: dict,
) -> None:
    """`conexus-router`'s ExecStart moves with ``cfg.pkgs`` ONLY through
    the `--dashboard-dir` flag's dashboard store path — the router
    binary itself (`conexusRouterPackage`, stubbed identically in both
    harness runs) does not move.
    """
    unset = module_eval["unset"]
    swapped = module_eval["swapped"]

    unset_binary = unset["routerExecStart"].split(" ", 1)[0]
    swapped_binary = swapped["routerExecStart"].split(" ", 1)[0]
    assert unset_binary == swapped_binary, (
        "the conexus-router BINARY path changed when only "
        "`services.agent-mcp.pkgs` moved — it should be entirely "
        "determined by conexusRouterPackage, not by `cfg.pkgs`."
    )
    assert unset["routerExecStart"] != swapped["routerExecStart"], (
        "conexus-router's ExecStart did not change at all when "
        "`cfg.pkgs` moved — it should embed cfg.dashboard.package's "
        "store path via --dashboard-dir, which DOES track `cfg.pkgs` "
        "(see test_override_reaches_the_dashboard)."
    )
