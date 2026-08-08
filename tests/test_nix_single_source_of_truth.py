"""Regression guard: the Nix expressions must declare the Python
application, and the router, exactly ONCE.

Background
----------

``nix/package.nix`` and ``nix/packages.nix`` both built a near-identical
``agentMcpPy`` (``buildPythonApplication``) derivation with *separately
maintained* dependency lists, and they drifted:

- ``packages.nix`` carried ``sse-starlette``, ``aiohttp``, ``requests``
  and the version-gated ``mcp`` floor override.
- ``package.nix`` carried none of them — it worked only because ``mcp``
  happened to pull ``sse-starlette`` transitively.

Every sweep had to be applied twice (the ``python312`` → ``python3``
move in PR #603 touched both; the ``mcp`` pin only reached one), which
is the failure mode documented in ``docs/learnings/duplication-drift.md``:
a fact with no single home.

``nix/package.nix`` turned out to be unreachable except for two flake
outputs that were themselves redundant (see the PR that added this
file), so the fix was deletion, not extraction. These tests keep it
deleted: a second ``buildPythonApplication`` — or a re-vendored copy of
the router that ``agent_mcp/router/`` already owns — fails here rather
than silently drifting for another six months.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
NIX_DIR = REPO_ROOT / "nix"


def _nix_expressions() -> list[Path]:
    return sorted(NIX_DIR.rglob("*.nix"))


def test_exactly_one_python_application_derivation() -> None:
    """Only ``nix/packages.nix`` may call ``buildPythonApplication``.

    The dependency list is the fact with one home. A second call site is
    a second copy of that list, and copies drift.
    """
    declaring = [
        p.relative_to(REPO_ROOT).as_posix()
        for p in _nix_expressions()
        if "buildPythonApplication" in p.read_text()
    ]
    assert declaring == ["nix/packages.nix"], (
        "buildPythonApplication must be declared exactly once, in "
        "nix/packages.nix — that expression owns the interpreter choice, "
        "the pyproject version extraction, the dependency list and the "
        "`mcp` floor gate. Found it in: " + ", ".join(declaring)
    )


def test_no_vendored_router_copy_in_nix() -> None:
    """The router lives in ``agent_mcp/router/``, not vendored under ``nix/``.

    ``nix/router.py`` was a pre-upstream copy of ``agent_mcp/router/app.py``
    that its own header admitted "runs nowhere"; likewise
    ``nix/installer.sh.in`` had drifted behind
    ``agent_mcp/router/installer.sh.in`` (it still emitted the ``type:"sse"``
    client config retired in 3.0.0).
    """
    for orphan, upstream in (
        ("router.py", "agent_mcp/router/app.py"),
        ("installer.sh.in", "agent_mcp/router/installer.sh.in"),
    ):
        assert not (NIX_DIR / orphan).exists(), (
            f"nix/{orphan} is a vendored copy of {upstream}; the packaged "
            "module is the single source of truth. Delete the copy rather "
            "than re-syncing it."
        )


def test_nix_dependency_list_matches_pyproject() -> None:
    """Every ``[project].dependencies`` entry in pyproject must appear in
    the one Nix dependency list.

    Nix may list *more* than pyproject (``aiohttp`` is imported by
    ``agent_mcp/router/*`` but not yet declared upstream), but it must
    never list *fewer* — a missing entry means the build only works by
    borrowing someone else's transitive closure.
    """
    pyproject = (REPO_ROOT / "pyproject.toml").read_text()
    block = pyproject.split("\ndependencies = [", 1)[1].split("\n]", 1)[0]

    declared: set[str] = set()
    for line in block.splitlines():
        line = line.strip()
        if not line.startswith('"'):
            continue
        spec = line.split('"')[1]
        # Strip extras and version constraints: `uvicorn[standard]`,
        # `mcp>=1.27.0,<2` -> `uvicorn`, `mcp`.
        name = spec.split("[")[0].split(">")[0].split("<")[0].split("=")[0]
        declared.add(name.strip().lower().replace("_", "-"))

    packages_nix = (NIX_DIR / "packages.nix").read_text()
    deps_block = packages_nix.split("dependencies = with python.pkgs; [", 1)[1]
    deps_block = deps_block.split("\n    ];", 1)[0]

    # `mcpPinned` is the version-gated override of `python.pkgs.mcp`.
    listed = set(deps_block.replace("mcpPinned", "mcp").split())

    missing = sorted(declared - listed)
    assert not missing, (
        "pyproject declares dependencies that nix/packages.nix does not "
        "list, so they reach the closure only transitively (if at all): "
        + ", ".join(missing)
    )
