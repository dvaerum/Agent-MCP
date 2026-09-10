"""Regression guard: the Nix expressions must not re-declare a Python
application, and the router must not be re-vendored under ``nix/``.

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
file), so the fix was deletion, not extraction.

Retirement note
----------------

The Python implementation this guard was originally about — the whole
``agent_mcp/app``/``agent_mcp/router``/``agent_mcp/cli.py`` tree —
was deleted wholesale once the Rust ``rust/`` workspace reached
functional completeness (packaged separately via ``nix/conexus.nix``).
``nix/packages.nix`` no longer calls ``buildPythonApplication`` at all
— the "exactly one call site" invariant tightened to "zero call sites,
anywhere": a second Python application derivation would be a full
re-introduction of the retired implementation, not a drift of an
existing one.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
NIX_DIR = REPO_ROOT / "nix"


def _nix_expressions() -> list[Path]:
    return sorted(NIX_DIR.rglob("*.nix"))


def test_no_python_application_derivation_anywhere() -> None:
    """No ``*.nix`` file may call ``buildPythonApplication``.

    The Python implementation was retired wholesale; a Nix expression
    building a Python application again would be a full reintroduction
    of it (or a reincarnation of the very ``nix/package.nix`` /
    ``nix/packages.nix`` duplication this test file was originally
    written to catch), not a legitimate new feature that can slip in
    silently.
    """
    declaring = [
        p.relative_to(REPO_ROOT).as_posix()
        for p in _nix_expressions()
        if "buildPythonApplication" in p.read_text()
    ]
    assert declaring == [], (
        "buildPythonApplication must not appear in any Nix expression — "
        "the Python implementation was retired in favor of the Rust "
        "rust/ workspace (see nix/conexus.nix). Found it in: "
        + ", ".join(declaring)
    )


def test_no_vendored_router_copy_in_nix() -> None:
    """The router does not live vendored under ``nix/``.

    ``nix/router.py`` was a pre-upstream copy of the Python router's
    ``app.py`` that its own header admitted "runs nowhere"; likewise
    ``nix/installer.sh.in`` had drifted behind the Python router's own
    copy (it still emitted the ``type:"sse"`` client config retired in
    3.0.0). Both the originals and the vendored copies are gone now
    that the Python implementation was retired wholesale — this test
    just keeps the vendored-copy shape from coming back.
    """
    for orphan in ("router.py", "installer.sh.in"):
        assert not (NIX_DIR / orphan).exists(), (
            f"nix/{orphan} looks like a vendored copy of a retired Python "
            "router asset. The Python implementation is gone; don't "
            "re-vendor a copy of it under nix/."
        )
