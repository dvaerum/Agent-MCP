"""One rule for "does this requester own this task" (per-task-ownership
consolidation).

Before this module the rule was reimplemented independently at 5+ call
sites, and had already quietly drifted into THREE different shapes:

- ``task_tools.py`` (5 write-path gates: status/field update, subtask
  parent/dependency attach, request_assistance, bulk ops): exact
  ``assigned_to == requester`` match only.
- ``task_comments_tools.py`` (``add_task_comment``): exact match
  WIDENED to also accept the task's ``created_by``.
- ``features/task_queries.py`` (``view_tasks`` read-scoping) and
  ``features/rag/query.py`` (RAG task-retrieval scoping): exact match
  WIDENED to also accept an unassigned/claimable task (each with its
  own definition of "unassigned" — see :func:`is_unassigned`'s
  docstring for why that dimension stays a caller-supplied flag rather
  than a single hardcoded rule).

Consolidating the CHECK itself (not the widenings, which are genuinely
different per call site and must stay explicit, opt-in parameters) means
a future change to the base rule lands in exactly one place instead of
requiring a class-sweep across 5+ files — which is exactly what
happened next: ``include_foreign`` (the
``config_allow_worker_view_foreign_tasks`` /
``config_allow_worker_comment_foreign_tasks`` toggles, both default
``True``) landed here once, rather than as a second class-sweep.

:func:`can_access_task` is the dict-based predicate for callers that
already have the task row (or a partial row with just ``assigned_to`` /
``created_by``) in hand. :func:`sql_fragment` is the SQL-layer
equivalent for callers that scope a query at the DB layer instead of
filtering an already-fetched dict (``rag/query.py``'s pre-vector-search
task-context fetch, which exists specifically to avoid pulling an
unscoped table into an LLM prompt) — it only expresses the *exact-match*
half of the rule (no ``include_created_by`` / ``include_unassigned``
widening), since those callers don't need it today. A property test
(``tests/test_task_ownership.py``) proves the two never disagree on the
rule they DO share.
"""

from __future__ import annotations

from typing import Any, List, Mapping, Optional, Tuple


def is_unassigned(task: Mapping[str, Any]) -> bool:
    """Whether ``task["assigned_to"]`` is NULL/empty/whitespace-only —
    the write-path definition of "in the claimable pool, no owner to
    hide" used by :func:`can_access_task`'s ``include_unassigned``
    widening (mirrors ``task_tools._worker_ownership_deny``'s own
    ``assignee is None or assignee.strip() == ""`` check).

    NOT the same predicate as ``task_queries.is_claimable_task``, which
    additionally excludes terminal-status tasks (the read-path's
    stricter "advertise as claimable" rule, R16-F2) — that extra axis is
    specific to the claimable-pool LISTING and stays in
    ``task_queries.py``, composed on top of this function rather than
    folded into it, so this module doesn't grow a dependency on task
    status semantics it doesn't otherwise need.
    """
    v = task.get("assigned_to")
    return v is None or (isinstance(v, str) and v.strip() == "")


def can_access_task(
    task: Mapping[str, Any],
    *,
    requester_id: Optional[str],
    can_view_all_tasks: bool,
    include_created_by: bool = False,
    include_unassigned: bool = False,
    include_foreign: bool = False,
) -> bool:
    """Whether ``requester_id`` may access ``task``.

    ``can_view_all_tasks`` is the caller's already-resolved
    ``tasks.assign`` capability check (operator / manager / sysadmin) —
    this function does not know about ``Principal``; every call site
    threads that bool in, exactly as they already do for
    ``is_admin_request`` / ``can_view_all_tasks`` today. Passing that
    resolution in rather than a ``Principal`` keeps this module usable
    from the leaf feature layers (``task_queries.py``, ``rag/query.py``)
    that must not import the tool-layer's auth types.

    ``include_foreign`` is the ``config_allow_worker_view_foreign_tasks``
    / ``config_allow_worker_comment_foreign_tasks`` widening (both
    default ``True``): a task assigned to a DIFFERENT agent is
    accessible too. Deliberately distinct from ``include_unassigned`` —
    an unassigned task has no owner to hide (already the claimable
    pool); a foreign-owned task has a real owner this flag chooses to
    stop hiding from. A caller wanting both passes both flags.

    A missing ``requester_id`` degrades CLOSED: with no admin bypass and
    ``include_foreign`` off, it can only ever match
    ``include_unassigned`` (never an ``assigned_to``/``created_by``
    equality, since ``None`` is deliberately never treated as a
    wildcard).
    """
    if can_view_all_tasks:
        return True
    if requester_id and task.get("assigned_to") == requester_id:
        return True
    if (
        include_created_by
        and requester_id
        and task.get("created_by") == requester_id
    ):
        return True
    if include_unassigned and is_unassigned(task):
        return True
    if include_foreign and not is_unassigned(task):
        return True
    return False


def sql_fragment(
    requester_id: Optional[str],
    can_view_all_tasks: bool,
    *,
    include_foreign: bool = False,
) -> Tuple[str, List[str]]:
    """Return the ``(sql_fragment, params)`` that scopes a ``tasks``
    ``SELECT`` to :func:`can_access_task`'s exact-match rule, optionally
    widened by ``include_foreign`` (no ``include_created_by`` /
    ``include_unassigned`` support — those callers don't need it today).

    An admin/manager (``can_view_all_tasks``) gets an empty fragment
    (unscoped). ``include_foreign`` scopes to "any task with a real
    assignee" (``own`` ∪ ``foreign`` — the exact complement of
    unassigned), needing no ``requester_id`` at all. Otherwise: ``AND
    assigned_to = ?`` bound to ``requester_id``. A missing/falsy
    ``requester_id`` (with ``include_foreign`` off) degrades closed via
    an unsatisfiable ``AND 1=0`` — NOT ``assigned_to = ''``, which would
    accidentally match a real row whose ``assigned_to`` is itself the
    empty string (``is_unassigned`` treats "" as unassigned, so such
    rows exist) rather than matching nothing.
    """
    if can_view_all_tasks:
        return "", []
    if include_foreign:
        return " AND assigned_to IS NOT NULL AND assigned_to != ''", []
    if not requester_id:
        return " AND 1=0", []
    return " AND assigned_to = ?", [requester_id]
