# Agent-MCP/agent_mcp/core/env_boot.py
"""``.env`` discovery walk — the pre-import boot step every entrypoint runs.

Extracted out of ``cli.py`` (arch-r4 #11a). Before this, ``__main__.py``
ran its own 1-level parent walk + ``load_dotenv`` and ``cli.py`` ran a
*different*, wider 3-level parent walk + its own prints, and both ran
back-to-back on every ``python -m agent_mcp`` invocation (``__main__``
imports ``.cli``). The 3-level walk is a strict superset of the
1-level one, so there was never a behavioral reason for two copies —
just an accretion. This module is the single walk; callers decide how
(and whether) to log the result.

Deliberately dependency-free w.r.t. the rest of ``agent_mcp`` — only
stdlib + ``python-dotenv``. ``core.config`` reads ``OPENAI_API_KEY``
(and other secrets) from the environment at *import time*, so the
walk must complete before any other ``agent_mcp`` submodule import.
Importing this module must not transitively import ``core.config`` or
anything that does, or the ordering guarantee breaks.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from dotenv import dotenv_values, load_dotenv


@dataclass(frozen=True)
class DotenvLoadResult:
    """Outcome of a `.env` discovery walk.

    ``path`` is the discovered file (``None`` if the walk found
    nothing at any level). ``var_count`` is how many variables it
    loaded from that file — deliberately not the variable *names*,
    which can be sensitive (AUTH_*, OPENAI_API_KEY, SMTP_*, ...); see
    ``tests/test_no_secret_prefix_in_logs.py`` (VULN-002).
    """

    path: Optional[Path]
    var_count: int


def discover_and_load_dotenv(start: Path, max_levels: int) -> DotenvLoadResult:
    """Walk from ``start`` up through ``max_levels`` parent directories
    (level 0 is ``start`` itself) looking for a ``.env`` file. The
    nearest match wins and its variables are loaded into
    ``os.environ`` (existing values are overridden — matches the
    pre-extraction behavior).

    Also runs an unconditional, cwd-relative ``load_dotenv()`` after
    the walk (whether or not the walk found anything) — this is
    ``python-dotenv``'s own discovery, kept as a fallback in case a
    wrapper script (e.g. the deploy repo's) puts ``.env`` somewhere
    this walk doesn't reach. This mirrors the pre-extraction ``cli.py``
    behavior exactly.

    No printing here — callers own how (and whether) to log the
    result; this function is a pure side-effecting env loader plus a
    structured result.
    """
    found_path: Optional[Path] = None
    var_count = 0

    for level in range(max_levels):
        candidate = (start / ("../" * level) / ".env").resolve()
        if candidate.exists():
            env_vars = dotenv_values(str(candidate))
            for key, value in env_vars.items():
                if value is not None:
                    os.environ[key] = value
            found_path = candidate
            var_count = len(env_vars)
            break

    load_dotenv()

    return DotenvLoadResult(path=found_path, var_count=var_count)
