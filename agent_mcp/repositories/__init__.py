# Agent-MCP/agent_mcp/repositories/__init__.py
"""Lifespan-owned Repository singletons (PR #146).

This package establishes the **class-based** Repository pattern as the
canonical seam between business logic and persistence. PR #137 ("per-
concept repositories") landed a *module of functions* under
``agent_mcp.core.repositories``; the architecture review (2026-06-09)
ruled that did not deliver on the "Repository" name — there was no
identity, no lifecycle, and no place to attach future state (request
counters, batched writes, per-process subscriber registries).

This PR makes the contract real for **Task**. Agent and Message
follow in PRs 2-3 of the architecture-review series.

Design decisions locked in grilling:

* **Shape**: Repository **classes**, not modules of functions. Each
  class is the single owner of its concept's cache+DB invariant.
* **Instantiation**: module singletons, **lifespan-owned**. The app
  startup hook instantiates and attaches the singleton; teardown
  clears it. Tests using the in-process harness pick up the same
  singleton — no per-test wiring.
* **First concept**: Task. Highest leakage in the architecture
  review (12 raw ``get_db_connection()`` sites in ``task_tools.py``
  + 4 in ``app/routes.py``) and the most complex domain (cascade
  deletes, parent-child trees, dependency edges).

Import pattern at call sites:

    from agent_mcp.repositories import task_repo
    task = task_repo.get_by_id(task_id)
    task_repo.update_fields(task_id, {"status": "completed"})

Lifecycle:

* ``set_task_repo(instance)`` is called by
  ``app.server_lifecycle.application_startup`` once the DB is ready.
* ``clear_task_repo()`` is called by ``application_shutdown``.
* ``task_repo`` is a module attribute resolved lazily via
  ``__getattr__`` so callers can ``from agent_mcp.repositories
  import task_repo`` at import time without forcing a startup-order
  constraint.

Co-existence with PR #137 module-of-functions:

The old module ``agent_mcp.core.repositories.task_repo`` stays alive
as a thin wrapper around the singleton — every call site that
imports the module form keeps working with no edits. The class
form is the new canonical surface; existing-call-site migration
follows in subsequent PRs once the foundation is proven.
"""
from __future__ import annotations

from typing import Any, Optional


# Module-level singleton slot. Populated by ``set_task_repo`` during
# lifespan startup; cleared by ``clear_task_repo`` on shutdown.
_task_repo_instance: Optional["TaskRepository"] = None  # noqa: F821


def set_task_repo(instance: "TaskRepository") -> None:  # noqa: F821
    """Install the TaskRepository singleton.

    Called by ``app.server_lifecycle.application_startup`` after the
    DB schema is ready. Idempotent: a second call replaces the first
    (the lifespan path runs once per app build, but tests build the
    app multiple times in one process).
    """
    global _task_repo_instance
    _task_repo_instance = instance


def clear_task_repo() -> None:
    """Drop the singleton.

    Called by ``application_shutdown`` so a stale instance bound to a
    closed engine doesn't leak across the lifespan boundary. Matches
    the ``write_queue.stop()`` pattern used by the same shutdown hook.
    """
    global _task_repo_instance
    _task_repo_instance = None


def get_task_repo() -> "TaskRepository":  # noqa: F821
    """Return the live singleton, instantiating a default if needed.

    Lazy-init protects two paths the lifespan hook can't always cover:

    1. Tests that import ``task_repo`` before any app is built. The
       lazy instantiation gives them a working repo against whatever
       DB their test fixture wires up.
    2. Module-import-time access from the legacy
       ``agent_mcp.core.repositories.task_repo`` shim. The shim is
       imported before the lifespan hook runs, so the slot is empty;
       lazy-init lets the shim's functions just work.

    The default instance has no state of its own — it delegates to
    the existing ORM-backed helpers — so an extra instantiation is
    cheap and idempotent.
    """
    global _task_repo_instance
    if _task_repo_instance is None:
        from .task_repository import TaskRepository

        _task_repo_instance = TaskRepository()
    return _task_repo_instance


def __getattr__(name: str) -> Any:
    """Module-level lazy attribute lookup.

    ``from agent_mcp.repositories import task_repo`` resolves through
    here. The attribute is computed on every access (cheap) so a
    later ``set_task_repo`` is picked up by call sites that have
    already imported the name.

    NB: callers that bind the name once (``r = task_repo``) capture
    the value of the singleton at bind time. That's fine for normal
    use; tests that need to swap instances per test should call
    ``get_task_repo()`` directly.
    """
    if name == "task_repo":
        return get_task_repo()
    raise AttributeError(
        f"module 'agent_mcp.repositories' has no attribute {name!r}"
    )


__all__ = [
    "clear_task_repo",
    "get_task_repo",
    "set_task_repo",
    "task_repo",
]
