"""Per-project debug-logging switches that default to an env var.

Each switch resolves in this order:

1. the project setting ``config_debug_<category>`` (toggled in the Settings
   dashboard, stored in ``project_settings``);
2. when that row is absent, the ``AGENT_MCP_<CATEGORY>_DEBUG`` environment
   variable (the deploy-time default);
3. otherwise ``False``.

So an operator can flip a debug stream on/off per project from the dashboard,
while the env var stays the fleet-wide default. Reads are TTL-cached (the
per-project backend serves ONE project, so a process-global cache keyed by the
setting name is correct) — the hot logging paths call this on every line, and
must not hit the DB each time. A dashboard toggle takes effect within
:data:`_TTL` seconds.
"""

from __future__ import annotations

import os
import time
from typing import Dict, Tuple

# setting_key -> (cached_at_monotonic, value)
_CACHE: Dict[str, Tuple[float, bool]] = {}
_TTL = 5.0

_TRUE = ("1", "true", "yes", "on")


def _env_bool(env_key: str) -> bool:
    return os.environ.get(env_key, "").strip().lower() in _TRUE


def _resolve(setting_key: str, env_key: str) -> bool:
    """Project setting if present, else the env var, else False."""
    default = _env_bool(env_key)
    try:
        # Local import: keeps this module import-cheap and avoids a cycle
        # (access.py imports the settings schema which imports core.*).
        from ..tools.access import _get_config_bool

        return _get_config_bool(setting_key, default)
    except Exception:
        # Off-wire / no project DB context / any read failure → the env
        # default. Debug logging must never break a request.
        return default


def debug_enabled(setting_key: str, env_key: str) -> bool:
    now = time.monotonic()
    hit = _CACHE.get(setting_key)
    if hit is not None and now - hit[0] < _TTL:
        return hit[1]
    val = _resolve(setting_key, env_key)
    _CACHE[setting_key] = (now, val)
    return val


def clear_cache() -> None:
    """Drop the TTL cache (test isolation / force an immediate re-read)."""
    _CACHE.clear()
