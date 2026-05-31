"""Regression guard: keep pytest-xdist wired up.

The full test suite has a one-time ~4s lifespan startup cost (sqlite-vec
load, Alembic upgrade, RAG init, write_queue start) that dominates serial
runs. pytest-xdist parallelizes across cores so the cost is paid per worker
in parallel, not once sequentially.

This test asserts the two pieces a future commit would have to silently
remove to lose the speedup:
  1. `pytest-xdist` listed in `[project.optional-dependencies] dev`
  2. `-n` flag present in `[tool.pytest.ini_options] addopts`

If you intentionally remove parallel execution, delete this file in the
same commit and justify it in the PR.
"""

from __future__ import annotations

import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - 3.10 fallback
    import tomli as tomllib


PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def _load() -> dict:
    with PYPROJECT.open("rb") as fh:
        return tomllib.load(fh)


def test_pytest_xdist_in_dev_optional_dependencies() -> None:
    """pytest-xdist must be declared as a dev dependency."""
    data = _load()
    dev_deps = data["project"]["optional-dependencies"]["dev"]
    assert any(
        dep.startswith("pytest-xdist") for dep in dev_deps
    ), f"pytest-xdist missing from [project.optional-dependencies] dev: {dev_deps}"


def test_pytest_addopts_enables_parallel_workers() -> None:
    """addopts must pass `-n <N>` so `pytest` defaults to parallel execution."""
    data = _load()
    ini = data.get("tool", {}).get("pytest", {}).get("ini_options", {})
    addopts = ini.get("addopts", "")
    # addopts may be a string or a list; normalize to a token stream.
    if isinstance(addopts, str):
        tokens = addopts.split()
    else:
        tokens = list(addopts)
    # Accept either `-n auto`, `-n 4`, `-nauto`, or `--numprocesses=...`
    has_n_flag = any(
        tok == "-n" or tok.startswith("-n") or tok.startswith("--numprocesses")
        for tok in tokens
    )
    assert has_n_flag, (
        "pytest addopts missing `-n` flag — xdist parallel execution is off. "
        f"Found addopts={addopts!r}"
    )
