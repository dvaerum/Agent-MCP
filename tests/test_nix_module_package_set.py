"""Regression guard: the home-manager module builds *everything* from one
package set, and an override of that set reaches the WRAPPERS.

Background
----------

``services.agent-mcp.package`` let an operator swap the agent-mcp Python
derivation. It did nothing. The module applied it by splicing the
attribute onto the result of ``nix/packages.nix``::

    resolvedPkgs =
      if cfg.package == null then pkgs'
      else pkgs' // { agentMcpPy = cfg.package; };

By the time ``packages.nix`` returns, it has already built
``agentMcpRouterWrapper``, ``agentMcpBackendWrapper``,
``agentMcpLauncher`` and the daemon-agent wrapper around its *own*
``agentMcpPy`` and ``python`` — each wrapper bakes in
``${python}/bin/python`` and a PYTHONPATH computed from the internal
tree. Replacing the attribute afterwards changed something nothing
downstream reads, so every systemd unit kept exec'ing the
internally-built tree. The knob lied, silently, for as long as it
existed.

The option is gone (``mkRemovedOptionModule``) and
``services.agent-mcp.pkgs`` — a whole package SET — took its place, so
application, interpreter, wrappers and dashboard move together or not
at all.

Two layers of guard, because the bug's whole nature was being invisible
to the obvious test:

* source-structural tests, which run everywhere, pin the shape: one
  import of ``packages.nix``, fed from ``cfg.pkgs``, with nothing
  spliced onto the result;
* a real ``nix eval`` (skipped where nix is unavailable, e.g. the CI
  Python matrix) reads the generated wrapper *text* and asserts the
  interpreter and the site-packages paths inside it actually follow the
  option. A test that compared only the overridden attribute would have
  passed against the original bug.
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

    ``packages.nix`` closes over its own ``agentMcpPy`` and ``python``
    while building the wrappers, so any attribute added to its return
    value is read by nothing.
    """
    text = _code(MODULE)

    assert re.search(r"pkgs'\s*//", text) is None, (
        "an attribute spliced onto the result of nix/packages.nix is "
        "invisible to the wrappers that were already built inside it. "
        "Thread the change through the import instead (see the `pkgs` "
        "option)."
    )
    assert "resolvedPkgs" not in text, (
        "`resolvedPkgs` was the name of the ineffective override branch; "
        "its return means the post-hoc splice is back."
    )


def test_single_derivation_package_option_stays_removed() -> None:
    """``services.agent-mcp.package`` must not come back.

    A lone derivation cannot carry the interpreter or the site-packages
    layout the wrappers need — ``pythonModule`` is absent on
    ``buildPythonApplication`` results — so the option could only ever
    be a no-op or a broken mixed closure. Operators get a migration
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
        "through nix/packages.nix together with its interpreter."
    )


def test_wrappers_and_interpreter_share_one_binding() -> None:
    """Every wrapper in ``packages.nix`` execs the set's one interpreter.

    This is the invariant that makes a package-set override coherent and
    a single-derivation override impossible: there is exactly one
    ``python``, it comes from the passed-in ``pkgs``, and every wrapper
    plus every PYTHONPATH is derived from it.
    """
    text = _code(PACKAGES)

    interpreters = re.findall(r"^\s*python = (.+);$", text, re.MULTILINE)
    assert interpreters == ["pkgs.python3"], (
        "nix/packages.nix must bind the interpreter exactly once, from "
        "the passed-in package set. Found: " + repr(interpreters)
    )

    execs = re.findall(r"exec \S*/bin/python ", text)
    assert execs and set(execs) == {"exec ${python}/bin/python "}, (
        "every wrapper must exec the single `python` binding above, so "
        "swapping the package set swaps the interpreter the units run. "
        "Found: " + repr(sorted(set(execs)))
    )

    # The PYTHONPATH the wrappers export is built from the same `let`
    # scope's application + dependencies.
    assert text.count('export PYTHONPATH="${agentMcpPyPath}') == 2, (
        "the router and backend wrappers must both take PYTHONPATH from "
        "`agentMcpPyPath`, which is derived from the same scope's "
        "agentMcpPy and python."
    )
    assert "${python.sitePackages}" in text and "makePythonPath" in text


# ── Real evaluation: the override must reach the wrappers ─────────────

_HARNESS_DRIVER = """
let
  repo = builtins.getFlake "@REPO@";
  base = repo.inputs.nixpkgs.legacyPackages.${builtins.currentSystem};
  harness = import @HARNESS@;
  run = modulePkgs: harness { pkgs = base; src = "@REPO@"; inherit modulePkgs; };
  pick = h: {
    inherit (h) routerExecStart backendExecStart routerWrapperText
      backendWrapperText launcherText daemonAgentWrapperOut dashboardOut
      pythonVersion aiohttpVersion homePackages;
  };
in {
  # Option left unset — must be byte-identical to the explicit default.
  unset = pick (run null);
  explicit = pick (run base);
  # Same nixpkgs, different default interpreter: a minimal, offline
  # stand-in for "a package set from another channel".
  swapped = pick (run (base.extend (_final: prev: { python3 = prev.python312; })));
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


def test_override_reaches_the_wrappers_not_just_the_attribute(
    module_eval: dict,
) -> None:
    """The generated wrapper *text* follows ``services.agent-mcp.pkgs``.

    This is the assertion the old ``package`` option would have failed:
    its store path moved, the wrapper's interpreter did not.
    """
    unset = module_eval["unset"]
    swapped = module_eval["swapped"]

    assert unset["pythonVersion"] != swapped["pythonVersion"], (
        "fixture is not exercising anything — both sets resolved to the "
        "same interpreter"
    )
    old_tag = "python" + ".".join(unset["pythonVersion"].split(".")[:2])
    new_tag = "python" + ".".join(swapped["pythonVersion"].split(".")[:2])

    for name in ("routerWrapperText", "backendWrapperText"):
        text = swapped[name]

        # The interpreter the unit actually execs.
        match = re.search(r"exec (\S+)/bin/python ", text)
        assert match, f"{name} does not exec a python at all:\n{text}"
        assert swapped["pythonVersion"] in match.group(1), (
            f"{name} execs {match.group(1)}, not the interpreter from the "
            "configured package set — the override stopped at the "
            "attribute, which is precisely the services.agent-mcp.package "
            "bug"
        )

        assert old_tag not in text, (
            f"{name} still references {old_tag}: the closure is MIXED "
            "(application from one set, interpreter or dependencies from "
            "another), which is worse than either pure option"
        )
        # The app tree and every dependency ride the same PYTHONPATH.
        assert f"/{new_tag}/site-packages" in text
        assert unset[name] != text


def test_override_reaches_every_unit_and_installed_package(
    module_eval: dict,
) -> None:
    """Units, profile packages and the dashboard move with the option too."""
    unset = module_eval["unset"]
    swapped = module_eval["swapped"]

    for key in (
        "routerExecStart",
        "backendExecStart",
        "launcherText",
        "daemonAgentWrapperOut",
        "dashboardOut",
    ):
        assert unset[key] != swapped[key], (
            f"{key} did not follow services.agent-mcp.pkgs — a partial "
            "override leaves the closure mixed"
        )

    # Not asserted as fully disjoint on purpose: the PreCompact hook is a
    # bash/curl/jq substitution with no Python in it, so this fixture's
    # interpreter-only stand-in cannot move it. A real cross-channel set
    # would. Everything Python-coupled is pinned individually above.
    assert sorted(unset["homePackages"]) != sorted(swapped["homePackages"])
