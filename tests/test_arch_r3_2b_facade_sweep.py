"""arch-deepening R3 #2b — event-bus shim relocation + db/actions facade sweep.

Finishes what #2a started (``tests/test_arch_r3_2a_shadow_repo_collapse.py``):

* Part A — ``agent_mcp/core/repositories/_event_bus_shim.py`` was the
  only real module left in the otherwise-emptied ``core/repositories``
  package once #2a deleted the four module-of-functions shadow repos.
  #2b relocates it to ``agent_mcp/core/event_bus_shim.py`` (dropping
  the leading underscore — it's no longer a package-private helper of
  a repo tree) and deletes the now-fully-empty ``core/repositories``
  package.
* Part B — four of the eight ``agent_mcp/db/actions/*_db.py`` modules
  (``task_db``, ``agent_db``, ``agent_messages_db``, ``rag_db``) were
  pure re-export shims onto the canonical ``agent_mcp/repositories/*``
  classes; #2b deletes them and repoints every importer directly at
  the canonical module. ``file_metadata_db.py`` was an empty, already
  fully-orphaned file (zero importers) and is deleted outright.
  ``agent_actions_db.py`` and ``task_notes_db.py`` own genuine logic
  and are kept.

Correction (arch-r4 #11d): #2b originally also kept ``context_db.py``,
reasoning it was "unrelated dead placeholder code" rather than a shim
in scope for this sweep. That was wrong on its own terms — dead code
with zero importers is dead code regardless of which sweep flagged it.
Both its functions (``get_context`` returning a hardcoded dict,
``update_context`` doing a log line and returning ``True``) were
grep-verified to have zero callers anywhere in ``agent_mcp/`` or
``tests/`` (the real project-context logic lives in
``project_context_tools.py`` / the project-context repository). #11d
deletes it and moves it from the "kept" list below to "deleted".

This guard pins both halves so a future change can't silently
re-introduce the shadow layer.
"""
from __future__ import annotations

import importlib


def test_core_repositories_package_is_gone():
    """The whole ``core.repositories`` package must no longer exist —
    not just the four shadow modules #2a deleted, but the package
    itself (the last real module in it, the event-bus shim, moved to
    ``core.event_bus_shim`` in #2b)."""
    try:
        importlib.import_module("agent_mcp.core.repositories")
    except ModuleNotFoundError:
        pass
    else:
        raise AssertionError(
            "agent_mcp.core.repositories should have been deleted (arch-"
            "deepening R3 #2b relocated its last module and removed the "
            "package)"
        )

    try:
        importlib.import_module("agent_mcp.core.repositories._event_bus_shim")
    except ModuleNotFoundError:
        pass
    else:
        raise AssertionError(
            "agent_mcp.core.repositories._event_bus_shim should have been "
            "deleted — relocated to agent_mcp.core.event_bus_shim"
        )


def test_event_bus_shim_relocated_and_importable():
    """The shim lives at its new canonical home and still publishes."""
    from agent_mcp.core import event_bus_shim

    assert hasattr(event_bus_shim, "publish")
    # No leading underscore any more — it's a plain core/ module now,
    # matching every other module in agent_mcp/core/ (auth.py,
    # config.py, event_bus.py, ...).
    assert event_bus_shim.__name__ == "agent_mcp.core.event_bus_shim"


def test_deleted_db_actions_facades_are_gone():
    """The four pure re-export shims + the empty orphan must be gone.

    ``context_db`` joins this list as of arch-r4 #11d — it was
    wrongly kept by R3 #2b as "unrelated dead placeholder code"; dead
    code with zero importers belongs in this list regardless of which
    sweep flagged it. See the module docstring's "Correction" note.
    """
    deleted = (
        "task_db",
        "agent_db",
        "agent_messages_db",
        "rag_db",
        "file_metadata_db",
        "context_db",
    )
    for name in deleted:
        try:
            importlib.import_module(f"agent_mcp.db.actions.{name}")
        except ModuleNotFoundError:
            continue
        raise AssertionError(
            f"agent_mcp.db.actions.{name} should have been deleted "
            "(arch-deepening R3 #2b / R4 #11d — pure re-export shim / "
            "orphaned empty module / dead placeholder code)"
        )


def test_kept_db_actions_facades_still_import():
    """Files carrying genuine logic are untouched and still importable."""
    kept = ("agent_actions_db", "task_notes_db")
    for name in kept:
        mod = importlib.import_module(f"agent_mcp.db.actions.{name}")
        assert mod is not None


def test_canonical_repository_modules_expose_the_relocated_functions():
    """The functions the deleted shims used to re-export are reachable
    directly from the canonical repository modules importers were
    repointed at."""
    from agent_mcp.repositories import agent_repository, task_repository
    from agent_mcp.repositories import message_repository, rag_repository

    assert callable(task_repository.get_task_by_id)
    assert callable(agent_repository.get_agent_by_token)
    assert callable(message_repository.insert_message)
    assert callable(rag_repository._embeddings_table_exists)


def test_import_agent_mcp_has_no_circular_import():
    """Importing the package fresh must not hit a cycle after the
    relocation + facade sweep."""
    mod = importlib.import_module("agent_mcp")
    assert mod is not None
