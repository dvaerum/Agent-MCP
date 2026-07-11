# Agent-MCP/agent_mcp/db/unit_of_work.py
"""Write-path unit-of-work: one scope that owns BOTH the multi-table
sqlite transaction AND its post-commit side effects.

Motivation (architecture-deepening candidate D — the KEYSTONE):

On the ``connection=cursor`` seam, ``task_repository`` (and its
siblings) defer commit + cache-update + EventBus publish + audit back
to the caller (see ``task_repository.delete``'s post-commit contract).
So every mutation re-implements the "DB row ⇔ ``g.tasks`` cache ⇔
EventBus event ⇔ audit" choreography by hand — 4 notify surfaces + 2
audit sinks + manual commit ordering, copy-pasted across 5+ sites.
"Forgot to notify" already shipped as a bug (BL-R26-1). The fix is a
scope that owns the transaction AND the effects, so *emit-iff-commit*
becomes structural rather than a thing every caller must remember:

    with unit_of_work() as u:
        u.cursor.execute("DELETE FROM tasks WHERE task_id = ?", (tid,))
        u.emit(assignee, "task.deleted", {"task_id": tid})   # after commit
        u.audit("admin", "deleted_task", task_id=tid, details={...})
        u.on_commit(lambda: task_repo.evict_from_cache(tid))  # after commit
    # __exit__: commit → then flush emits + audits + on_commit hooks
    #           IN REGISTRATION ORDER.
    # On exception before commit: rollback → fire NOTHING.

The whole point is the last two lines: post-commit effects are only
*registered* during the scope and flushed *after* a successful commit.
On rollback, zero effects fire — the "forgot to notify" class is made
unrepresentable because you cannot emit without committing.

D0 (this PR) introduces the seam and migrates ONE mutation
(``delete_task``) end-to-end as the proof. The legacy ``connection=``
path stays intact for every other mutation; D1..n migrate the rest in
file-disjoint waves.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterator, List, Optional

from ..core.config import logger
from .connection import get_db_connection


class UnitOfWork:
    """A single sqlite transaction plus its registered post-commit effects.

    The caller drives writes through :attr:`cursor` (the scope owns the
    ``BEGIN``/``COMMIT``). Side effects — EventBus publishes, audit
    writes, cache updates — are *registered* via :meth:`emit`,
    :meth:`audit` and :meth:`on_commit`; they fire only after the
    transaction commits, in the order they were registered.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection
        self._cursor = connection.cursor()
        # Ordered list of zero-arg callables run after a successful
        # commit. emit / audit / on_commit all append here so a single
        # ordered flush preserves registration order across effect kinds.
        self._post_commit: List[Callable[[], None]] = []
        self.committed = False

    # --- transaction handles ------------------------------------------

    @property
    def cursor(self) -> sqlite3.Cursor:
        """The transaction's cursor. Hand this to repo ``connection=``
        methods or drive raw SQL through it — it owns the open txn."""
        return self._cursor

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    # --- post-commit effect registration ------------------------------

    def emit(
        self,
        addressee: Optional[str],
        event_type: str,
        payload: Dict[str, Any],
    ) -> None:
        """Register an EventBus publish to fire AFTER commit.

        ``addressee`` is the recipient agent id (or ``"*"`` for
        broadcast); a falsy value is normalised to ``"*"`` to match the
        repo's ``_publish`` contract. Delivery goes through the same
        ``_event_bus_shim.publish`` funnel the repos use, so a broken
        bus never crashes the caller whose commit already happened.
        """

        def _effect() -> None:
            from ..core.repositories import _event_bus_shim

            _event_bus_shim.publish(addressee or "*", event_type, payload)

        self._post_commit.append(_effect)

    def audit(
        self,
        actor: Optional[str],
        action: str,
        *,
        task_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        principal: Any = None,
    ) -> None:
        """Register an audit write to BOTH sinks, to fire AFTER commit.

        Callers historically had to hand-sequence two calls:

        * ``log_agent_action_to_db(cursor=...)`` — the persistent
          ``agent_actions`` DB sink; and
        * ``log_audit(...)`` — the in-memory ``g.audit_log`` sink.

        Registering one :meth:`audit` writes both, only if the
        transaction commits. The DB sink is written post-commit as its
        own small transaction on the same connection (emit-iff-commit:
        a rolled-back scope leaves no ``agent_actions`` row).
        """

        def _effect() -> None:
            from ..db.actions.agent_actions_db import log_agent_action_to_db
            from ..utils.audit_utils import log_audit

            # DB sink — a post-commit write in its own transaction on the
            # scope's connection. Failure here must not sink the other
            # effects (the source-of-truth delete already committed).
            try:
                cur = self._conn.cursor()
                log_agent_action_to_db(
                    cursor=cur,
                    agent_id=actor,
                    action_type=action,
                    task_id=task_id,
                    details=details,
                    principal=principal,
                )
                self._conn.commit()
            except Exception as exc:  # pragma: no cover — defensive
                logger.error(
                    "unit_of_work audit DB sink failed for action %r: %s",
                    action,
                    exc,
                )

            # In-memory sink (g.audit_log). log_audit derives its own
            # actor label; pass the actor through so both sinks agree.
            try:
                log_audit(actor or "?", action, details or {})
            except Exception as exc:  # pragma: no cover — defensive
                logger.error(
                    "unit_of_work audit in-memory sink failed for %r: %s",
                    action,
                    exc,
                )

        self._post_commit.append(_effect)

    def on_commit(self, fn: Callable[[], None]) -> None:
        """Register a generic zero-arg post-commit hook (e.g. a cache
        update). Fires after commit, in registration order."""
        self._post_commit.append(fn)

    # --- internal -----------------------------------------------------

    def _flush(self) -> None:
        """Run every registered post-commit effect in order. An effect
        that raises is logged and skipped so one broken side effect
        can't strand the others — the commit already happened."""
        for effect in self._post_commit:
            try:
                effect()
            except Exception as exc:  # pragma: no cover — defensive
                logger.warning(
                    "unit_of_work post-commit effect raised; ignoring: %s",
                    exc,
                )


@contextmanager
def unit_of_work() -> Iterator[UnitOfWork]:
    """Open a write-path unit of work.

    On clean exit: ``COMMIT`` then flush the registered post-commit
    effects (emits, audits, cache hooks) in order. On any exception
    raised inside the scope: ``ROLLBACK`` and fire NOTHING, then
    re-raise. Either way the connection is closed on the way out.
    """
    conn = get_db_connection()
    u = UnitOfWork(conn)
    try:
        yield u
    except BaseException:
        # Exception inside the scope: roll back and fire ZERO effects.
        try:
            conn.rollback()
        finally:
            conn.close()
        raise

    # Clean exit: commit first. If the commit itself fails, roll back
    # and fire nothing — emit-iff-commit holds even for a failed commit.
    try:
        conn.commit()
    except BaseException:
        try:
            conn.rollback()
        finally:
            conn.close()
        raise

    u.committed = True
    try:
        u._flush()
    finally:
        conn.close()
