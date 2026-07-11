# Agent-MCP/agent_mcp/core/event_bus_shim.py
"""Soft-dependency adapter for the (parallel) PR-W2b EventBus.

Relocated from ``agent_mcp/core/repositories/_event_bus_shim.py``
(arch-deepening R3 #2b): it was the only real module left in the
otherwise-emptied ``core/repositories`` package once #2a deleted the
four module-of-functions shadow repos, so the package was retired and
this module moved to a plain ``core/`` home. No longer named with a
leading underscore — it isn't a package-private helper of a repo tree
any more, and no other ``core/*.py`` module uses the convention.

PR-W2c can land before *or* after PR-W2b. To avoid a hard import-order
constraint between two PRs in the same wave, the repos publish to the
bus via this shim, which:

* Imports :mod:`agent_mcp.core.event_bus` lazily on each call so a
  later in-process registration (e.g. a test monkey-patching
  ``sys.modules["agent_mcp.core.event_bus"]``) is picked up
  immediately.
* Silently no-ops if the module is unavailable, so production code
  is unaffected when only PR-W2c has merged.
* Swallows exceptions raised by the bus itself — event delivery is a
  *side effect* of the write; a broken bus must never crash the
  caller whose source-of-truth commit already happened.

Why the indirection (vs. ``from agent_mcp.core import event_bus``):

* A top-level ``import`` at module load locks in the import result
  even after a test injects a fake bus into ``sys.modules``. The
  fresh ``importlib.import_module`` per call sidesteps that.
* The fake bus pattern used by the repo tests (``sys.modules["…"] =
  _FakeBus()``) works uniformly across all four repos via this
  single helper.
"""
from __future__ import annotations

import importlib
from typing import Any, Mapping

from .config import logger


def publish(agent_id: str, event_type: str, payload: Mapping[str, Any]) -> None:
    """Publish to the EventBus if available, else silently no-op.

    The signature matches the bus's documented
    ``bus.notify(agent_id, event_type, payload)`` interface so the
    repos read naturally at the call site.

    ``agent_id`` may be ``"*"`` for broadcast events (e.g. project
    context updates) where no single recipient owns the notification.
    """
    try:
        bus = importlib.import_module("agent_mcp.core.event_bus")
    except ImportError:
        return
    notify = getattr(bus, "notify", None)
    if notify is None:
        return
    try:
        notify(agent_id, event_type, payload)
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning(
            "EventBus.notify(%r, %r) raised; ignoring: %s",
            agent_id,
            event_type,
            exc,
        )
