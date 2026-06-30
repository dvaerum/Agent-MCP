"""VULN-002 regression guard: secret-prefix logging at CLI startup.

Background
----------

The audit (VULN-002, Medium) flagged three startup print statements
that leaked sensitive material to stdout — and therefore to the
systemd journal / docker logs / Forgejo CI runner output / any
operator scrollback. The trust boundary for journal reads is wider
than the in-process secret store: anyone with `journalctl -u
agent-mcp@*` access (often a larger ops set than holders of the
literal `.env` file) could fingerprint the org's OpenAI key from a
20-character prefix.

The three offending sites were:

1. ``agent_mcp/__main__.py:15`` — printed ``OPENAI_API_KEY[:20]``
2. ``agent_mcp/cli.py:75`` — printed ``OPENAI_API_KEY[:10]``
3. ``agent_mcp/cli.py:71`` — printed ``list(_env_vars.keys())`` (full
   inventory of secret-bearing env-var names — AUTH_*, SMTP_*,
   OIDC_*, etc. — handing a journal reader the shopping list)

The fixes replace prefix-prints with presence-only ("present" /
"NOT FOUND") and replace the inventory print with a count.

What this test pins
-------------------

The test spawns ``python -m agent_mcp --help`` with a temp .env that
holds a recognizable fake key and a recognizable extra var name. It
captures stdout and asserts:

* the fake key's value does NOT appear (in full or as any prefix)
* the extra var's NAME does NOT appear (the inventory leak)
* the word "present" DOES appear (the loaded-confirmation signal
  still reaches operators)

A regression of any of the three sites would surface here.

The discovery walk in ``agent_mcp.__main__`` is anchored at
``Path(__file__).resolve().parent.parent`` — i.e. the source-tree
project root — so the test writes the .env there, with try/finally
restore of any pre-existing file. ``.env`` is git-ignored in this
repo, so the file is not visible to commits even mid-test.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Iterator

import pytest


# A value chosen to be unmistakable in any stdout output. The prefix
# (first 20 chars) is what the legacy code used to log, so we assert
# the prefix is absent — but the full string is asserted absent too
# as belt-and-suspenders.
_FAKE_KEY = "sk-test-DO-NOT-LOG-THIS-1234567890abcdef"
_FAKE_KEY_PREFIX_20 = _FAKE_KEY[:20]
_FAKE_KEY_PREFIX_10 = _FAKE_KEY[:10]

# An extra environment variable with a distinctive name. The audit
# called out the inventory leak: the legacy code printed the *names*
# of all .env-loaded vars, which is itself sensitive (it tells a
# journal reader what kinds of secrets the deploy holds). We pick a
# name that's obvious in stdout if the leak regresses.
_SENTINEL_VAR_NAME = "AUTH_TOKEN_VULN002_CANARY"
_SENTINEL_VAR_VALUE = "do-not-log-this-name-either"


def _project_root() -> Path:
    """The directory where ``agent_mcp/__main__.py``'s discovery walk
    will look for ``.env`` (= parent.parent of __main__.py)."""
    import agent_mcp  # noqa: PLC0415

    return Path(agent_mcp.__file__).resolve().parent.parent


@pytest.fixture
def _tmp_env_file() -> Iterator[Path]:
    """Drop a controlled .env at project_root, restore on exit.

    Safe under pytest-xdist because no other test in the suite writes
    to this path (conftest uses ``monkeypatch.setenv`` for env vars
    and points DOTENV_PATH at /dev/null). The fixture takes ownership
    of project_root/.env for its duration.
    """
    env_path = _project_root() / ".env"

    # Preserve any pre-existing .env (developer scenario; CI starts
    # clean). The backup name is unique enough not to collide.
    backup = env_path.with_suffix(".env.vuln002-test-backup")
    if env_path.exists():
        env_path.rename(backup)

    try:
        env_path.write_text(
            f"OPENAI_API_KEY={_FAKE_KEY}\n"
            f"{_SENTINEL_VAR_NAME}={_SENTINEL_VAR_VALUE}\n"
        )
        yield env_path
    finally:
        env_path.unlink(missing_ok=True)
        if backup.exists():
            backup.rename(env_path)


def test_startup_does_not_leak_openai_key_prefix(_tmp_env_file: Path) -> None:
    """VULN-002: the .env-loaded OPENAI_API_KEY value must not appear
    in startup stdout, and the loaded-var name set must not be
    enumerated. The "present" signal must still appear so operators
    can tell the key was loaded."""
    # Run with a clean env so the parent process's OPENAI_API_KEY
    # (which conftest blanks to "") doesn't shadow the .env load.
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("OPENAI_API_KEY", _SENTINEL_VAR_NAME)
    }
    # PYTHONUNBUFFERED so the prints are flushed before --help exits.
    env["PYTHONUNBUFFERED"] = "1"

    proc = subprocess.run(
        [sys.executable, "-m", "agent_mcp", "--help"],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )

    # Diagnostic dump on any assertion failure.
    diag = f"\n--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"

    assert proc.returncode == 0, f"`agent_mcp --help` failed{diag}"

    # 1. The full fake key must not appear anywhere in stdout.
    assert _FAKE_KEY not in proc.stdout, (
        "VULN-002 regression: full OPENAI_API_KEY value leaked to stdout"
        + diag
    )

    # 2. Neither the legacy 20-char nor 10-char prefix should appear.
    #    (The 20-char check is the one that mattered for __main__.py;
    #    the 10-char check is the one that mattered for cli.py.)
    assert _FAKE_KEY_PREFIX_20 not in proc.stdout, (
        "VULN-002 regression: 20-char OPENAI_API_KEY prefix leaked (legacy "
        "__main__.py format)" + diag
    )
    assert _FAKE_KEY_PREFIX_10 not in proc.stdout, (
        "VULN-002 regression: 10-char OPENAI_API_KEY prefix leaked (legacy "
        "cli.py format)" + diag
    )

    # 3. The sentinel variable's NAME must not appear — the inventory
    #    leak in the old `print(f"Loaded variables: {list(_env_vars.keys())}")`.
    assert _SENTINEL_VAR_NAME not in proc.stdout, (
        "VULN-002 regression: loaded-env-var name enumerated in stdout "
        "(was 'Loaded variables: [...]')" + diag
    )

    # 4. The presence-confirmation signal must still reach operators.
    #    Both __main__.py and cli.py emit a "present" string when the
    #    key is loaded.
    assert "present" in proc.stdout, (
        "expected 'present' confirmation in startup output" + diag
    )
