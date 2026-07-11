"""arch-r4 #11a: single .env discovery walk.

Regression guard for ``agent_mcp.core.env_boot.discover_and_load_dotenv``
— the function that replaced two independently-drifted parent-directory
walks (a 1-level one in ``agent_mcp/__main__.py``, a 3-level one in
``agent_mcp/cli.py``). This tests the walk directly, in-process, over a
fake directory tree — no subprocess spawn + stdout-grep needed now that
there's one function with a real return value to assert on.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_mcp.core.env_boot import DotenvLoadResult, discover_and_load_dotenv


@pytest.fixture(autouse=True)
def _no_cwd_fallback_load(monkeypatch: pytest.MonkeyPatch) -> None:
    """``discover_and_load_dotenv`` also runs an unconditional,
    cwd-relative ``load_dotenv()`` fallback (preserved from the
    pre-extraction behavior; see the module docstring). That fallback
    would otherwise search from the real pytest cwd and could pick up
    a developer's actual (gitignored) project ``.env``, leaking real
    secrets into this test's process env. Neutralize it so these tests
    only observe the walk itself.
    """
    monkeypatch.setattr("agent_mcp.core.env_boot.load_dotenv", lambda *a, **k: False)


def _clear(monkeypatch: pytest.MonkeyPatch, *names: str) -> None:
    for name in names:
        monkeypatch.delenv(name, raising=False)


def test_dotenv_discovery_prefers_nearest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Given .env files at two ancestor levels, the walk loads the
    NEAREST one — the outer (farther) file's value must not win."""
    _clear(monkeypatch, "ENV_BOOT_TEST_VAR")

    outer = tmp_path
    middle = tmp_path / "middle"
    leaf = middle / "leaf"
    leaf.mkdir(parents=True)

    (outer / ".env").write_text("ENV_BOOT_TEST_VAR=outer\n")
    (middle / ".env").write_text("ENV_BOOT_TEST_VAR=middle\n")
    # leaf itself has none; nearest ancestor carrying a .env is `middle`.

    result = discover_and_load_dotenv(leaf, max_levels=3)

    assert isinstance(result, DotenvLoadResult)
    assert result.path == (middle / ".env").resolve()
    assert result.var_count == 1
    assert os.environ["ENV_BOOT_TEST_VAR"] == "middle"


def test_dotenv_discovery_finds_env_at_start_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `.env` right at the start directory (level 0) is the nearest
    possible match and wins over any ancestor."""
    _clear(monkeypatch, "ENV_BOOT_TEST_VAR")

    (tmp_path / ".env").write_text("ENV_BOOT_TEST_VAR=start\n")
    (tmp_path.parent / ".env.unrelated-marker").write_text("noop\n")

    result = discover_and_load_dotenv(tmp_path, max_levels=3)

    assert result.path == (tmp_path / ".env").resolve()
    assert os.environ["ENV_BOOT_TEST_VAR"] == "start"


def test_dotenv_discovery_returns_none_path_when_nothing_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear(monkeypatch, "ENV_BOOT_TEST_VAR")
    isolated = tmp_path / "no" / "env" / "here"
    isolated.mkdir(parents=True)

    result = discover_and_load_dotenv(isolated, max_levels=2)

    assert result.path is None
    assert result.var_count == 0


def test_dotenv_discovery_respects_max_levels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `.env` two levels up is invisible to a 1-level walk (level 0
    only — the start directory itself)."""
    _clear(monkeypatch, "ENV_BOOT_TEST_VAR")
    leaf = tmp_path / "a" / "b"
    leaf.mkdir(parents=True)
    (tmp_path / ".env").write_text("ENV_BOOT_TEST_VAR=too-far\n")

    result = discover_and_load_dotenv(leaf, max_levels=1)

    assert result.path is None
    assert os.environ.get("ENV_BOOT_TEST_VAR") != "too-far"
