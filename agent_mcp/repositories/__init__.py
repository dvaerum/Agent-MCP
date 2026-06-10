# Agent-MCP/agent_mcp/repositories/__init__.py
"""Lifespan-owned Repository singletons.

This package establishes the **class-based** Repository pattern as the
canonical seam between business logic and persistence. PR #137 ("per-
concept repositories") landed a *module of functions* under
``agent_mcp.core.repositories``; the architecture review (2026-06-09)
ruled that did not deliver on the "Repository" name — there was no
identity, no lifecycle, and no place to attach future state (request
counters, batched writes, per-process subscriber registries).

PR #146 made the contract real for **Task** (the first concept in the
series). PR #147 cloned it for **Agent**. This PR (PR 3 of the
series) clones it again for **Message** — the same shape, same
lifecycle, same lazy ``__getattr__`` resolution; just a different
domain.

Note: ``MessageRepository`` carries **no in-memory cache** today —
messages have no ``state.messages``-shaped dict in the codebase, so
the class is a thinner seam over the DB + EventBus than its peers.
``disable_cache`` exists as a no-op so call sites can write the same
``with repo.disable_cache():`` block across all three concepts.

Design decisions locked in grilling (verbatim from PR #146):

* **Shape**: Repository **classes**, not modules of functions. Each
  class is the single owner of its concept's cache+DB invariant.
* **Instantiation**: module singletons, **lifespan-owned**. The app
  startup hook instantiates and attaches the singleton; teardown
  clears it. Tests using the in-process harness pick up the same
  singleton — no per-test wiring.

Import pattern at call sites::

    from agent_mcp.repositories import agent_repo, task_repo
    agent = agent_repo.get_by_token(bearer_token)
    task = task_repo.get_by_id(task_id)
    agent_repo.update_field(agent_id, "status", "active")

Lifecycle:

* ``set_<concept>_repo(instance)`` is called by
  ``app.server_lifecycle.application_startup`` once the DB is ready.
* ``clear_<concept>_repo()`` is called by ``application_shutdown``.
* ``<concept>_repo`` is a module attribute resolved lazily via
  ``__getattr__`` so callers can ``from agent_mcp.repositories
  import <concept>_repo`` at import time without forcing a startup-order
  constraint.

Co-existence with PR #137 module-of-functions:

The old modules ``agent_mcp.core.repositories.task_repo`` /
``agent_repo`` stay alive — every call site that imports the module
form keeps working with no edits. The class form is the new canonical
surface; existing-call-site migration follows in subsequent PRs once
the foundation is proven.
"""
from __future__ import annotations

from typing import Any, Optional


# --- TaskRepository singleton slot (PR #146) ---------------------------

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


# --- AgentRepository singleton slot ------------------------------------

_agent_repo_instance: Optional["AgentRepository"] = None  # noqa: F821


def set_agent_repo(instance: "AgentRepository") -> None:  # noqa: F821
    """Install the AgentRepository singleton.

    Mirrors :func:`set_task_repo`. Called by
    ``app.server_lifecycle.application_startup`` after the DB schema
    is ready.
    """
    global _agent_repo_instance
    _agent_repo_instance = instance


def clear_agent_repo() -> None:
    """Drop the AgentRepository singleton.

    Mirrors :func:`clear_task_repo`. Called by
    ``application_shutdown`` so a stale instance bound to a closed
    engine doesn't leak across the lifespan boundary.
    """
    global _agent_repo_instance
    _agent_repo_instance = None


def get_agent_repo() -> "AgentRepository":  # noqa: F821
    """Return the live singleton, instantiating a default if needed.

    Same lazy-init rationale as :func:`get_task_repo`: protects tests
    and the legacy module-of-functions shim from a cold-start race
    against the lifespan hook.
    """
    global _agent_repo_instance
    if _agent_repo_instance is None:
        from .agent_repository import AgentRepository

        _agent_repo_instance = AgentRepository()
    return _agent_repo_instance


# --- MessageRepository singleton slot ----------------------------------

_message_repo_instance: Optional["MessageRepository"] = None  # noqa: F821


def set_message_repo(instance: "MessageRepository") -> None:  # noqa: F821
    """Install the MessageRepository singleton.

    Mirrors :func:`set_task_repo`. Called by
    ``app.server_lifecycle.application_startup`` after the DB schema
    is ready.
    """
    global _message_repo_instance
    _message_repo_instance = instance


def clear_message_repo() -> None:
    """Drop the MessageRepository singleton.

    Mirrors :func:`clear_task_repo`. Called by
    ``application_shutdown`` so a stale instance bound to a closed
    engine doesn't leak across the lifespan boundary.
    """
    global _message_repo_instance
    _message_repo_instance = None


def get_message_repo() -> "MessageRepository":  # noqa: F821
    """Return the live singleton, instantiating a default if needed.

    Same lazy-init rationale as :func:`get_task_repo`: protects tests
    and the legacy module-of-functions shim from a cold-start race
    against the lifespan hook.
    """
    global _message_repo_instance
    if _message_repo_instance is None:
        from .message_repository import MessageRepository

        _message_repo_instance = MessageRepository()
    return _message_repo_instance


def __getattr__(name: str) -> Any:
    """Module-level lazy attribute lookup.

    ``from agent_mcp.repositories import task_repo`` (or
    ``agent_repo``) resolves through here. The attribute is computed
    on every access (cheap) so a later ``set_*_repo`` is picked up by
    call sites that have already imported the name.

    NB: callers that bind the name once (``r = agent_repo``) capture
    the value of the singleton at bind time. That's fine for normal
    use; tests that need to swap instances per test should call
    ``get_agent_repo()`` directly.
    """
    if name == "task_repo":
        return get_task_repo()
    if name == "agent_repo":
        return get_agent_repo()
    if name == "message_repo":
        return get_message_repo()
    raise AttributeError(
        f"module 'agent_mcp.repositories' has no attribute {name!r}"
    )


__all__ = [
    "agent_repo",
    "clear_agent_repo",
    "clear_message_repo",
    "clear_task_repo",
    "get_agent_repo",
    "get_message_repo",
    "get_task_repo",
    "message_repo",
    "set_agent_repo",
    "set_message_repo",
    "set_task_repo",
    "task_repo",
]
