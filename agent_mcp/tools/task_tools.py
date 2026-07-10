# Agent-MCP/mcp_template/mcp_server_src/tools/task_tools.py
import json
import datetime
import secrets  # For task_id generation
import os  # For request_assistance (notifications path)
import sqlite3  # For database operations
from pathlib import Path  # For request_assistance
from typing import List, Dict, Any, Optional

from .registry import register_tool
from . import access as _access  # Canonical home for _get_config_bool
from ..core.config import logger, ENABLE_TASK_PLACEMENT_RAG, ALLOW_RAG_OVERRIDE
from ..core import globals as g
from ..core.auth import get_agent_id
from ..core.authorize import requires_capability, requires_policy
from ..core.principal import Principal
from ..core.tool_result import (
    Conflict,
    Failed,
    Invalid,
    NotFound,
    Ok,
    PermissionDenied,
    ToolResult,
)
from ..utils.audit_utils import log_audit
from ..db.connection import get_db_connection, execute_db_write
from ..db.actions.agent_actions_db import log_agent_action_to_db
from ..features.task_placement.validator import validate_task_placement
from ..features.task_placement.suggestions import (
    format_suggestions_for_agent,
    format_override_reason,
    should_escalate_to_admin,
)
from ..features.rag.indexing import index_task_data
from ..features.task_queries import (
    TaskFilterSpec,
    TaskQueryEngine,
    TaskSortSpec,
)

# For request_assistance, generate_id was used. Let's use secrets.token_hex for consistency.
# from main.py:1191 (generate_id - not present, assuming secrets.token_hex was intended)
from .agent_communication_tools import send_agent_message_tool_impl

# Wave 7 PR 3 (coordinator transition): the post-task-completion
# auto-launch of a "testing agent" via tmux is gone. agent-mcp no
# longer spawns claude processes — the operator registers any
# follow-up agent via ``register_agent`` and the user starts their
# own claude session.
from ..utils.prompt_templates import build_agent_prompt  # still used by other paths


def _publish_task_event(
    assigned_to: Optional[str], event: str, payload: Dict[str, Any]
) -> None:
    """Publish a task lifecycle event through the EventBus shim.

    Mirrors ``task_repository._publish``: the addressee is the assignee
    (or ``"*"`` for unassigned/broadcast). Used on the ``connection=``
    write paths, where ``task_repo.create``/``delete`` defer the publish
    to the caller (post-commit) so a subscriber never observes an
    uncommitted / rolled-back row. Delivery failures are swallowed by the
    shim — the source-of-truth commit already happened.
    """
    from ..core.repositories import _event_bus_shim

    _event_bus_shim.publish(assigned_to or "*", event, payload)


def _link_child_to_parent(cursor, parent_task_id, child_task_id) -> bool:
    """Append ``child_task_id`` to the parent's ``child_tasks`` mirror.

    BL-2: every creation path that sets ``parent_task`` must maintain the
    parent's back-reference (like ``request_assistance`` does) so
    hierarchy reads (``view_tasks`` / metrics) and the ``delete_task``
    cascade see the child. Runs inside the caller's creation transaction
    (writes via ``task_repo.update_fields`` with the caller's cursor, so
    it stays atomic with the child INSERT). The parent's cache entry is
    reconciled separately post-commit via :func:`_refresh_parent_cache`.

    No-ops when ``parent_task_id`` is falsy or the parent row is absent.
    Returns True when a mirror write happened (so the caller knows to
    refresh the parent's cache after commit).
    """
    if not parent_task_id:
        return False
    from ..repositories import task_repo as _task_repo

    cursor.execute(
        "SELECT child_tasks FROM tasks WHERE task_id = ?", (parent_task_id,)
    )
    row = cursor.fetchone()
    if row is None:
        return False
    children = json.loads(row["child_tasks"] or "[]")
    if child_task_id in children:
        return True
    children.append(child_task_id)
    _task_repo.update_fields(
        parent_task_id, {"child_tasks": children}, connection=cursor
    )
    return True


def _refresh_parent_cache(parent_task_id) -> None:
    """Reconcile a parent task's cache entry after its ``child_tasks``
    mirror was updated inside a now-committed transaction.

    ``update_fields(connection=)`` defers the cache write to the caller
    (see ``task_repository.update_fields``); this mirrors the
    ``upsert_cache`` the working create paths do for the child row.
    No-ops when ``parent_task_id`` is falsy or the row vanished.
    """
    if not parent_task_id:
        return
    from ..repositories import task_repo as _task_repo

    fresh_parent = _task_repo.get_by_id(parent_task_id)
    if fresh_parent is not None:
        _task_repo.upsert_cache(fresh_parent)


def _collect_task_descendants(cursor, root_task_id) -> list[tuple[str, Any]]:
    """Return ``[(task_id, assigned_to), ...]`` for every descendant of
    ``root_task_id``, ordered so front-to-back deletion never violates the
    ``tasks.parent_task`` self-FK (deepest descendants first).

    Source of truth is the ``parent_task`` FK column — NOT the
    ``child_tasks`` JSON mirror — so ``force_delete`` cascades correctly
    even when the mirror has drifted (BL-2). A ``seen`` set guards against
    a malformed parent cycle.
    """
    ordered: list[tuple[str, Any]] = []  # BFS order: parent before child
    seen: set[str] = {root_task_id}
    frontier = [root_task_id]
    while frontier:
        next_frontier: list[str] = []
        for tid in frontier:
            cursor.execute(
                "SELECT task_id, assigned_to FROM tasks WHERE parent_task = ?",
                (tid,),
            )
            for r in cursor.fetchall():
                child_id = r["task_id"]
                if child_id in seen:
                    continue
                seen.add(child_id)
                ordered.append((child_id, r["assigned_to"]))
                next_frontier.append(child_id)
        frontier = next_frontier
    ordered.reverse()  # deepest first → safe delete order under the FK
    return ordered


def _authorize_assign_task(
    *,
    admin_auth_token: Optional[str],
    target_agent_token: Optional[str],
    task_ids: Optional[List[str]],
    arguments: Dict[str, Any],
    principal: Principal,
) -> Optional[str]:
    """Authorize a call to `assign_task_tool_impl`.

    Returns `None` if the call is permitted; otherwise returns the
    error message string to surface to the caller (the caller wraps
    it in a `PermissionDenied(reason=...)` so this helper stays
    unit-testable).

    Wave 9 PR 3: the manager-tier admit (operator-session, sysadmin,
    or manager-role agent) is gated by ``has_capability("tasks.assign")``
    — the capability marker present in both ``PROJECT_ROLE_BUNDLES["operator"]``
    and ``AGENT_ROLE_BUNDLES["manager"]`` (and short-circuited by the
    sysadmin wildcard). Replaces the legacy
    ``has_role("admin") or has_role("manager")`` per the Wave 9 design.

    Permission matrix:

    - admin (operator-session / sysadmin) → always permitted (carries
      ``tasks.assign`` via the operator bundle or wildcard).
    - manager-role agent token → always permitted (Phase 2 Wave 3,
      plan §2c: managers can assign tasks to other agents — the
      supervision-tier feature that distinguishes manager from
      worker; ``tasks.assign`` is the manager-role bundle's marker).
    - worker token + no `target_agent_token` (Mode 0, file
      unassigned) → gated by `config_allow_worker_create_unassigned`
      (default true). Tags `arguments["_worker_created_by"]` so the
      creator field records the worker, not "admin".
    - worker token + `target_agent_token == own token` + non-empty
      `task_ids` (Mode 3, self-claim existing unassigned task) →
      gated by `config_allow_worker_self_assign` (default true).
    - worker token + `target_agent_token == own token` + no
      `task_ids` (would be Mode 1/2 = create-and-assign-to-self) →
      rejected; the supported worker self-claim flow is Mode 3.
    - worker token + `target_agent_token != own token` → always
      rejected. Worker→worker delegation is operator/manager-only.
    """
    is_admin_or_manager = principal.has_capability("tasks.assign")
    worker_id = (
        principal.agent_id if principal.kind == "agent_bearer" else None
    )

    if is_admin_or_manager:
        # Strip any client-supplied provenance tag. ``_worker_created_by``
        # is an internal marker THIS function sets for the worker Mode-0
        # path; an operator/manager caller must never be able to smuggle
        # it in via raw arguments to forge the ``created_by`` attribution
        # on a Mode-0 unassigned task (it would otherwise flow straight
        # into ``_create_unassigned_tasks``'s ``creator``).
        arguments.pop("_worker_created_by", None)
        return None

    if not worker_id:
        return "Unauthorized: Admin token required"

    # Mode 0: worker files an unassigned task.
    if not target_agent_token:
        if not _access._get_config_bool(
            "config_allow_worker_create_unassigned", default=True
        ):
            return (
                "Unauthorized: worker self-filing of unassigned tasks "
                "is disabled by project policy "
                "(config_allow_worker_create_unassigned=false). Ask "
                "admin to enable it in dashboard Settings."
            )
        arguments["_worker_created_by"] = worker_id
        return None

    # `target_agent_token` is set. Identify whether the worker is
    # targeting themselves.
    target_agent_id = get_agent_id(target_agent_token)
    targeting_self = (
        target_agent_id is not None and target_agent_id == worker_id
    )

    if not targeting_self:
        return (
            "Unauthorized: workers can only assign tasks to themselves "
            "(use config_allow_worker_self_assign + "
            "agent_token=<your own>)"
        )

    # Self-targeted. We only support the self-claim flow (Mode 3
    # with `task_ids`) — create-and-assign-to-self isn't a supported
    # worker path today; tell the caller why.
    if not task_ids:
        return (
            "Unauthorized: workers may only self-claim existing "
            "unassigned tasks (pass task_ids=[...]); create-and-"
            "assign-to-self is not supported. File the task with no "
            "agent_token and then claim it."
        )

    if not _access._get_config_bool(
        "config_allow_worker_self_assign", default=True
    ):
        return (
            "Unauthorized: worker self-assignment is disabled "
            "(config_allow_worker_self_assign=false). Ask admin to "
            "enable it in dashboard Settings."
        )

    return None


def estimate_tokens(text: str) -> int:
    """Accurate token estimation using tiktoken for GPT-4"""
    try:
        import tiktoken

        encoding = tiktoken.encoding_for_model("gpt-4")
        return len(encoding.encode(text))
    except ImportError:
        # Fallback to rough estimation if tiktoken not available
        return len(text) // 4
    except Exception:
        # Fallback for any other tiktoken errors
        return len(text) // 4


def _generate_task_id() -> str:
    """Generates a unique task ID."""
    return f"task_{secrets.token_hex(6)}"


def _generate_notification_id() -> str:
    """Generates a unique notification ID."""
    return f"notification_{secrets.token_hex(8)}"


# --- Task status lifecycle -------------------------------------------------
#
# Terminal states are sinks: once a task reaches completed / cancelled /
# failed, no further status write is permitted — not even to the same
# terminal state. Without this guard a caller could double-complete a
# task (re-firing ``auto_update_dependencies``), un-complete it, or
# resurrect a cancelled/failed task. Enforced in both
# ``_update_single_task`` (the update_task_status path) and
# ``bulk_task_operations``.
_TERMINAL_TASK_STATUSES: set = {"completed", "cancelled", "failed"}


def _is_status_transition_allowed(old_status: Optional[str], new_status: str) -> bool:
    """Return True iff a task may move from ``old_status`` to ``new_status``.

    Rules:
      * A terminal source state (completed/cancelled/failed) is a sink —
        every outgoing transition is rejected, including a no-op write
        of the same terminal state (a re-complete would re-fire the
        dependency-advance side effects).
      * A same-state write on a non-terminal state is an idempotent
        no-op and is allowed (e.g. re-affirming ``in_progress`` while
        appending a note).
      * Any transition out of a non-terminal state is allowed.
    """
    if old_status == new_status:
        return old_status not in _TERMINAL_TASK_STATUSES
    if old_status in _TERMINAL_TASK_STATUSES:
        return False
    return True


def _agent_assignable(cursor, agent_id: str) -> bool:
    """True iff ``agent_id`` exists and is not terminated.

    Assignment targets must be live agents — a task pinned on a
    terminated agent is unreachable work (and, for the audit trail,
    attributes to a revoked identity).
    """
    cursor.execute(
        "SELECT 1 FROM agents WHERE agent_id = ? AND status != ?",
        (agent_id, "terminated"),
    )
    return cursor.fetchone() is not None


def _missing_capabilities(
    cursor,
    task_required_capabilities: Any,
    target_agent_id: str,
) -> List[str]:
    """Capabilities the target agent lacks for a task's required set.

    Single source of truth for the Mode-3 routing control
    ``required_capabilities ⊆ agent.capabilities`` (added round-1 in
    ``_assign_to_existing_tasks``). EVERY assign/reassign write site must
    call this before pinning a capability-tagged task onto an agent, so
    the control cannot be bypassed on a reassign path (AZ-R26-1). Returns
    the sorted list of missing capabilities (empty ⇒ satisfied).

    ``task_required_capabilities`` may be the raw DB TEXT column (a JSON
    string) or an already-decoded list. An empty/absent required set
    always satisfies (empty ⇒ no missing). A terminated/absent agent has
    no capabilities row, so the whole required set is reported missing.
    Both sides are normalized (lowercased) at write time.
    """
    if isinstance(task_required_capabilities, str):
        required = set(
            json.loads(task_required_capabilities)
            if task_required_capabilities
            else []
        )
    else:
        required = set(task_required_capabilities or [])
    if not required:
        return []
    cursor.execute(
        "SELECT capabilities FROM agents WHERE agent_id = ? AND status != ?",
        (target_agent_id, "terminated"),
    )
    row = cursor.fetchone()
    agent_caps = set(json.loads(row["capabilities"] or "[]")) if row else set()
    return sorted(required - agent_caps)


async def _update_single_task(
    cursor,
    task_id: str,
    new_status: str,
    requesting_agent_id: str,
    is_admin_request: bool,
    notes_content: Optional[str] = None,
    new_title: Optional[str] = None,
    new_description: Optional[str] = None,
    new_priority: Optional[str] = None,
    new_assigned_to: Optional[str] = None,
    new_depends_on_tasks: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Helper function to update a single task with smart features"""

    # Fetch task current data
    cursor.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
    task_db_row = cursor.fetchone()
    if not task_db_row:
        return {"success": False, "error": f"Task '{task_id}' not found"}

    task_current_data = dict(task_db_row)

    # Verify permissions.
    #
    # SECURITY (PF-1): a non-owner without ``tasks.assign`` must NOT be
    # able to tell "task exists but isn't yours" from "task doesn't
    # exist", and must never see the owning agent's id. Both are
    # differential-response oracles a worker can exploit — workers
    # routinely hold foreign task_ids (via ``depends_on_tasks`` /
    # ``parent_task``, coordination messages, ``view_tasks``) and cannot
    # otherwise enumerate the owning agent's identity. So return the
    # EXACT not-found result the missing-row branch above returns, with
    # no ``assigned_to`` interpolation.
    if (
        task_current_data.get("assigned_to") != requesting_agent_id
        and not is_admin_request
    ):
        return {"success": False, "error": f"Task '{task_id}' not found"}

    # Terminal-state / transition guard. Terminal states are sinks; a
    # double-complete would re-fire auto_update_dependencies and an
    # un-complete / resurrect would violate the lifecycle invariant.
    old_status = task_current_data.get("status")
    if not _is_status_transition_allowed(old_status, new_status):
        return {
            "success": False,
            "error": (
                f"Invalid status transition for task '{task_id}': "
                f"'{old_status}' -> '{new_status}' is not allowed "
                f"({old_status} is a terminal state)."
                if old_status in _TERMINAL_TASK_STATUSES
                else (
                    f"Invalid status transition for task '{task_id}': "
                    f"'{old_status}' -> '{new_status}' is not allowed."
                )
            ),
        }

    # Reassignment target validation (admin path only). A free-string
    # ``assigned_to`` would otherwise pin the task on a non-existent or
    # terminated agent.
    if is_admin_request and new_assigned_to is not None:
        if not _agent_assignable(cursor, new_assigned_to):
            return {
                "success": False,
                "error": (
                    f"Cannot reassign task '{task_id}' to "
                    f"'{new_assigned_to}': agent does not exist or is "
                    f"terminated."
                ),
            }
        # Capability-routing parity (AZ-R26-1): the canonical assign path
        # (``_assign_to_existing_tasks``) refuses to pin a
        # capability-tagged task onto an under-capable agent; the single
        # reassign path must enforce the SAME control or it becomes a
        # bypass. ``required_capabilities`` is unchanged by this update,
        # so check the task's stored tag against the new assignee.
        missing_caps = _missing_capabilities(
            cursor,
            task_current_data.get("required_capabilities"),
            new_assigned_to,
        )
        if missing_caps:
            return {
                "success": False,
                "error": (
                    f"Cannot reassign task '{task_id}' to "
                    f"'{new_assigned_to}': agent lacks required "
                    f"capabilities {missing_caps}."
                ),
            }

    updated_at_iso = datetime.datetime.now().isoformat()

    # PR 6: route the main UPDATE through task_repo with the caller's
    # cursor — the repo's allowlist enforces the same safe-field set
    # the legacy inline code did (status/notes/title/description/
    # priority/assigned_to/depends_on_tasks all live in _MUTABLE_FIELDS).
    from ..repositories import task_repo

    # Notes are append-only; build the new list here since the legacy
    # path did the same.
    current_notes_list = json.loads(task_current_data.get("notes") or "[]")
    if notes_content:
        current_notes_list.append(
            {
                "timestamp": updated_at_iso,
                "author": requesting_agent_id,
                "content": notes_content,
            }
        )

    fields_to_update: Dict[str, Any] = {
        "status": new_status,
        "notes": current_notes_list,
    }
    if is_admin_request:
        if new_title is not None:
            fields_to_update["title"] = new_title
        if new_description is not None:
            fields_to_update["description"] = new_description
        if new_priority is not None:
            fields_to_update["priority"] = new_priority
        if new_assigned_to is not None:
            fields_to_update["assigned_to"] = new_assigned_to
        if new_depends_on_tasks is not None:
            fields_to_update["depends_on_tasks"] = new_depends_on_tasks

    task_repo.update_fields(task_id, fields_to_update, connection=cursor)

    # Update in-memory cache — the repo defers cache write on the
    # connection= path because the caller's transaction is still open;
    # we apply the same per-field updates the legacy inline code did
    # so the cache reflects the in-flight transaction. A subsequent
    # rollback path doesn't exist in this helper's call sites (its
    # callers commit immediately after the helper returns).
    if task_id in g.tasks:
        g.tasks[task_id]["status"] = new_status
        g.tasks[task_id]["updated_at"] = updated_at_iso
        g.tasks[task_id]["notes"] = current_notes_list
        if is_admin_request:
            if new_title is not None:
                g.tasks[task_id]["title"] = new_title
            if new_description is not None:
                g.tasks[task_id]["description"] = new_description
            if new_priority is not None:
                g.tasks[task_id]["priority"] = new_priority
            if new_assigned_to is not None:
                g.tasks[task_id]["assigned_to"] = new_assigned_to
            if new_depends_on_tasks is not None:
                g.tasks[task_id]["depends_on_tasks"] = new_depends_on_tasks

    # Clear agents.current_task when the task it points at reaches a
    # terminal status. Before this guard, ios-app-dev (washing-brothers
    # production DB, 2026-06-04) had `agents.current_task` still
    # pointing at a long-completed task, which leaked into every
    # consumer of /api/all-data and the dashboard's "current task"
    # indicator. Scoped to `WHERE current_task = ?` so unrelated
    # agents (e.g. bob's row) are left alone.
    if new_status in ["completed", "cancelled", "failed"]:
        # PR 8 (Agent flip): filter-based bulk UPDATE goes through
        # agent_repo.clear_current_task_for so the cache mirror is
        # owned by the repo (the loop over state.active_agents below
        # used to be duplicated at every call site). The caller's
        # cursor is passed through so the UPDATE stays inside the
        # wider task-status BEGIN/COMMIT.
        from ..repositories import agent_repo as _agent_repo
        _agent_repo.clear_current_task_for(task_id, connection=cursor)

    # Handle parent task notifications
    if new_status in ["completed", "cancelled", "failed"] and task_current_data.get(
        "parent_task"
    ):
        parent_task_id = task_current_data["parent_task"]
        cursor.execute("SELECT notes FROM tasks WHERE task_id = ?", (parent_task_id,))
        parent_row = cursor.fetchone()
        if parent_row:
            parent_notes_list = json.loads(parent_row["notes"] or "[]")
            parent_notes_list.append(
                {
                    "timestamp": updated_at_iso,
                    "author": "system",
                    "content": f"Subtask '{task_id}' ({task_current_data.get('title', '')}) status changed to: {new_status}",
                }
            )
            # PR 7 (Task flip): parent-task note write now flows
            # through task_repo with the caller's cursor so the
            # allowlist + JSON serialisation rule live in one place.
            task_repo.update_fields(
                parent_task_id,
                {"notes": parent_notes_list},
                connection=cursor,
            )
            if parent_task_id in g.tasks:
                g.tasks[parent_task_id]["notes"] = parent_notes_list
                g.tasks[parent_task_id]["updated_at"] = updated_at_iso

    return {
        "success": True,
        "task_id": task_id,
        "old_status": task_current_data.get("status"),
        "new_status": new_status,
        "child_tasks": json.loads(task_current_data.get("child_tasks") or "[]"),
        "depends_on_tasks": json.loads(
            task_current_data.get("depends_on_tasks") or "[]"
        ),
    }


# --- Shared post-status reconcile / notify tail (BL-R26-1) -----------------
#
# The canonical single ``update_task_status`` path runs a three-phase tail
# after a successful status mutation: dependency-advance (Phase 3, inside
# the transaction), assignee-wake (Phase 2, post-commit), and RAG reindex
# (Phase 4, post-commit). ``bulk_task_operations`` historically skipped all
# three, so a bulk status→completed stalled dependents forever, never woke
# the reassign target, and left RAG stale. These helpers are the single
# source of truth for that tail so the single and bulk paths cannot drift
# apart again.


async def _advance_dependents_after_completion(
    cursor,
    completed_task_id: str,
    requesting_agent_id: str,
    is_admin_request: bool,
) -> List[Dict[str, Any]]:
    """Phase-3 dependency advance for one just-completed task.

    Finds every task that depends on ``completed_task_id``; when ALL of
    that dependent's dependencies are now completed and the dependent is
    still ``pending``, advances it to ``in_progress`` via
    ``_update_single_task`` (inside the caller's open transaction).
    Returns the advance-result dicts so the caller can wake/reindex them
    post-commit. Shared by the single + bulk status paths.
    """
    advanced: List[Dict[str, Any]] = []
    cursor.execute("SELECT task_id, depends_on_tasks FROM tasks")
    all_tasks = cursor.fetchall()
    for task_row in all_tasks:
        task_deps = json.loads(task_row["depends_on_tasks"] or "[]")
        if completed_task_id not in task_deps:
            continue
        # Every OTHER dependency of this dependent must also be complete.
        all_deps_completed = True
        for dep_id in task_deps:
            if dep_id == completed_task_id:
                continue
            cursor.execute(
                "SELECT status FROM tasks WHERE task_id = ?", (dep_id,)
            )
            dep_row = cursor.fetchone()
            if not dep_row or dep_row["status"] != "completed":
                all_deps_completed = False
                break
        if not all_deps_completed:
            continue
        cursor.execute(
            "SELECT status FROM tasks WHERE task_id = ?",
            (task_row["task_id"],),
        )
        dependent_task = cursor.fetchone()
        if dependent_task and dependent_task["status"] == "pending":
            dep_result = await _update_single_task(
                cursor,
                task_row["task_id"],
                "in_progress",
                requesting_agent_id,
                is_admin_request,
                "Auto-advanced: all dependencies completed",
                None,
                None,
                None,
                None,
                None,
            )
            advanced.append(dep_result)
    return advanced


def _wake_task_assignees(task_ids: List[str]) -> None:
    """Post-commit Phase-2 wake of each mutated task's current assignee.

    Prefers the in-memory cache (updated in-transaction by the write
    paths) with a lazy DB fallback for a task not held in cache. Deduped
    so bulk-updating one agent's tasks wakes them once. Best-effort — a
    notify failure must never poison an already-committed write. MUST run
    AFTER commit so re-reads observe the new state. Shared by the single
    + bulk status paths.
    """
    fallback_conn = None
    try:
        woken: set = set()
        for tid in task_ids:
            if not tid:
                continue
            assignee = None
            if tid in g.tasks:
                assignee = g.tasks[tid].get("assigned_to")
            if not assignee:
                if fallback_conn is None:
                    fallback_conn = get_db_connection()
                fc = fallback_conn.cursor()
                fc.execute(
                    "SELECT assigned_to FROM tasks WHERE task_id = ?",
                    (tid,),
                )
                row = fc.fetchone()
                if row:
                    assignee = row["assigned_to"]
            if assignee and assignee not in woken:
                g.notify_agent_inbox(assignee)
                woken.add(assignee)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("notify_agent_inbox fan-out raised: %s", e)
    finally:
        if fallback_conn is not None:
            fallback_conn.close()


def _reindex_tasks(task_ids: List[str]) -> None:
    """Post-commit Phase-4 RAG reindex of each mutated task.

    Reads the committed cache snapshot; a task absent from cache is
    skipped (nothing to index). Fire-and-forget via ``asyncio.create_task``,
    deduped. Shared by the single + bulk status paths.
    """
    import asyncio

    seen: set = set()
    for tid in task_ids:
        if not tid or tid in seen:
            continue
        seen.add(tid)
        if tid in g.tasks:
            asyncio.create_task(index_task_data(tid, g.tasks[tid].copy()))


# Filter / sort / dependency-analysis / health-metrics rules moved to
# ``agent_mcp/features/task_queries.py`` (TaskQueryEngine). The
# handler now consumes the engine directly.


# --- Helper functions for assign_task modes ---


async def _create_unassigned_tasks(
    arguments: Dict[str, Any],
) -> ToolResult:
    """Mode 0: Create unassigned tasks (assigned_to = NULL)"""
    task_title = arguments.get("task_title")
    task_description = arguments.get("task_description")
    tasks = arguments.get("tasks")
    priority = arguments.get("priority", "medium")
    parent_task_id_arg = arguments.get("parent_task_id")
    # Event-coord PR-1: top-level required_capabilities applies to
    # every task in the call (single + multi). For the multi path,
    # individual task entries may override via their own
    # `required_capabilities` key; otherwise they inherit the top-level
    # value. Normalize once at write time.
    from agent_mcp.utils.capability_normalization import normalize_capabilities

    top_level_required_caps_raw = arguments.get("required_capabilities")

    # Provenance (non-repudiation). ``_worker_created_by`` is tagged by
    # ``_authorize_assign_task`` when a *worker* files an unassigned task
    # (Mode 0, no agent_token). Operator / manager callers never carry
    # the tag, so they fall back to the historical "admin" attribution.
    # Resolving it here means BOTH the ``tasks.created_by`` column and
    # the ``agent_actions`` audit actor name the real creator, never the
    # forged literal "admin". Mirrors request_assistance_tool_impl.
    worker_created_by = arguments.get("_worker_created_by")
    creator = worker_created_by or "admin"

    # Single-root / parent-required guard. A worker on the Mode-0 path
    # must NOT be able to create parent-less ROOT tasks — that would
    # bypass the hierarchy invariant that create_self_task ("Agents can
    # NEVER create root tasks") and the Mode-1 root-count check enforce.
    # Operator/manager callers (no ``_worker_created_by`` tag) are
    # unaffected.
    if worker_created_by is not None:
        if tasks:
            if any(not t.get("parent_task_id") for t in tasks):
                return Conflict(
                    reason=(
                        "Workers cannot create root tasks. Every task filed "
                        "via assign_task must specify a parent_task_id."
                    )
                )
            worker_parent_ids = [t.get("parent_task_id") for t in tasks]
        elif not parent_task_id_arg:
            return Conflict(
                reason=(
                    "Workers cannot create root tasks. Specify a "
                    "parent_task_id when filing an unassigned task."
                )
            )
        else:
            worker_parent_ids = [parent_task_id_arg]

        # AZ-R19-1: a WORKER may only attach a child under a parent it
        # OWNS (parent.assigned_to == worker). Without this gate a worker
        # could inject an attacker-titled child under ANY foreign /
        # operator-owned parent, mutating the victim parent's child_tasks
        # JSON mirror (a cross-agent stored-injection primitive — the
        # victim sees an unexpected child appear under their task). Mirrors
        # the ownership gate ``add_task_note`` / ``request_assistance``
        # enforce (assigned_to == requesting agent). A FOREIGN *or*
        # NONEXISTENT parent collapses to the SAME phantom NotFound the
        # existence-oracle-safe siblings return (AZ-R17-1 / AZ-R18-1) so a
        # worker can't distinguish "not yours" from "doesn't exist".
        # Operator/manager callers never carry ``_worker_created_by`` (it's
        # stripped in ``_authorize_assign_task``), so ``worker_created_by``
        # is None for them and they keep the ability to parent under any
        # task — this gate is worker-only.
        from ..db.connection import get_db_connection_read

        _read_conn = get_db_connection_read()
        try:
            _read_cur = _read_conn.cursor()
            for _pid in worker_parent_ids:
                _read_cur.execute(
                    "SELECT assigned_to FROM tasks WHERE task_id = ?", (_pid,)
                )
                _prow = _read_cur.fetchone()
                if _prow is None or _prow["assigned_to"] != worker_created_by:
                    return NotFound(resource="task", identifier=_pid)
        finally:
            _read_conn.close()

    # Define the write operation as an async function
    async def write_operation():
        conn = None
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            created_tasks = []
            created_at = datetime.datetime.now().isoformat()

            # PR 6: task INSERTs now go through task_repo.create with
            # the caller's cursor so they stay atomic with the
            # agent_actions audit-log INSERT below. Cache reconciliation
            # happens after conn.commit() via task_repo.upsert_cache.
            from ..repositories import task_repo
            cached_dicts: list[dict] = []
            parents_to_refresh: set[str] = set()

            if tasks:
                # Multiple unassigned task creation
                for i, task in enumerate(tasks):
                    task_id = (
                        f"task_{int(datetime.datetime.now().timestamp() * 1000)}_{i}"
                    )
                    title = task["title"]
                    description = task["description"]
                    task_priority = task.get("priority", "medium")
                    parent_task = task.get("parent_task_id")
                    # Per-task override of top-level required_capabilities.
                    per_task_caps_raw = task.get(
                        "required_capabilities", top_level_required_caps_raw
                    )
                    normalized_caps = normalize_capabilities(per_task_caps_raw)

                    fresh = task_repo.create(
                        {
                            "task_id": task_id,
                            "title": title,
                            "description": description,
                            "assigned_to": None,
                            "created_by": creator,
                            "status": "unassigned",
                            "priority": task_priority,
                            "parent_task": parent_task,
                            "child_tasks": [],
                            "depends_on_tasks": [],
                            "notes": [],
                            "required_capabilities": (
                                normalized_caps if normalized_caps else None
                            ),
                        },
                        connection=cursor,
                    )
                    cached_dicts.append(fresh)

                    # BL-2: maintain the parent's child_tasks mirror.
                    if _link_child_to_parent(cursor, parent_task, task_id):
                        parents_to_refresh.add(parent_task)

                    log_agent_action_to_db(
                        cursor,
                        creator,
                        "created_unassigned_task",
                        task_id=task_id,
                        details={"title": title, "mode": "unassigned_multiple"},
                    )

                    created_tasks.append(
                        {"task_id": task_id, "title": title, "priority": task_priority}
                    )

            elif task_title and task_description:
                # Single unassigned task creation
                task_id = f"task_{int(datetime.datetime.now().timestamp() * 1000)}"
                normalized_caps = normalize_capabilities(top_level_required_caps_raw)

                fresh = task_repo.create(
                    {
                        "task_id": task_id,
                        "title": task_title,
                        "description": task_description,
                        "assigned_to": None,
                        "created_by": creator,
                        "status": "unassigned",
                        "priority": priority,
                        "parent_task": parent_task_id_arg,
                        "child_tasks": [],
                        "depends_on_tasks": [],
                        "notes": [],
                        "required_capabilities": (
                            normalized_caps if normalized_caps else None
                        ),
                    },
                    connection=cursor,
                )
                cached_dicts.append(fresh)

                # BL-2: maintain the parent's child_tasks mirror.
                if _link_child_to_parent(cursor, parent_task_id_arg, task_id):
                    parents_to_refresh.add(parent_task_id_arg)

                log_agent_action_to_db(
                    cursor,
                    creator,
                    "created_unassigned_task",
                    task_id=task_id,
                    details={"title": task_title, "mode": "unassigned_single"},
                )

                created_tasks.append(
                    {"task_id": task_id, "title": task_title, "priority": priority}
                )

            else:
                raise ValueError(
                    "Error: Provide either 'task_title' and 'task_description' for single task, or 'tasks' array for multiple tasks."
                )

            conn.commit()

            # Post-commit cache reconciliation through the repo. Mirrors
            # the legacy inline `g.tasks[task_id] = task_data` writes
            # but routes them through the repo so the cache invariant
            # is owned in one place.
            for d in cached_dicts:
                task_repo.upsert_cache(d)
            # BL-2: reconcile parents whose child_tasks mirror changed.
            for parent_id in parents_to_refresh:
                _refresh_parent_cache(parent_id)

            return created_tasks

        except Exception as e:
            if conn:
                conn.rollback()
            logger.error(f"Error creating unassigned tasks: {e}", exc_info=True)
            raise e
        finally:
            if conn:
                conn.close()

    # Execute the write operation through the queue
    try:
        created_tasks = await execute_db_write(write_operation)

        # PR-2 event-coord: fan out `unassigned_task_appeared` events
        # to every active agent whose capabilities satisfy the task's
        # required_capabilities. Per-task fanout (so a multi-task call
        # with heterogeneous capability requirements still wakes the
        # right subset per task). Wrapped in broad try/except so a
        # notification failure can't poison a successful task write.
        for task in created_tasks:
            try:
                task_id = task["task_id"]
                # Per-task required_capabilities (may have overridden
                # the top-level value in the multi path); resolve from
                # `g.tasks` (populated by `write_operation`) to keep
                # this branch independent of which arg path produced
                # the row.
                cached = g.tasks.get(task_id, {})
                raw_caps = cached.get("required_capabilities")
                if isinstance(raw_caps, str):
                    try:
                        caps_list = json.loads(raw_caps)
                    except Exception:
                        caps_list = []
                elif isinstance(raw_caps, list):
                    caps_list = raw_caps
                else:
                    caps_list = []
                g.notify_unassigned_task_appeared(task_id, caps_list)
            except Exception as e:  # pragma: no cover - defensive
                logger.warning(
                    "notify_unassigned_task_appeared(%s) failed: %s",
                    task.get("task_id"), e,
                )

        # Build response
        response_parts = [
            f"✅ **Unassigned Tasks Created**",
            f"   Tasks Created: {len(created_tasks)}",
            f"   Status: Unassigned",
            "",
        ]

        for i, task in enumerate(created_tasks, 1):
            response_parts.append(
                f"   {i}. {task['task_id']}: {task['title']} (Priority: {task['priority']})"
            )

        response_parts.append(
            "\n💡 Use assign_task with task_ids parameter to assign these tasks to agents."
        )

        return Ok(message="\n".join(response_parts))

    except ValueError as e:
        return Invalid(message=str(e))
    except Exception as e:
        return Failed(message=f"Error creating unassigned tasks: {e}")


async def _assign_to_existing_tasks(
    arguments: Dict[str, Any],
    target_agent_id: str,
    task_ids: List[str],
    validate_agent_workload: bool,
    coordination_notes: str,
    requesting_actor: str = "admin",
    is_admin_request: bool = False,
) -> ToolResult:
    """Mode 3: Assign agent to existing unassigned tasks.

    ``requesting_actor`` is the real principal actor label (the worker
    for a self-claim, the operator/admin when they assign on a
    worker's behalf) — used for audit attribution instead of a
    hardcoded ``"admin"`` so the trail reflects who actually claimed
    the work (OBS-R17-AZ, same provenance family as the Mode-0 fix).

    ``is_admin_request`` is the caller's ``tasks.assign`` capability
    (operator / manager / sysadmin). It gates the informative-vs-phantom
    error split below:

      * SEC-R18 (AZ-R18-1) — for a NON-admin self-claim caller EVERY
        non-claimable outcome (nonexistent task, task assigned to
        another, terminal-status task, or capability-mismatch)
        collapses to the IDENTICAL phantom ``NotFound`` the nonexistent
        branch returns — no owner id, no existence signal. This closes
        the last worker-reachable sibling of the uniform-phantom
        existence-oracle class (AZ-R17-1 et al): a worker can no longer
        enumerate which task_ids exist, nor read a foreign task's
        assignee.
      * SEC-R18 (BL-R18-1) — a TERMINAL task (completed/cancelled/
        failed) is non-claimable on the ASSIGN axis too, mirroring the
        status-axis terminal sink in ``_update_single_task`` /
        ``_is_status_transition_allowed``. A terminal-but-unassigned
        task (reachable by admin-cancelling an unclaimed task, or the
        BL-R17-2 purge clearing a terminal task's assignee) must not be
        re-claimable and re-executed.

    Admin/manager callers keep the real, informative errors
    (Conflict-with-owner, PermissionDenied, and an informative terminal
    block) — the phantom collapse is ONLY for the non-admin self-claim
    oracle.
    """
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Validate that all tasks exist and are unassigned. ``status`` is
        # SELECTed so the terminal-sink check below can see terminal
        # state (BL-R18-1).
        placeholders = ",".join(["?" for _ in task_ids])
        cursor.execute(
            f"SELECT task_id, title, assigned_to, required_capabilities, status "
            f"FROM tasks WHERE task_id IN ({placeholders})",
            task_ids,
        )
        found_tasks = cursor.fetchall()

        # SEC-R18 phantom NotFound. For a non-admin self-claim caller,
        # every non-claimable outcome collapses to this — byte-identical
        # to the nonexistent-task branch, with the identifier being the
        # id(s) the caller asked for (never the missing subset, which
        # would itself signal which of a batch exists). See the function
        # docstring for the AZ-R18-1 / BL-R18-1 rationale.
        phantom_not_found = NotFound(
            resource="task", identifier=", ".join(task_ids)
        )

        if len(found_tasks) != len(task_ids):
            if not is_admin_request:
                return phantom_not_found
            found_ids = [task["task_id"] for task in found_tasks]
            missing_ids = [tid for tid in task_ids if tid not in found_ids]
            return NotFound(resource="task", identifier=", ".join(missing_ids))

        # Check for already assigned tasks
        assigned_tasks = [
            task for task in found_tasks if task["assigned_to"] is not None
        ]
        if assigned_tasks:
            if not is_admin_request:
                return phantom_not_found
            assigned_list = [
                f"{task['task_id']} (assigned to {task['assigned_to']})"
                for task in assigned_tasks
            ]
            return Conflict(
                reason=f"some tasks are already assigned: {', '.join(assigned_list)}"
            )

        # Terminal-sink on the assign axis (BL-R18-1). A terminal task
        # (completed/cancelled/failed) is finished work; re-assigning it
        # would let the claimant re-execute it. Terminal is a sink here
        # exactly as it is on the status axis
        # (``_is_status_transition_allowed`` / ``_update_single_task``).
        # Non-admin callers get the phantom NotFound; admins get an
        # informative block (never a silent claim).
        terminal_tasks = [
            task
            for task in found_tasks
            if task["status"] in _TERMINAL_TASK_STATUSES
        ]
        if terminal_tasks:
            if not is_admin_request:
                return phantom_not_found
            terminal_list = [
                f"{task['task_id']} ({task['status']})"
                for task in terminal_tasks
            ]
            return Conflict(
                reason=(
                    "cannot assign task(s) in a terminal state "
                    f"(terminal states are a sink): {', '.join(terminal_list)}"
                )
            )

        # Validate agent exists and is not terminated. A terminated
        # target makes the task unreachable work and misattributes the
        # audit trail to a revoked identity.
        cursor.execute(
            "SELECT capabilities FROM agents WHERE agent_id = ? AND status != ?",
            (target_agent_id, "terminated"),
        )
        agent_caps_row = cursor.fetchone()
        if not agent_caps_row:
            return NotFound(resource="agent", identifier=target_agent_id)

        # Capability-routing enforcement (Mode-3 self-claim). A caller
        # that learns a task_id must not claim work it lacks the
        # capabilities for: enforce required_capabilities ⊆ agent
        # capabilities. Empty required_capabilities always passes. The
        # subset check is factored into ``_missing_capabilities`` so
        # every reassign path enforces the SAME control (AZ-R26-1).
        for task in found_tasks:
            missing = _missing_capabilities(
                cursor, task["required_capabilities"], target_agent_id
            )
            if missing:
                # SEC-R18: a non-admin self-claim caller must not learn
                # the task exists via a capability-mismatch signal —
                # collapse to the phantom NotFound. Admins keep the
                # informative PermissionDenied.
                if not is_admin_request:
                    return phantom_not_found
                return PermissionDenied(
                    reason=(
                        f"agent '{target_agent_id}' lacks required "
                        f"capabilities for task {task['task_id']}: "
                        f"{missing}"
                    )
                )

        # PR 6: task assignment UPDATEs go through task_repo with the
        # caller's cursor so they're atomic with the audit-log
        # INSERTs. The repo defers cache + publish on the
        # connection= path; we reconcile after commit below.
        from ..repositories import agent_repo, task_repo
        for task_id in task_ids:
            task_repo.update_fields(
                task_id, {"assigned_to": target_agent_id},
                connection=cursor,
            )
            log_agent_action_to_db(
                cursor,
                requesting_actor,
                "assigned_task",
                task_id=task_id,
                details={
                    "agent_id": target_agent_id,
                    "mode": "existing_task_assignment",
                },
            )

        # Update agent's current task if they don't have one (use first task)
        cursor.execute(
            "SELECT current_task FROM agents WHERE agent_id = ?", (target_agent_id,)
        )
        agent_row = cursor.fetchone()
        if agent_row and agent_row["current_task"] is None:
            agent_repo.update_field(
                target_agent_id, "current_task", task_ids[0],
                connection=cursor,
            )

        conn.commit()

        # Post-commit cache reconciliation through the repos.
        for task_id in task_ids:
            fresh = task_repo.get_by_id(task_id)
            if fresh is not None:
                task_repo.upsert_cache(fresh)

        # Wake wait_for_events waiter + fan out resources/updated to
        # every registered GET /mcp stream for the newly-assigned agent.
        try:
            g.notify_agent_inbox(target_agent_id)
        except Exception as e:  # pragma: no cover - defensive
            logger.warning(
                "notify_agent_inbox(%s) raised after _assign_to_existing_tasks: %s",
                target_agent_id, e,
            )

        # Build response
        task_titles = [task["title"] for task in found_tasks]
        response_parts = [
            f"✅ **Tasks Assigned Successfully**",
            f"   Agent: {target_agent_id}",
            f"   Tasks Assigned: {len(task_ids)}",
            "",
        ]

        for i, (task_id, title) in enumerate(zip(task_ids, task_titles), 1):
            response_parts.append(f"   {i}. {task_id}: {title}")

        if coordination_notes:
            response_parts.append(f"\n📋 **Coordination Notes:** {coordination_notes}")

        return Ok(message="\n".join(response_parts))

    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Error assigning existing tasks: {e}", exc_info=True)
        return Failed(message=f"Error assigning tasks: {e}")
    finally:
        if conn:
            conn.close()


async def _create_and_assign_multiple_tasks(
    arguments: Dict[str, Any],
    target_agent_id: str,
    tasks: List[Dict[str, Any]],
    auto_suggest_parent: bool,
    validate_agent_workload: bool,
    coordination_notes: str,
) -> ToolResult:
    """Mode 2: Create multiple tasks and assign to agent"""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Validate agent exists and is not terminated.
        if not _agent_assignable(cursor, target_agent_id):
            return NotFound(resource="agent", identifier=target_agent_id)

        created_tasks = []
        created_at = datetime.datetime.now().isoformat()

        # PR 6: task INSERTs go through task_repo.create with the
        # caller's cursor — atomic with agent_actions audit log.
        # Cache reconciliation deferred to post-commit.
        from ..repositories import agent_repo, task_repo
        cached_dicts: list[dict] = []
        parents_to_refresh: set[str] = set()

        # Create each task
        for i, task in enumerate(tasks):
            task_id = f"task_{int(datetime.datetime.now().timestamp() * 1000)}_{i}"
            title = task["title"]
            description = task["description"]
            priority = task.get("priority", "medium")
            parent_task = task.get("parent_task_id")

            fresh = task_repo.create(
                {
                    "task_id": task_id,
                    "title": title,
                    "description": description,
                    "assigned_to": target_agent_id,
                    "created_by": "admin",
                    "status": "pending",
                    "priority": priority,
                    "parent_task": parent_task,
                    "child_tasks": [],
                    "depends_on_tasks": [],
                    "notes": [],
                },
                connection=cursor,
            )
            cached_dicts.append(fresh)

            # BL-2: maintain the parent's child_tasks mirror.
            if _link_child_to_parent(cursor, parent_task, task_id):
                parents_to_refresh.add(parent_task)

            # Log the creation
            log_agent_action_to_db(
                cursor,
                "admin",
                "assigned_task",
                task_id=task_id,
                details={
                    "agent_id": target_agent_id,
                    "title": title,
                    "mode": "multiple_task_creation",
                },
            )

            created_tasks.append(
                {"task_id": task_id, "title": title, "priority": priority}
            )

        # Update agent's current task if they don't have one (use first task)
        cursor.execute(
            "SELECT current_task FROM agents WHERE agent_id = ?", (target_agent_id,)
        )
        agent_row = cursor.fetchone()
        if agent_row and agent_row["current_task"] is None and created_tasks:
            agent_repo.update_field(
                target_agent_id, "current_task", created_tasks[0]["task_id"],
                connection=cursor,
            )

        conn.commit()

        # Post-commit cache reconciliation through the repo.
        for d in cached_dicts:
            task_repo.upsert_cache(d)
        # BL-2: reconcile parents whose child_tasks mirror changed.
        for parent_id in parents_to_refresh:
            _refresh_parent_cache(parent_id)

        # Wake wait_for_events waiter + fan out resources/updated to
        # every registered GET /mcp stream for the newly-assigned agent.
        try:
            g.notify_agent_inbox(target_agent_id)
        except Exception as e:  # pragma: no cover - defensive
            logger.warning(
                "notify_agent_inbox(%s) raised after _create_and_assign_multiple_tasks: %s",
                target_agent_id, e,
            )

        # Build response
        response_parts = [
            f"✅ **Multiple Tasks Created and Assigned**",
            f"   Agent: {target_agent_id}",
            f"   Tasks Created: {len(created_tasks)}",
            "",
        ]

        for i, task in enumerate(created_tasks, 1):
            response_parts.append(
                f"   {i}. {task['task_id']}: {task['title']} (Priority: {task['priority']})"
            )

        if coordination_notes:
            response_parts.append(f"\n📋 **Coordination Notes:** {coordination_notes}")

        return Ok(message="\n".join(response_parts))

    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Error creating multiple tasks: {e}", exc_info=True)
        return Failed(message=f"Error creating multiple tasks: {e}")
    finally:
        if conn:
            conn.close()


# --- assign_task tool ---
# Original logic from main.py: lines 1319-1384 (assign_task_tool function)
# assign_task: admin always; worker iff at least one of the two
# worker-paths-policy toggles is on (self-claim via Mode 3, or
# file-unassigned via Mode 0). The per-mode arbitration stays in
# `_authorize_assign_task` below; the decorator only ensures we don't
# let an anonymous caller in.
@requires_policy(
    "config_allow_worker_self_assign",
    "config_allow_worker_create_unassigned",
    default=True,
)
async def assign_task_tool_impl(
    arguments: Dict[str, Any],
    *,
    principal: Optional[Principal] = None,
) -> ToolResult:
    admin_auth_token = arguments.get("token")
    target_agent_token = arguments.get("agent_token")
    target_agent_id_alias = arguments.get("agent_id")

    # Wave 9 PR 3: ``is_admin_request`` admits the supervision-tier
    # (operator-session / sysadmin / manager-role agent) via the
    # ``tasks.assign`` capability — the cap is granted by both
    # ``PROJECT_ROLE_BUNDLES["operator"]`` and
    # ``AGENT_ROLE_BUNDLES["manager"]`` and short-circuited by the
    # sysadmin wildcard. Workers (and viewer-tier operators) lack the
    # cap and continue to fall through to the per-mode arbitration in
    # ``_authorize_assign_task``.
    is_admin_request = principal.has_capability("tasks.assign")

    # Admin-only `agent_id` alternative (Phase 7d). Resolves to the
    # agent's token server-side so admins can target an agent by their
    # human-readable name in a single call, retiring the need for the
    # old `create_task_for` router synthetic.
    #
    # Precedence: if both `agent_id` and `agent_token` are supplied,
    # `agent_token` wins and `agent_id` is silently ignored — keeps
    # mixed-bearer setups (e.g. a worker happens to know their own
    # agent_id) from erroring noisily.
    #
    # Worker bearers may NOT use `agent_id` at all; they must pass
    # their own `agent_token`. Otherwise a worker could pass an
    # arbitrary agent_id and impersonate the admin-side resolution
    # path.
    if target_agent_id_alias and not target_agent_token:
        if not is_admin_request:
            return PermissionDenied(
                reason=(
                    "agent_id parameter is admin-only; "
                    "workers must pass agent_token (their own token)"
                )
            )
        # PR 6: routed through agent_repo (drops raw cursor that only
        # did a PK lookup). disable_cache to force a DB read — the
        # cache shape (state.active_agents) is token-keyed and the
        # cached value dicts don't carry the `token` field; the DB
        # projection does.
        from ..repositories import agent_repo
        with agent_repo.disable_cache():
            row = agent_repo.get_by_id(target_agent_id_alias)
        # get_by_id RETURNS terminated rows (for audit) but never caches
        # them; treat a terminated agent as unassignable here.
        if not row or row.get("status") == "terminated":
            # Use Invalid with the legacy "Unknown agent_id" wording so
            # the rendered wire text continues to contain that prefix
            # (callers + tests grep for it).
            return Invalid(
                field="agent_id",
                message=f"Unknown agent_id: '{target_agent_id_alias}'",
            )
        target_agent_token = row["token"]

    # Mode 1: Single task creation (existing behavior)
    task_title = arguments.get("task_title")
    task_description = arguments.get("task_description")
    priority = arguments.get("priority", "medium")  # Default from schema
    depends_on_tasks_list = arguments.get("depends_on_tasks")  # List[str] or None
    parent_task_id_arg = arguments.get("parent_task_id")  # Optional str

    # Mode 2: Multiple task creation (new)
    tasks = arguments.get("tasks")  # List[Dict] with task details

    # Mode 3: Existing task assignment (new)
    task_ids = arguments.get("task_ids")  # List[str] of existing task IDs

    # Smart coordination features
    auto_suggest_parent = arguments.get(
        "auto_suggest_parent", True
    )  # Auto-suggest parent tasks
    validate_agent_workload = arguments.get(
        "validate_agent_workload", True
    )  # Check agent capacity
    auto_schedule = arguments.get(
        "auto_schedule", False
    )  # Auto-schedule based on dependencies
    coordination_notes = arguments.get(
        "coordination_notes"
    )  # Optional coordination context
    estimated_hours = arguments.get("estimated_hours")  # Optional workload estimation
    # VULN-004: explicit opt-in to apply validator-suggested
    # parent_task / dependencies. Default False — when the validator
    # returns suggestions, surface them as text instead of silently
    # mutating the request. The validator's RAG corpus includes
    # project_context entries any agent can write, so an
    # attacker-poisoned entry could otherwise jailbreak the validator
    # into rerouting a victim's task to an attacker-chosen parent.
    #
    # Defense-in-depth (audit-A INFO-001): use ``is True`` rather than
    # ``bool()`` so any non-True value — including the strings
    # ``"true"``/``"false"`` and integer ``1`` — is rejected. Real MCP
    # clients are protected by the registry's jsonschema validation
    # (``"type": "boolean"``), but in-process callers that bypass the
    # dispatcher would trip ``bool("false") == True`` otherwise.
    _raw_accept = arguments.get("accept_suggestions", False)
    accept_suggestions = _raw_accept is True

    # Auth: admin can always call. Workers may call in a narrow set
    # of modes, each gated by its own per-project toggle. See
    # `_authorize_assign_task` for the full matrix.
    auth_error = _authorize_assign_task(
        admin_auth_token=admin_auth_token,
        target_agent_token=target_agent_token,
        task_ids=task_ids,
        arguments=arguments,
        principal=principal,
    )
    if auth_error is not None:
        return PermissionDenied(reason=auth_error.removeprefix("Unauthorized: "))

    # Handle unassigned task creation (agent_token is optional)
    if not target_agent_token:
        # Mode 0: Create unassigned tasks
        return await _create_unassigned_tasks(arguments)

    # PR 6: routed through AgentRepository — drops a raw cursor that
    # only does an indexed lookup by token. The repo's
    # get_by_token() uses the same DB seam (and reads through the
    # token-keyed cache when warm), so behaviour is wire-equivalent.
    from ..repositories import agent_repo
    agent_row = agent_repo.get_by_token(target_agent_token)
    if not agent_row:
        return NotFound(
            resource="agent_token",
            identifier="(invalid or unknown)",
        )

    target_agent_id = agent_row["agent_id"]

    # Prevent admin agents from being assigned tasks
    if target_agent_id.lower().startswith("admin"):
        return Conflict(
            reason=(
                "admin agents cannot be assigned tasks. Admin agents are "
                "for coordination and management only."
            )
        )

    # Determine operation mode and validate parameters (when agent_token provided)
    if task_ids:
        # Mode 3: Assign to existing tasks
        operation_mode = "existing"
        if not isinstance(task_ids, list) or not task_ids:
            return Invalid(
                field="task_ids",
                message="task_ids must be a non-empty list of task IDs.",
            )
    elif tasks:
        # Mode 2: Create multiple tasks + assign
        operation_mode = "multiple"
        if not isinstance(tasks, list) or not tasks:
            return Invalid(
                field="tasks",
                message="tasks must be a non-empty list of task objects.",
            )
        # Validate each task object
        for i, task in enumerate(tasks):
            if not isinstance(task, dict) or not all(
                [task.get("title"), task.get("description")]
            ):
                return Invalid(
                    field="tasks",
                    message=f"Task {i+1} must have 'title' and 'description' fields.",
                )
    else:
        # Mode 1: Single task creation (existing behavior)
        operation_mode = "single"
        if not all([task_title, task_description]):
            return Invalid(
                message=(
                    "task_title and task_description are required for single task "
                    "creation, or provide 'tasks' array for multiple tasks, or "
                    "'task_ids' for existing task assignment."
                ),
            )

    # Route to appropriate handler based on operation mode
    if operation_mode == "existing":
        # OBS-R17-AZ: thread the real principal actor into the Mode-3
        # audit trail. Mirrors the actor idiom used elsewhere in this
        # file (``agent_id or user_id or "admin"``): the worker's id on
        # a self-claim, ``"admin"`` for the operator/admin caller.
        requesting_actor = (
            (principal.agent_id or principal.user_id or "admin")
            if principal is not None
            else "admin"
        )
        return await _assign_to_existing_tasks(
            arguments,
            target_agent_id,
            task_ids,
            validate_agent_workload,
            coordination_notes,
            requesting_actor,
            is_admin_request,
        )
    elif operation_mode == "multiple":
        return await _create_and_assign_multiple_tasks(
            arguments,
            target_agent_id,
            tasks,
            auto_suggest_parent,
            validate_agent_workload,
            coordination_notes,
        )
    else:
        # operation_mode == "single" - continue with existing logic
        pass

    # Enforce single root task rule BEFORE any processing (Mode 1: Single task)
    if parent_task_id_arg is None:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) as count, GROUP_CONCAT(task_id) as root_ids FROM tasks WHERE parent_task IS NULL"
        )
        result = cursor.fetchone()
        root_count = result["count"]
        root_ids = result["root_ids"]

        if root_count > 0:
            if auto_suggest_parent:
                # Use smart parent suggestion
                parent_suggestions = _suggest_optimal_parent_task(
                    cursor, target_agent_id, task_description
                )

                suggestion_text = "\n🧠 **Smart Parent Suggestions:**\n"
                if parent_suggestions["has_suggestions"]:
                    for i, suggestion in enumerate(
                        parent_suggestions["suggestions"], 1
                    ):
                        suggestion_text += (
                            f"  {i}. {suggestion['task_id']}: {suggestion['title']}\n"
                        )
                        suggestion_text += f"     Status: {suggestion['status']} | Priority: {suggestion['priority']} | {suggestion['reason']}\n"
                else:
                    # Fallback to basic suggestions
                    cursor.execute(
                        """
                        SELECT task_id, title, status 
                        FROM tasks 
                        WHERE status IN ('pending', 'in_progress') AND assigned_to = ?
                        ORDER BY updated_at DESC
                        LIMIT 3
                    """,
                        (target_agent_id,),
                    )

                    basic_suggestions = cursor.fetchall()
                    if basic_suggestions:
                        suggestion_text += "  Based on agent's recent tasks:\n"
                        for task in basic_suggestions:
                            suggestion_text += f"  - {task['task_id']}: {task['title']} (status: {task['status']})\n"
                    else:
                        suggestion_text += (
                            "  No suitable parent tasks found for this agent.\n"
                        )
                        suggestion_text += "  Consider assigning to a different agent with active tasks.\n"
            else:
                # Basic suggestion fallback
                cursor.execute(
                    """
                    SELECT task_id, title, status 
                    FROM tasks 
                    WHERE status IN ('pending', 'in_progress')
                    ORDER BY 
                        CASE WHEN assigned_to = ? THEN 0 ELSE 1 END,
                        created_at DESC
                    LIMIT 5
                """,
                    (target_agent_id,),
                )

                suggestions = cursor.fetchall()
                suggestion_text = "\nSuggested parent tasks:\n"
                for task in suggestions:
                    suggestion_text += f"  - {task['task_id']}: {task['title']} (status: {task['status']})\n"

            conn.close()

            return Conflict(
                reason=(
                    f"Cannot create task without parent. {root_count} root "
                    f"task(s) already exist: {root_ids}\n\n"
                    f"You MUST specify a parent_task_id. Every task except "
                    f"the first must have a parent.\n"
                    f"{suggestion_text}\n"
                    f"💡 Use auto_suggest_parent=true for smarter suggestions "
                    f"based on task content.\n"
                    f"Use 'view_tasks' for complete task list, or use one of "
                    f"the suggestions above."
                )
            )

        conn.close()

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Check if agent exists (in memory or DB) - main.py:1331-1346
        agent_exists_in_memory = target_agent_id in g.agent_working_dirs
        assigned_agent_active_token: Optional[str] = None
        if agent_exists_in_memory:
            for tkn, data in g.active_agents.items():
                if data.get("agent_id") == target_agent_id:
                    assigned_agent_active_token = tkn
                    break

        if not agent_exists_in_memory:
            cursor.execute(
                "SELECT token FROM agents WHERE agent_id = ? AND status != ?",
                (target_agent_id, "terminated"),
            )
            row = cursor.fetchone()
            if not row:
                return NotFound(resource="agent", identifier=target_agent_id)
            # Agent exists in DB but not memory, can still assign task.
            logger.warning(
                f"Assigning task to agent {target_agent_id} found in DB but not active memory."
            )
            # assigned_agent_active_token remains None if not in active_agents

        # Explicit assignability gate. The in-memory presence check above
        # does NOT verify DB status, so a terminated-but-warm agent
        # (still in g.agent_working_dirs) would otherwise skip the
        # ``status != 'terminated'`` check entirely. Run it unconditionally.
        if not _agent_assignable(cursor, target_agent_id):
            return NotFound(resource="agent", identifier=target_agent_id)

        # Generate task ID and timestamps first
        new_task_id = _generate_task_id()
        created_at_iso = datetime.datetime.now().isoformat()
        status = "pending"

        # Check single root task rule
        if parent_task_id_arg is None:
            cursor.execute(
                "SELECT COUNT(*) as count, MIN(task_id) as root_id FROM tasks WHERE parent_task IS NULL"
            )
            result = cursor.fetchone()
            root_count = result["count"]
            existing_root_id = result["root_id"]

            if root_count > 0:
                logger.error(
                    f"Attempt to create second root task. Existing root: {existing_root_id}"
                )
                return Conflict(
                    reason=(
                        f"Cannot create root task. A root task already "
                        f"exists ({existing_root_id}). All new tasks must "
                        f"have a parent."
                    )
                )

        # Smart workload validation
        workload_analysis = None
        workload_warnings = []

        if validate_agent_workload:
            workload_analysis = _analyze_agent_workload(cursor, target_agent_id)

            if not workload_analysis["can_take_new_task"]:
                warning_msg = (
                    f"⚠️ Agent workload warning: {workload_analysis['capacity_status']} "
                )
                warning_msg += (
                    f"({workload_analysis['total_active_tasks']} active tasks, "
                )
                warning_msg += (
                    f"{workload_analysis['high_priority_tasks']} high priority)"
                )
                workload_warnings.append(warning_msg)

                if workload_analysis["recommendations"]:
                    workload_warnings.extend(
                        [
                            f"   💡 {rec}"
                            for rec in workload_analysis["recommendations"][:2]
                        ]
                    )

        # System 8: RAG Pre-Check for Task Placement
        final_parent_task_id = parent_task_id_arg
        final_depends_on_tasks = depends_on_tasks_list
        validation_performed = False
        validation_message = ""

        if ENABLE_TASK_PLACEMENT_RAG:
            validation_performed = True
            validation_result = await validate_task_placement(
                title=task_title,
                description=task_description,
                parent_task_id=parent_task_id_arg,
                depends_on_tasks=depends_on_tasks_list,
                created_by="admin",
                auth_token=admin_auth_token,
            )

            suggestion_message = format_suggestions_for_agent(
                validation_result, parent_task_id_arg, depends_on_tasks_list
            )

            # For denied status, block creation unless override is allowed.
            # This is an admin-tier kill switch — runs before the
            # accept_suggestions opt-in, because an operator with
            # ALLOW_RAG_OVERRIDE=False is telling the system "never
            # apply a denied-status suggestion, period".
            if validation_result["status"] == "denied" and not ALLOW_RAG_OVERRIDE:
                return PermissionDenied(
                    reason=(
                        f"Task creation BLOCKED by RAG validation:\n"
                        f"{suggestion_message}"
                    )
                )

            if validation_result["status"] != "approved":
                suggestions = validation_result["suggestions"]
                if not accept_suggestions:
                    # VULN-004: surface suggestions as text instead of
                    # mutating the request. The validator's RAG corpus
                    # can be poisoned via project_context entries, so
                    # any LLM-driven suggestion must be the caller's
                    # explicit decision — not an implicit auto-apply.
                    return Invalid(
                        field=None,
                        message=(
                            "Task placement validator suggests changes. "
                            "Re-submit with accept_suggestions=true to "
                            "apply them, or adjust your request based "
                            f"on these suggestions:\n\n{suggestion_message}"
                        ),
                    )

                # accept_suggestions=True: caller explicitly consented
                # to apply the validator's suggested parent / deps.
                validation_message = (
                    f"\nRAG Validation ({validation_result['status']}):\n"
                    f"{suggestion_message}\n"
                )
                if suggestions.get("parent_task") is not None:
                    final_parent_task_id = suggestions["parent_task"]
                    validation_message += (
                        f"✓ Applied suggested parent: {final_parent_task_id}\n"
                    )
                if suggestions.get("dependencies"):
                    final_depends_on_tasks = suggestions["dependencies"]
                    validation_message += (
                        f"✓ Applied suggested dependencies: {final_depends_on_tasks}\n"
                    )

                logger.info(
                    f"RAG suggestions applied (explicitly accepted) "
                    f"for task {new_task_id}"
                )
            else:
                validation_message = "\n✓ RAG validation approved placement\n"

        # Build initial notes with coordination information
        initial_notes = []

        # Add coordination notes if provided
        if coordination_notes:
            initial_notes.append(
                {
                    "timestamp": created_at_iso,
                    "author": "admin",
                    "content": f"📋 Coordination: {coordination_notes}",
                }
            )

        # Add workload information
        if workload_analysis:
            workload_note = (
                f"👤 Agent workload: {workload_analysis['capacity_status']} "
            )
            workload_note += f"({workload_analysis['total_active_tasks']} active tasks)"
            if estimated_hours:
                workload_note += f" | Estimated: {estimated_hours}h"
            initial_notes.append(
                {
                    "timestamp": created_at_iso,
                    "author": "system",
                    "content": workload_note,
                }
            )

        # Add smart parent suggestion note if used
        if auto_suggest_parent and final_parent_task_id:
            initial_notes.append(
                {
                    "timestamp": created_at_iso,
                    "author": "system",
                    "content": f"🧠 Smart assignment: Parent task suggested based on content similarity",
                }
            )

        # Event-coord PR-1: normalize required_capabilities at write
        # time. None / missing key ⇒ store NULL ("anyone can claim",
        # though this is the assigned path so the field is informational
        # for routing on future reassignment / unassign).
        from agent_mcp.utils.capability_normalization import normalize_capabilities

        _norm_caps = normalize_capabilities(
            arguments.get("required_capabilities")
        )
        _required_caps_json = json.dumps(_norm_caps) if _norm_caps else None

        # TOCTOU recheck (terminate-reconcile). ``validate_task_placement``
        # above yields on a RAG await; a concurrent ``terminate_agent``
        # can commit in that window, after the assignability gate passed
        # but before this INSERT — pinning the task on a terminated agent.
        # Re-run the gate in-transaction, immediately before the write,
        # so the just-terminated agent can't receive the task.
        if not _agent_assignable(cursor, target_agent_id):
            return Conflict(
                reason=(
                    f"Cannot assign task to '{target_agent_id}': agent was "
                    f"terminated during task placement. Re-issue against a "
                    f"live agent."
                )
            )

        # SECURITY (AZ-R26-1): capability-routing parity at create time.
        # This Mode-1 create+assign path tags the new task with
        # ``required_capabilities`` AND pins it on ``target_agent_id`` in
        # one call — so a caps-tagged task could land on an under-capable
        # agent, the same routing-control bypass the reassign paths close.
        # Enforce the SAME subset check the canonical assign path uses; an
        # admin/operator caller gets the informative refusal.
        missing_caps = _missing_capabilities(
            cursor, _norm_caps, target_agent_id
        )
        if missing_caps:
            return PermissionDenied(
                reason=(
                    f"Cannot assign task to '{target_agent_id}': agent "
                    f"lacks required capabilities {missing_caps}."
                )
            )

        # PR 6: task INSERT goes through task_repo with the caller's
        # cursor so it's atomic with the agent UPDATE and audit log.
        from ..repositories import agent_repo, task_repo

        fresh_task = task_repo.create(
            {
                "task_id": new_task_id,
                "title": task_title,
                "description": task_description,
                "assigned_to": target_agent_id,
                "created_by": "admin",
                "status": status,
                "priority": priority,
                "parent_task": final_parent_task_id,
                "child_tasks": [],
                "depends_on_tasks": final_depends_on_tasks or [],
                "notes": initial_notes,
                "required_capabilities": (
                    _norm_caps if _norm_caps else None
                ),
            },
            connection=cursor,
        )

        # BL-2: maintain the parent's child_tasks mirror.
        parent_mirror_updated = _link_child_to_parent(
            cursor, final_parent_task_id, new_task_id
        )

        # Update agent's current task in DB if they don't have one (main.py:1376-1387)
        should_update_agent_current_task = False
        if (
            assigned_agent_active_token
            and assigned_agent_active_token in g.active_agents
        ):
            if g.active_agents[assigned_agent_active_token].get("current_task") is None:
                should_update_agent_current_task = True
        else:  # Agent not in active memory, check DB
            cursor.execute(
                "SELECT current_task FROM agents WHERE agent_id = ?", (target_agent_id,)
            )
            agent_row = cursor.fetchone()
            if agent_row and agent_row["current_task"] is None:
                should_update_agent_current_task = True

        if should_update_agent_current_task:
            # PR 6: routed through agent_repo with caller's cursor.
            agent_repo.update_field(
                target_agent_id, "current_task", new_task_id,
                connection=cursor,
            )

        log_agent_action_to_db(
            cursor,
            "admin",
            "assigned_task",
            task_id=new_task_id,
            details={"agent_id": target_agent_id, "title": task_title},
        )
        conn.commit()

        # Post-commit cache reconciliation.
        task_repo.upsert_cache(fresh_task)
        # BL-2: reconcile the parent whose child_tasks mirror changed.
        if parent_mirror_updated:
            _refresh_parent_cache(final_parent_task_id)

        # Wake wait_for_events waiter + fan out resources/updated to
        # every registered GET /mcp stream for the new assignee. Done
        # AFTER commit so the re-query sees the row.
        try:
            g.notify_agent_inbox(target_agent_id)
        except Exception as e:  # pragma: no cover - defensive
            logger.warning(
                "notify_agent_inbox(%s) raised after assign_task: %s",
                target_agent_id, e,
            )

        # Update agent's current task in memory if needed (main.py:1390-1391)
        if (
            should_update_agent_current_task
            and assigned_agent_active_token
            and assigned_agent_active_token in g.active_agents
        ):
            g.active_agents[assigned_agent_active_token]["current_task"] = new_task_id

        # PR 6: in-memory cache already reconciled by task_repo.upsert_cache
        # above; fresh_task carries the dict shape consumers expect
        # (JSON list fields deserialised). Use it for the RAG index call.
        index_data = dict(fresh_task)
        index_data["depends_on_tasks"] = final_depends_on_tasks or []
        # Start indexing asynchronously (fire and forget)
        import asyncio

        asyncio.create_task(index_task_data(new_task_id, index_data))

        log_audit(
            "admin",
            "assign_task",
            {"task_id": new_task_id, "agent_id": target_agent_id, "title": task_title},
        )  # main.py:1404
        logger.info(
            f"Task '{new_task_id}' ({task_title}) assigned to agent '{target_agent_id}'."
        )

        # Build comprehensive response
        response_parts = [f"✅ **Task Assigned Successfully**"]
        response_parts.append(f"   Task ID: {new_task_id}")
        response_parts.append(f"   Title: {task_title}")
        response_parts.append(f"   Agent: {target_agent_id}")
        response_parts.append(f"   Priority: {priority}")

        if final_parent_task_id:
            response_parts.append(f"   Parent: {final_parent_task_id}")

        if final_depends_on_tasks:
            response_parts.append(
                f"   Dependencies: {', '.join(final_depends_on_tasks)}"
            )

        if estimated_hours:
            response_parts.append(f"   Estimated: {estimated_hours} hours")

        # Add workload analysis
        if workload_analysis:
            response_parts.append("")
            capacity_icon = (
                "🟢"
                if workload_analysis["capacity_status"] == "available"
                else "🟡" if workload_analysis["capacity_status"] == "busy" else "🔴"
            )
            response_parts.append(
                f"👤 **Agent Workload:** {capacity_icon} {workload_analysis['capacity_status'].title()}"
            )
            response_parts.append(
                f"   Active Tasks: {workload_analysis['total_active_tasks']} ({workload_analysis['high_priority_tasks']} high priority)"
            )

            if workload_warnings:
                response_parts.extend(workload_warnings)

        # Add RAG validation info
        if validation_performed and validation_message:
            response_parts.append(validation_message)

        # Add coordination info
        if coordination_notes:
            response_parts.append(f"\n📋 **Coordination Notes:** {coordination_notes}")

        # Add smart feature usage tips
        response_parts.append("\n💡 **Smart Features Used:**")
        if auto_suggest_parent:
            response_parts.append(
                "• Smart parent suggestion based on content similarity"
            )
        if validate_agent_workload:
            response_parts.append("• Agent workload analysis and capacity checking")
        if coordination_notes:
            response_parts.append("• Coordination context captured for team awareness")

        return Ok(message="\n".join(response_parts))

    except sqlite3.Error as e_sql:
        if conn:
            conn.rollback()
        logger.error(
            f"Database error assigning task to agent {target_agent_id}: {e_sql}",
            exc_info=True,
        )
        return Failed(message=f"Database error assigning task: {e_sql}")
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(
            f"Unexpected error assigning task to agent {target_agent_id}: {e}",
            exc_info=True,
        )
        return Failed(message=f"Unexpected error assigning task: {e}")
    finally:
        if conn:
            conn.close()


# --- create_self_task tool ---
# Original logic from main.py: lines 1409-1474 (create_self_task_tool function)
# Wave 9 PR 2: @requires("any") → @requires_capability("tasks.create").
# Workers + managers carry ``tasks.create`` via
# :data:`AGENT_ROLE_BUNDLES`; operator-tier callers carry it via
# :data:`PROJECT_ROLE_BUNDLES["operator"]`; sysadmins wildcard-admit.
@requires_capability("tasks.create")
async def create_self_task_tool_impl(
    arguments: Dict[str, Any],
    *,
    principal: Optional[Principal] = None,
) -> ToolResult:
    agent_auth_token = arguments.get("token")
    task_title = arguments.get("task_title")
    task_description = arguments.get("task_description")
    priority = arguments.get("priority", "medium")
    depends_on_tasks_list = arguments.get("depends_on_tasks")
    parent_task_id_arg = arguments.get("parent_task_id")
    # VULN-004: explicit opt-in to apply validator-suggested
    # parent_task / dependencies. See note in assign_task_tool_impl —
    # same prompt-injection vector applies here (in fact more directly,
    # since this is the agent-driven path). Defense-in-depth (audit-A
    # INFO-001): use ``is True`` so non-True values from in-process
    # callers that bypass the registry's jsonschema check are rejected.
    _raw_accept = arguments.get("accept_suggestions", False)
    accept_suggestions = _raw_accept is True

    # @requires_capability("tasks.create") guarantees a valid caller
    # principal at the decorator layer; principal.agent_id is therefore
    # set for agent_bearer callers.
    requesting_agent_id = principal.agent_id

    if not all([task_title, task_description]):
        return Invalid(
            message="task_title and task_description are required.",
        )

    # Determine actual parent task ID (main.py:1419-1423)
    actual_parent_task_id = parent_task_id_arg
    if actual_parent_task_id is None and agent_auth_token in g.active_agents:
        actual_parent_task_id = g.active_agents[agent_auth_token].get("current_task")

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Hierarchy Validation - Agents can NEVER create root tasks
        if requesting_agent_id != "admin" and actual_parent_task_id is None:
            logger.error(
                f"Agent '{requesting_agent_id}' attempted to create a root task"
            )

            # Find a suitable parent task for the agent
            cursor.execute(
                """
                SELECT task_id, title FROM tasks
                WHERE assigned_to = ? OR created_by = ?
                ORDER BY created_at DESC LIMIT 1
            """,
                (requesting_agent_id, requesting_agent_id),
            )

            suggested_parent = cursor.fetchone()
            suggestion_text = ""
            if suggested_parent:
                suggestion_text = f"\nSuggested parent: {suggested_parent['task_id']} ({suggested_parent['title']})"

            return Conflict(
                reason=(
                    f"Agents cannot create root tasks. Every task must have "
                    f"a parent.{suggestion_text}\nPlease specify a parent_task_id."
                )
            )

        # Additional check for single root rule even for admin
        if actual_parent_task_id is None:
            cursor.execute(
                "SELECT COUNT(*) as count, MIN(task_id) as root_id FROM tasks WHERE parent_task IS NULL"
            )
            result = cursor.fetchone()
            root_count = result["count"]
            existing_root_id = result["root_id"]

            if root_count > 0:
                logger.error(
                    f"Attempt to create second root task. Existing root: {existing_root_id}"
                )
                return Conflict(
                    reason=(
                        f"Cannot create root task. A root task already "
                        f"exists ({existing_root_id}). All new tasks must "
                        f"have a parent."
                    )
                )

        # Generate task ID and timestamps first
        new_task_id = _generate_task_id()
        created_at_iso = datetime.datetime.now().isoformat()
        status = "pending"

        # System 8: RAG Pre-Check for Task Placement
        final_parent_task_id = actual_parent_task_id
        final_depends_on_tasks = depends_on_tasks_list
        validation_message = ""

        if ENABLE_TASK_PLACEMENT_RAG:
            validation_result = await validate_task_placement(
                title=task_title,
                description=task_description,
                parent_task_id=actual_parent_task_id,
                depends_on_tasks=depends_on_tasks_list,
                created_by=requesting_agent_id,
                auth_token=agent_auth_token,
            )

            suggestion_message = format_suggestions_for_agent(
                validation_result, actual_parent_task_id, depends_on_tasks_list
            )

            # Check for denial — agent path always blocks; there is no
            # ALLOW_RAG_OVERRIDE escape for self-task creation.
            if validation_result["status"] == "denied":
                return PermissionDenied(
                    reason=(
                        f"Task creation BLOCKED by RAG validation:\n"
                        f"{suggestion_message}"
                    )
                )

            if validation_result["status"] != "approved":
                suggestions = validation_result["suggestions"]
                if not accept_suggestions:
                    # VULN-004: surface suggestions as text instead of
                    # mutating the request. The validator's RAG corpus
                    # can be poisoned via project_context entries, so
                    # any LLM-driven suggestion must be the caller's
                    # explicit decision — not an implicit auto-apply.
                    return Invalid(
                        field=None,
                        message=(
                            "Task placement validator suggests changes. "
                            "Re-submit with accept_suggestions=true to "
                            "apply them, or adjust your request based "
                            f"on these suggestions:\n\n{suggestion_message}"
                        ),
                    )

                # accept_suggestions=True: caller explicitly consented.
                validation_message = (
                    f"\nRAG Validation ({validation_result['status']}):\n"
                    f"{suggestion_message}\n"
                )
                if suggestions.get("parent_task") is not None:
                    final_parent_task_id = suggestions["parent_task"]
                    validation_message += (
                        f"✓ Applied suggested parent: {final_parent_task_id}\n"
                    )
                if suggestions.get("dependencies"):
                    final_depends_on_tasks = suggestions["dependencies"]
                    validation_message += (
                        f"✓ Applied suggested dependencies: {final_depends_on_tasks}\n"
                    )

                logger.info(
                    f"Agent {requesting_agent_id} explicitly accepted "
                    f"RAG suggestions for task {new_task_id}"
                )

                # Check if escalation is needed
                if should_escalate_to_admin(validation_result, requesting_agent_id):
                    logger.warning(
                        f"Task {new_task_id} flagged for admin review: {validation_result.get('message')}"
                    )
                    validation_message += "⚠️ Task flagged for admin review\n"
            else:
                validation_message = "\n✓ RAG validation approved placement\n"

        # TOCTOU recheck (terminate-reconcile). ``validate_task_placement``
        # above yields on a RAG await; a concurrent ``terminate_agent``
        # can commit in that window and revoke this agent before the row
        # is written — leaving a task self-assigned to a terminated
        # identity. Re-run the assignability gate in-transaction,
        # immediately before the INSERT. ``admin`` is exempt: it has no
        # standard assignable agent row and creates coordination tasks.
        if requesting_agent_id != "admin" and not _agent_assignable(
            cursor, requesting_agent_id
        ):
            return Conflict(
                reason=(
                    f"Cannot create task for '{requesting_agent_id}': agent "
                    f"was terminated during task placement."
                )
            )

        # AZ-R19-1 (class-sweep sibling of the Mode-0 fix): a worker may
        # only parent its self-task under a task it OWNS (assigned_to ==
        # itself). An unguarded parent link lets a worker inject an
        # attacker-titled child under ANY foreign parent, mutating that
        # parent's child_tasks JSON mirror (a cross-agent stored-injection
        # primitive — the victim sees an unexpected child appear under
        # their task). A FOREIGN *or* NONEXISTENT parent collapses to the
        # SAME phantom NotFound the Mode-0 / add_task_note /
        # request_assistance gates return (no existence oracle).
        # Supervision-tier callers (``tasks.assign``: operator / manager /
        # sysadmin) are exempt, mirroring the Mode-0 gate's
        # ``is_admin_request`` exemption. Checks ``final_parent_task_id``
        # so an accepted RAG re-parent suggestion is covered too.
        _is_privileged = principal is not None and principal.has_capability(
            "tasks.assign"
        )
        if not _is_privileged and final_parent_task_id is not None:
            cursor.execute(
                "SELECT assigned_to FROM tasks WHERE task_id = ?",
                (final_parent_task_id,),
            )
            _prow = cursor.fetchone()
            if _prow is None or _prow["assigned_to"] != requesting_agent_id:
                return NotFound(
                    resource="task", identifier=final_parent_task_id
                )

        # PR 6: task INSERT via task_repo with the caller's cursor.
        from ..repositories import agent_repo, task_repo
        fresh_task = task_repo.create(
            {
                "task_id": new_task_id,
                "title": task_title,
                "description": task_description,
                "assigned_to": requesting_agent_id,
                "created_by": requesting_agent_id,  # Agent creates for self
                "status": status,
                "priority": priority,
                "parent_task": final_parent_task_id,
                "child_tasks": [],
                "depends_on_tasks": final_depends_on_tasks or [],
                "notes": [],
            },
            connection=cursor,
        )

        # BL-2: maintain the parent's child_tasks mirror.
        parent_mirror_updated = _link_child_to_parent(
            cursor, final_parent_task_id, new_task_id
        )

        # Update agent's current task in DB if they don't have one (main.py:1455-1469)
        should_update_agent_current_task = False
        if agent_auth_token in g.active_agents:  # Check memory first
            if g.active_agents[agent_auth_token].get("current_task") is None:
                should_update_agent_current_task = True
        elif (
            requesting_agent_id != "admin"
        ):  # If not admin and not in active_agents (e.g. loaded from DB only)
            cursor.execute(
                "SELECT current_task FROM agents WHERE agent_id = ?",
                (requesting_agent_id,),
            )
            agent_row = cursor.fetchone()
            if agent_row and agent_row["current_task"] is None:
                should_update_agent_current_task = True
        # Admin agents don't have a persistent 'current_task' in the agents table.

        if should_update_agent_current_task and requesting_agent_id != "admin":
            agent_repo.update_field(
                requesting_agent_id, "current_task", new_task_id,
                connection=cursor,
            )

        log_agent_action_to_db(
            cursor,
            requesting_agent_id,
            "created_self_task",
            task_id=new_task_id,
            details={"title": task_title},
        )
        conn.commit()

        # Post-commit: reconcile caches through repos.
        task_repo.upsert_cache(fresh_task)
        # BL-2: reconcile the parent whose child_tasks mirror changed.
        if parent_mirror_updated:
            _refresh_parent_cache(final_parent_task_id)

        if should_update_agent_current_task and agent_auth_token in g.active_agents:
            g.active_agents[agent_auth_token]["current_task"] = new_task_id

        # Build RAG index payload from the fresh dict.
        index_data = dict(fresh_task)
        # No need to override depends_on_tasks again, it's already the validated value
        # Start indexing asynchronously (fire and forget)
        import asyncio

        asyncio.create_task(index_task_data(new_task_id, index_data))

        log_audit(
            requesting_agent_id,
            "create_self_task",
            {"task_id": new_task_id, "title": task_title},
        )  # main.py:1485
        logger.info(
            f"Agent '{requesting_agent_id}' created self-task '{new_task_id}' ({task_title})."
        )

        response_text = (
            f"Self-assigned task '{new_task_id}' created.\nTitle: {task_title}"
        )
        if validation_message:
            response_text += validation_message

        return Ok(message=response_text)

    except sqlite3.Error as e_sql:
        if conn:
            conn.rollback()
        logger.error(
            f"Database error creating self task for agent {requesting_agent_id}: {e_sql}",
            exc_info=True,
        )
        return Failed(message=f"Database error creating self task: {e_sql}")
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(
            f"Unexpected error creating self task for agent {requesting_agent_id}: {e}",
            exc_info=True,
        )
        return Failed(message=f"Unexpected error creating self task: {e}")
    finally:
        if conn:
            conn.close()


# --- update_task_status tool ---
# Original logic from main.py: lines 1477-1583 (update_task_status_tool function)
@requires_policy("config_allow_worker_update_own_status", default=True)
async def update_task_status_tool_impl(
    arguments: Dict[str, Any],
    *,
    principal: Optional[Principal] = None,
) -> ToolResult:
    agent_auth_token = arguments.get("token")
    task_id_to_update = arguments.get("task_id")
    task_ids_bulk = arguments.get(
        "task_ids"
    )  # NEW: List of task IDs for bulk operations
    new_status = arguments.get("status")
    notes_content = arguments.get("notes")  # Optional string for new note

    # Admin-only fields for full task update
    new_title = arguments.get("title")
    new_description = arguments.get("description")
    new_priority = arguments.get("priority")
    new_assigned_to = arguments.get("assigned_to")
    new_depends_on_tasks = arguments.get("depends_on_tasks")  # List[str] or None

    # Smart features
    auto_update_dependencies = arguments.get(
        "auto_update_dependencies", True
    )  # Auto-update dependent tasks
    cascade_to_children = arguments.get(
        "cascade_to_children", False
    )  # Cascade status to child tasks
    validate_dependencies = arguments.get(
        "validate_dependencies", True
    )  # Validate dependency constraints

    # Wave 9 PR 3: ``is_admin_request`` admits the supervision-tier
    # via ``tasks.assign`` (operator bundle + manager-role bundle +
    # sysadmin wildcard). Worker-role agents (and viewer-tier
    # operators) lack the cap and get the per-row ownership gate
    # applied via ``_update_single_task``.
    is_admin_request = principal.has_capability("tasks.assign")
    requesting_agent_id = (
        principal.agent_id or principal.user_id or "admin"
    )

    # Determine if this is bulk or single operation
    task_ids_to_process = []
    if task_ids_bulk:
        task_ids_to_process = task_ids_bulk
    elif task_id_to_update:
        task_ids_to_process = [task_id_to_update]
    else:
        return Invalid(message="Either task_id or task_ids is required.")

    if not new_status:
        return Invalid(field="status", message="status is required.")

    valid_statuses = ["pending", "in_progress", "completed", "cancelled", "failed"]
    if new_status not in valid_statuses:
        return Invalid(
            field="status",
            message=f"Invalid status: {new_status}. Valid: {', '.join(valid_statuses)}",
        )

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Process tasks (bulk or single)
        results = []
        tasks_to_cascade = []

        # Phase 1: Update primary tasks
        for task_id in task_ids_to_process:
            result = await _update_single_task(
                cursor,
                task_id,
                new_status,
                requesting_agent_id,
                is_admin_request,
                notes_content,
                new_title,
                new_description,
                new_priority,
                new_assigned_to,
                new_depends_on_tasks,
            )
            results.append(result)

            if result["success"] and cascade_to_children:
                tasks_to_cascade.extend(result["child_tasks"])

            # Log individual task action
            if result["success"]:
                log_details = {"status": new_status, "old_status": result["old_status"]}
                if notes_content:
                    log_details["notes_added"] = True
                log_agent_action_to_db(
                    cursor,
                    requesting_agent_id,
                    "update_task_status",
                    task_id=task_id,
                    details=log_details,
                )

        # Phase 2: Smart cascade to children if requested
        cascade_results = []
        if cascade_to_children and tasks_to_cascade:
            for child_task_id in tasks_to_cascade:
                # Only cascade certain status changes to avoid breaking workflows
                if new_status in ["cancelled", "failed"]:  # Cascade blocking states
                    child_result = await _update_single_task(
                        cursor,
                        child_task_id,
                        new_status,
                        requesting_agent_id,
                        is_admin_request,
                        f"Auto-cascaded from parent task status change",
                        None,
                        None,
                        None,
                        None,
                        None,
                    )
                    cascade_results.append(child_result)

        # Phase 3: Smart dependency updates if requested. The advance
        # logic is shared with the bulk path via
        # ``_advance_dependents_after_completion`` (BL-R26-1) so both
        # surfaces unblock dependents identically.
        dependency_updates = []
        if auto_update_dependencies:
            for result in results:
                if result["success"] and new_status == "completed":
                    dependency_updates.extend(
                        await _advance_dependents_after_completion(
                            cursor,
                            result["task_id"],
                            requesting_agent_id,
                            is_admin_request,
                        )
                    )

        # Wave 7 PR 3 (coordinator transition): the "Phase 3.5
        # auto-launch testing agents on task completion" hook is gone.
        # agent-mcp no longer spawns claude processes — if an operator
        # wants a follow-up testing agent, they register it via
        # ``register_agent`` and start their own claude session.

        # Commit all changes
        conn.commit()

        # Phase 2 + 4: wake each touched task's assignee and re-index
        # the mutated tasks. Both run AFTER commit (re-reads observe the
        # new state) and are shared verbatim with the bulk path via
        # ``_wake_task_assignees`` / ``_reindex_tasks`` (BL-R26-1).
        _mutated_ids = [
            r["task_id"]
            for r in results + cascade_results + dependency_updates
            if r.get("success") and r.get("task_id")
        ]
        _wake_task_assignees(_mutated_ids)
        _reindex_tasks(_mutated_ids)

        # Build comprehensive response
        successful_updates = [r for r in results if r.get("success")]
        failed_updates = [r for r in results if not r.get("success")]

        response_parts = []

        if len(task_ids_to_process) == 1:
            # Single task response
            if successful_updates:
                response_parts.append(
                    f"Task {successful_updates[0]['task_id']} status updated to {new_status}."
                )
            else:
                response_parts.append(
                    f"Failed to update task: {failed_updates[0]['error']}"
                )
        else:
            # Bulk operation response
            response_parts.append(
                f"Bulk update completed: {len(successful_updates)}/{len(task_ids_to_process)} tasks updated."
            )

            if failed_updates:
                response_parts.append(f"Failed updates:")
                for fail in failed_updates[:3]:  # Limit to first 3 failures
                    response_parts.append(f"  - {fail['error']}")
                if len(failed_updates) > 3:
                    response_parts.append(
                        f"  ... and {len(failed_updates) - 3} more failures"
                    )

        # Add smart feature results
        if cascade_results:
            successful_cascades = [r for r in cascade_results if r.get("success")]
            response_parts.append(
                f"Cascaded to {len(successful_cascades)} child tasks."
            )

        if dependency_updates:
            successful_deps = [r for r in dependency_updates if r.get("success")]
            response_parts.append(
                f"Auto-advanced {len(successful_deps)} dependent tasks."
            )

        log_audit(
            requesting_agent_id,
            "update_task_status",
            {
                "task_count": len(task_ids_to_process),
                "successful": len(successful_updates),
                "failed": len(failed_updates),
                "status": new_status,
                "cascade_count": len(cascade_results),
                "dependency_updates": len(dependency_updates),
            },
        )

        # Single-task case: if the only failure is an unauthorized
        # one, surface as PermissionDenied so the rendered wire text
        # starts with "Unauthorized:" (consistent with the typed-error
        # vocabulary of Wave 6). Bulk callers keep the aggregated
        # response shape for backward compat.
        if (
            len(task_ids_to_process) == 1
            and failed_updates
            and not successful_updates
        ):
            err_text = (failed_updates[0].get("error") or "").lower()
            if err_text.startswith("unauthorized"):
                return PermissionDenied(
                    reason=failed_updates[0]["error"].removeprefix(
                        "Unauthorized: "
                    )
                )
            if "not found" in err_text:
                return NotFound(
                    resource="task", identifier=task_ids_to_process[0],
                )

        return Ok(message="\n".join(response_parts))

    except sqlite3.Error as e_sql:
        if conn:
            conn.rollback()
        logger.error(f"Database error updating tasks: {e_sql}", exc_info=True)
        return Failed(message=f"Database error updating tasks: {e_sql}")
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Unexpected error updating tasks: {e}", exc_info=True)
        return Failed(message=f"Unexpected error updating tasks: {e}")
    finally:
        if conn:
            conn.close()


# --- view_tasks tool ---
# Original logic from main.py: lines 1586-1655 (view_tasks_tool function)
# Wave 9 PR 2: @requires("any") → @requires_capability("tasks.view").
# Workers + managers carry ``tasks.view`` via
# :data:`AGENT_ROLE_BUNDLES`; viewer + operator project roles carry
# it via :data:`PROJECT_ROLE_BUNDLES`; sysadmins wildcard-admit.
@requires_capability("tasks.view")
async def view_tasks_tool_impl(
    arguments: Dict[str, Any],
    *,
    principal: Optional[Principal] = None,
) -> ToolResult:
    agent_auth_token = arguments.get("token")
    filter_agent_id = arguments.get("agent_id")  # Optional agent_id to filter by
    filter_status = arguments.get("status")  # Optional status to filter by
    max_tokens = arguments.get(
        "max_tokens", 25000
    )  # Maximum response tokens (default: 25k)
    start_after = arguments.get(
        "start_after"
    )  # Task ID to start after (for pagination)
    summary_mode = arguments.get(
        "summary_mode", False
    )  # If True, show only summary info
    # `summary` is the canonical knob (added for callers that want a
    # tiny per-task projection — task_id, title, status, priority,
    # assigned_to). `summary_mode` is the older alias and still works.
    # If either is true, summary projection wins.
    if arguments.get("summary", False):
        summary_mode = True

    # Page-based pagination (added alongside the legacy token-based
    # `start_after` knob). `limit` caps the number of tasks returned;
    # `offset` skips the first N (applied AFTER all filters + sort,
    # BEFORE summary projection / serialization). When `limit` is set,
    # a `Total: <N>` line is added to the response so the caller knows
    # whether there is a next page.
    limit = arguments.get("limit")  # None = unbounded, preserves shape
    offset = arguments.get("offset", 0) or 0
    # OBS-R28-PF: coerce to int BEFORE the [offset:] / [:limit] slices.
    # jsonschema's ``integer`` type admits an integral float (limit=2.0),
    # which then raises ``TypeError: slice indices must be integers`` at
    # the slice. Mirror the sibling numeric fields (admin_tools.list_agents,
    # messages router) which int()-coerce; preserve None (unbounded) for
    # ``limit`` so the response shape + the "Total:" gate are unchanged.
    if limit is not None:
        limit = int(limit)
    offset = int(offset)

    # Smart filtering and analysis options
    show_dependencies = arguments.get(
        "show_dependencies", False
    )  # Show dependency graph info
    show_health_analysis = arguments.get(
        "show_health_analysis", False
    )  # Show task health metrics
    filter_priority = arguments.get("filter_priority")  # Filter by priority
    filter_parent_task = arguments.get("filter_parent_task")  # Filter by parent task
    show_blocked_tasks = arguments.get(
        "show_blocked_tasks", False
    )  # Show only blocked tasks
    sort_by = arguments.get(
        "sort_by", "created_at"
    )  # Sort by: created_at, updated_at, priority, status

    # Wave 9 PR 3: per-row filter sources ``is_admin_request`` from
    # ``tasks.assign`` (supervision-tier marker shared by operator +
    # manager-role bundles + sysadmin wildcard). Workers (and
    # viewer-tier operators) lack the cap and see only the rows the
    # ownership filter below admits.
    is_admin_request = principal.has_capability("tasks.assign")
    requesting_agent_id = (
        principal.agent_id or principal.user_id or "admin"
    )

    # Permission check
    target_agent_id_for_filter = filter_agent_id
    if not is_admin_request:
        if filter_agent_id is None:
            target_agent_id_for_filter = requesting_agent_id
        elif filter_agent_id != requesting_agent_id:
            return PermissionDenied(
                reason=(
                    "Non-admin agents can only view their own tasks or all "
                    "tasks assigned to them if no agent_id filter is specified."
                )
            )

    # Delegate filter/sort/dependency-analysis to the engine — the
    # handler stays an adapter (parse args -> query -> presentation).
    # See agent_mcp/features/task_queries.py for the rules.
    engine = TaskQueryEngine(task_source=lambda: g.tasks)

    query_result = engine.query(
        filters=TaskFilterSpec(
            status=filter_status,
            priority=filter_priority,
            agent_id=target_agent_id_for_filter,
            parent_task_id=filter_parent_task,
            blocked_only=bool(show_blocked_tasks),
        ),
        sort=TaskSortSpec(by=sort_by),
    )
    tasks_to_display: List[Dict[str, Any]] = query_result.tasks
    # Dependency analysis is attached for the formatter as the legacy
    # dict shape (`_dependency_analysis`) — done after the engine query
    # so the snapshot used here is the same snapshot the engine saw.
    if show_dependencies:
        # Need the full snapshot (not just the window) to compute
        # `blocks_tasks` correctly across the whole graph.
        full_snapshot = dict(g.tasks)
        tasks_to_display = [
            {
                **t,
                "_dependency_analysis": engine.health_of(
                    t, full_snapshot
                ).as_dict(),
            }
            for t in tasks_to_display
        ]

    # Legacy token-style pagination: `start_after=<task_id>` skips to
    # the first task AFTER the named one. Kept here (vs. inside the
    # engine) because it's a presentation-window concern — the engine
    # already supports offset/limit which is the structural API.
    if start_after:
        start_index = 0
        for i, task in enumerate(tasks_to_display):
            if task.get("task_id") == start_after:
                start_index = i + 1
                break
        tasks_to_display = tasks_to_display[start_index:]

    # Page-based pagination: capture the total matching count BEFORE
    # offset/limit slicing so we can report "Total: N" to the caller.
    # Only emit the Total line when `limit` is explicitly set — older
    # callers that don't pass `limit` must see the exact same response
    # shape. Mirrors the legacy: total reflects the list AFTER
    # start_after but BEFORE offset+limit.
    total_matching = len(tasks_to_display)
    if offset:
        tasks_to_display = tasks_to_display[offset:]
    if limit is not None:
        tasks_to_display = tasks_to_display[:limit]

    if not tasks_to_display:
        response_text = "No tasks found matching the criteria."
    else:
        # Generate health analysis if requested — engine owns the rule;
        # the handler renders the icon + summary line.
        health_analysis = None
        if show_health_analysis:
            health_analysis = engine.health_metrics(tasks_to_display)

        # Build response with smart headers
        filter_info = []
        if filter_status:
            filter_info.append(f"status={filter_status}")
        if filter_priority:
            filter_info.append(f"priority={filter_priority}")
        if filter_agent_id:
            filter_info.append(f"agent={filter_agent_id}")
        if filter_parent_task:
            filter_info.append(f"parent={filter_parent_task}")
        if show_blocked_tasks:
            filter_info.append("blocked_only=true")

        header = f"Tasks ({len(tasks_to_display)} found"
        if filter_info:
            header += f", filtered by: {', '.join(filter_info)}"
        header += f", sorted by: {sort_by})"

        response_parts = [header + "\n"]

        # Add health analysis at the top if requested
        if health_analysis:
            health_status = health_analysis["health_status"]
            health_score = health_analysis["health_score"]

            health_icon = (
                "🟢"
                if health_status == "excellent"
                else (
                    "🟡"
                    if health_status == "good"
                    else "🟠" if health_status == "needs_attention" else "🔴"
                )
            )

            response_parts.append(
                f"📊 **Health Analysis:** {health_icon} {health_status.title()} ({health_score}/100)"
            )
            response_parts.append(
                f"   Status: {health_analysis['status_distribution']}"
            )
            response_parts.append(
                f"   Issues: {health_analysis['blocked_tasks']} blocked, {health_analysis['stale_tasks']} stale"
            )
            response_parts.append("")

        current_tokens = estimate_tokens("\n".join(response_parts))
        tasks_included = 0
        last_task_id = None
        truncated = False

        for task in tasks_to_display:
            # Format task with dependency info if requested
            if show_dependencies and "_dependency_analysis" in task:
                task_text = _format_task_with_dependencies(task)
            elif summary_mode:
                task_text = _format_task_summary(task)
            else:
                task_text = _format_task_detailed(task)

            task_tokens = estimate_tokens(task_text)

            # Check token limit with safety buffer
            safety_buffer = 1000
            if (
                current_tokens + task_tokens > (max_tokens - safety_buffer)
                and tasks_included > 0
            ):
                truncated = True
                break

            response_parts.append(f"{task_text}\n")
            current_tokens += task_tokens
            tasks_included += 1
            last_task_id = task.get("task_id")

        # Add smart pagination and usage tips
        if truncated:
            remaining_count = len(tasks_to_display) - tasks_included
            response_parts.append(
                f"--- Response truncated to stay under {max_tokens} tokens ---"
            )
            response_parts.append(
                f"Showing {tasks_included} of {len(tasks_to_display)} tasks ({remaining_count} remaining)"
            )
            response_parts.append(
                f"Continue: view_tasks(start_after='{last_task_id}', max_tokens={max_tokens})"
            )
            if not summary_mode:
                response_parts.append(f"Overview: view_tasks(summary_mode=true)")
        else:
            response_parts.append(f"--- All {tasks_included} matching tasks shown ---")

        # When the caller is paginating with `limit`, expose the total
        # matching count + the current window so they know whether to
        # ask for the next page. Only emitted on the new API surface;
        # legacy callers (no `limit`) see the existing shape verbatim.
        if limit is not None:
            response_parts.append(
                f"Total: {total_matching} (showing offset={offset}, limit={limit})"
            )

        # Add smart usage tips
        response_parts.append("\n💡 Smart Tips:")
        if not show_dependencies:
            response_parts.append(
                "• Add show_dependencies=true to see dependency chains"
            )
        if not show_health_analysis:
            response_parts.append("• Add show_health_analysis=true for health metrics")
        if not show_blocked_tasks:
            response_parts.append(
                "• Add show_blocked_tasks=true to see only blocked tasks"
            )
        response_parts.append(
            "• Use sort_by=[priority|status|updated_at] for different sorting"
        )

        response_text = "\n".join(response_parts)

    log_audit(
        requesting_agent_id,
        "view_tasks",
        {"filter_agent_id": filter_agent_id, "filter_status": filter_status},
    )
    return Ok(message=response_text)


def _format_task_summary(task: Dict[str, Any]) -> str:
    """Format task in summary mode (minimal tokens)"""
    task_id = task.get("task_id", "N/A")
    title = task.get("title", "N/A")
    status = task.get("status", "N/A")
    priority = task.get("priority", "medium")
    assigned_to = task.get("assigned_to", "Unassigned")

    # Truncate description
    description = task.get("description", "No description")
    if len(description) > 100:
        description = description[:100] + "..."

    return f"""ID: {task_id}
Title: {title}
Status: {status} | Priority: {priority}
Assigned to: {assigned_to}
Description: {description}"""


def _format_task_detailed(task: Dict[str, Any]) -> str:
    """Format task in detailed mode (includes notes, full description)"""
    parts = []
    parts.append(f"ID: {task.get('task_id', 'N/A')}")
    parts.append(f"Title: {task.get('title', 'N/A')}")
    parts.append(f"Description: {task.get('description', 'No description')}")
    parts.append(f"Status: {task.get('status', 'N/A')}")
    parts.append(f"Priority: {task.get('priority', 'medium')}")
    parts.append(f"Assigned to: {task.get('assigned_to', 'None')}")
    parts.append(f"Created by: {task.get('created_by', 'N/A')}")
    parts.append(f"Created: {task.get('created_at', 'N/A')}")
    parts.append(f"Updated: {task.get('updated_at', 'N/A')}")

    if task.get("parent_task"):
        parts.append(f"Parent task: {task['parent_task']}")

    child_tasks_val = task.get("child_tasks", [])
    if isinstance(child_tasks_val, str):
        try:
            child_tasks_val = json.loads(child_tasks_val or "[]")
        except:
            child_tasks_val = ["Error decoding child_tasks"]
    if child_tasks_val:
        parts.append(f"Child tasks: {', '.join(child_tasks_val)}")

    notes_val = task.get("notes", [])
    if isinstance(notes_val, str):
        try:
            notes_val = json.loads(notes_val or "[]")
        except:
            notes_val = [{"author": "System", "content": "Error decoding notes"}]
    if notes_val:
        parts.append("Notes:")
        # Limit notes to prevent token explosion
        recent_notes = notes_val[-5:] if len(notes_val) > 5 else notes_val
        for note in recent_notes:
            if isinstance(note, dict):
                ts = note.get("timestamp", "Unknown time")
                auth = note.get("author", "Unknown")
                cont = note.get("content", "No content")
                parts.append(f"  - [{ts}] {auth}: {cont}")
            else:
                parts.append(f"  - [Invalid Note Format: {str(note)}]")
        if len(notes_val) > 5:
            parts.append(f"  ... and {len(notes_val) - 5} more notes")

    return "\n".join(parts)


def _format_task_with_dependencies(task: Dict[str, Any]) -> str:
    """Format task with dependency analysis information"""
    # Start with detailed format
    task_text = _format_task_detailed(task)

    # Add dependency analysis
    dep_analysis = task.get("_dependency_analysis", {})
    if dep_analysis:
        dep_parts = ["\n🔗 Dependency Analysis:"]

        # Health status
        health = dep_analysis.get("dependency_health", "unknown")
        health_icon = (
            "🟢"
            if health == "healthy"
            else "🟡" if health == "waiting" else "🟠" if health == "warning" else "🔴"
        )
        dep_parts.append(f"   Status: {health_icon} {health}")

        # Blocking info
        if dep_analysis.get("is_blocked"):
            dep_parts.append("   ⚠️  BLOCKED - Cannot proceed")
        elif not dep_analysis.get("can_start"):
            dep_parts.append("   ⏳ WAITING - Dependencies not ready")
        else:
            dep_parts.append("   ✅ READY - Can proceed")

        # Dependencies details
        completed_deps = dep_analysis.get("completed_dependencies", [])
        blocking_deps = dep_analysis.get("blocking_dependencies", [])
        missing_deps = dep_analysis.get("missing_dependencies", [])

        if completed_deps:
            dep_parts.append(
                f"   ✅ Completed: {', '.join(completed_deps[:3])}"
                + (
                    f" (+{len(completed_deps)-3} more)"
                    if len(completed_deps) > 3
                    else ""
                )
            )

        if blocking_deps:
            dep_parts.append(
                f"   🔴 Blocking: {', '.join(blocking_deps[:3])}"
                + (f" (+{len(blocking_deps)-3} more)" if len(blocking_deps) > 3 else "")
            )

        if missing_deps:
            dep_parts.append(
                f"   ❌ Missing: {', '.join(missing_deps[:3])}"
                + (f" (+{len(missing_deps)-3} more)" if len(missing_deps) > 3 else "")
            )

        # What this task blocks
        blocks_tasks = dep_analysis.get("blocks_tasks", [])
        if blocks_tasks:
            dep_parts.append(
                f"   🔒 Blocks: {', '.join(blocks_tasks[:3])}"
                + (f" (+{len(blocks_tasks)-3} more)" if len(blocks_tasks) > 3 else "")
            )

        task_text += "\n".join(dep_parts)

    return task_text


def _analyze_agent_workload(cursor, agent_id: str) -> Dict[str, Any]:
    """Analyze agent's current workload and capacity"""

    # Get agent's current tasks
    cursor.execute(
        """
        SELECT task_id, title, status, priority, created_at, updated_at 
        FROM tasks 
        WHERE assigned_to = ? AND status IN ('pending', 'in_progress')
        ORDER BY priority DESC, created_at ASC
    """,
        (agent_id,),
    )

    active_tasks = [dict(row) for row in cursor.fetchall()]

    # Calculate workload metrics
    total_tasks = len(active_tasks)
    high_priority_tasks = len([t for t in active_tasks if t.get("priority") == "high"])
    in_progress_tasks = len(
        [t for t in active_tasks if t.get("status") == "in_progress"]
    )
    pending_tasks = len([t for t in active_tasks if t.get("status") == "pending"])

    # Calculate "staleness" - tasks not updated recently
    current_time = datetime.datetime.now()
    stale_tasks = 0
    for task in active_tasks:
        if task.get("updated_at"):
            try:
                updated_time = datetime.datetime.fromisoformat(
                    task["updated_at"].replace("Z", "+00:00").replace("+00:00", "")
                )
                days_stale = (current_time - updated_time).days
                if days_stale > 3:  # No update in 3+ days
                    stale_tasks += 1
            except:
                pass

    # Simple capacity assessment
    capacity_status = "available"
    if total_tasks >= 8:
        capacity_status = "overloaded"
    elif total_tasks >= 5:
        capacity_status = "busy"
    elif high_priority_tasks >= 3:
        capacity_status = "busy"

    return {
        "agent_id": agent_id,
        "total_active_tasks": total_tasks,
        "high_priority_tasks": high_priority_tasks,
        "in_progress_tasks": in_progress_tasks,
        "pending_tasks": pending_tasks,
        "stale_tasks": stale_tasks,
        "capacity_status": capacity_status,
        "workload_score": min(
            100, total_tasks * 10 + high_priority_tasks * 5
        ),  # 0-100+
        "can_take_new_task": capacity_status in ["available", "busy"]
        and high_priority_tasks < 4,
        "recommendations": _generate_workload_recommendations(
            capacity_status, total_tasks, stale_tasks
        ),
    }


def _generate_workload_recommendations(
    capacity_status: str, total_tasks: int, stale_tasks: int
) -> List[str]:
    """Generate workload management recommendations"""
    recommendations = []

    if capacity_status == "overloaded":
        recommendations.append("Consider redistributing some tasks to other agents")
        recommendations.append("Focus on completing high-priority tasks first")

    if stale_tasks > 0:
        recommendations.append(
            f"Review {stale_tasks} stale tasks that haven't been updated recently"
        )

    if total_tasks > 6:
        recommendations.append(
            "Consider breaking down large tasks into smaller subtasks"
        )

    if not recommendations:
        recommendations.append("Workload appears manageable")

    return recommendations


def _suggest_optimal_parent_task(
    cursor, agent_id: str, task_description: str
) -> Dict[str, Any]:
    """Suggest optimal parent task based on context and agent workload"""

    # Get agent's current tasks that could be parents
    cursor.execute(
        """
        SELECT task_id, title, description, status, priority 
        FROM tasks 
        WHERE assigned_to = ? AND status IN ('pending', 'in_progress')
        ORDER BY 
            CASE WHEN status = 'in_progress' THEN 1 ELSE 2 END,
            CASE priority WHEN 'high' THEN 3 WHEN 'medium' THEN 2 ELSE 1 END DESC,
            updated_at DESC
        LIMIT 10
    """,
        (agent_id,),
    )

    agent_tasks = [dict(row) for row in cursor.fetchall()]

    # Simple text similarity scoring (could be enhanced with embeddings)
    def similarity_score(text1: str, text2: str) -> float:
        text1_words = set(text1.lower().split())
        text2_words = set(text2.lower().split())
        if not text1_words or not text2_words:
            return 0.0
        intersection = text1_words.intersection(text2_words)
        union = text1_words.union(text2_words)
        return len(intersection) / len(union) if union else 0.0

    suggestions = []
    for task in agent_tasks:
        # Score based on title and description similarity
        title_sim = similarity_score(task_description, task.get("title", ""))
        desc_sim = similarity_score(task_description, task.get("description", ""))
        combined_score = (title_sim * 0.6) + (desc_sim * 0.4)

        # Boost score for in-progress tasks (more likely to be good parents)
        if task.get("status") == "in_progress":
            combined_score *= 1.2

        if combined_score > 0.1:  # Only suggest if there's some relevance
            suggestions.append(
                {
                    "task_id": task["task_id"],
                    "title": task["title"],
                    "status": task["status"],
                    "priority": task["priority"],
                    "similarity_score": round(combined_score, 3),
                    "reason": f"Similar content ({int(combined_score*100)}% match)",
                }
            )

    # Sort by score and take top 3
    suggestions.sort(key=lambda x: x["similarity_score"], reverse=True)

    return {
        "agent_id": agent_id,
        "suggestions": suggestions[:3],
        "has_suggestions": len(suggestions) > 0,
    }


# --- request_assistance tool ---
# Original logic from main.py: lines 1658-1763 (request_assistance_tool function)
# This tool had file-based notification system. We'll replicate that for 1-to-1.
# Wave 9 PR 2: @requires("any") → @requires_capability("coordination.assist").
# Workers + managers carry ``coordination.assist`` via
# :data:`AGENT_ROLE_BUNDLES`; sysadmins wildcard-admit. The verb is
# coordination-shaped (an agent flagging a task it can't finish for
# another agent / human to pick up), not a task mutation.
@requires_capability("coordination.assist")
async def request_assistance_tool_impl(
    arguments: Dict[str, Any],
    *,
    principal: Optional[Principal] = None,
) -> ToolResult:
    agent_auth_token = arguments.get("token")
    parent_task_id = arguments.get("task_id")  # Task ID needing assistance
    assistance_description = arguments.get("description")

    # Wave 9 PR 3: ``is_admin_request`` admits the supervision-tier
    # via ``tasks.assign`` (operator + manager-role bundles +
    # sysadmin wildcard). The per-task ownership gate below admits
    # the assignee always and admins/managers always; workers can
    # only request assistance on tasks they own.
    is_admin_request = principal.has_capability("tasks.assign")
    requesting_agent_id = (
        principal.agent_id or principal.user_id or "admin"
    )

    if not parent_task_id or not assistance_description:
        return Invalid(
            message="task_id (for parent) and description are required.",
        )

    # Fetch parent task data (original used in-memory g.tasks, main.py:1674)
    # For robustness, let's fetch from DB, then update g.tasks.
    # PR A (round 2): pre-flight validation moves out of the write
    # transaction. The original code did the SELECT on the same cursor
    # the writes used; splitting it out lets the not-found / unauthorized
    # early-returns happen *without* opening a transaction that the
    # `atomic_with_audit` seam would otherwise commit-and-audit.
    from ..db.connection import get_db_connection_read

    _read_conn = get_db_connection_read()
    try:
        _read_cur = _read_conn.cursor()
        _read_cur.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (parent_task_id,)
        )
        parent_task_db_row = _read_cur.fetchone()
    finally:
        _read_conn.close()

    if not parent_task_db_row:
        return NotFound(resource="task", identifier=parent_task_id)

    parent_task_current_data = dict(parent_task_db_row)

    # Verify ownership or admin (main.py:1688-1691) — the per-task
    # ownership gate now sources is_admin_request from the principal
    # block above; assignee can always request, admins/managers always.
    #
    # AZ-R17-1: on the ownership-deny branch return the SAME phantom
    # NotFound a nonexistent task returns (identical variant + text),
    # not a PermissionDenied. A distinct 403-vs-404 split leaked a
    # task-existence oracle — a worker could probe arbitrary task_ids
    # and read "exists but not yours" (403) vs "doesn't exist" (404),
    # enumerating foreign tasks project-wide. This is the last sibling
    # of the uniform-not-found class already closed in
    # _update_single_task / bulk_task_operations / add_task_note.
    if (
        parent_task_current_data.get("assigned_to") != requesting_agent_id
        and not is_admin_request
    ):
        return NotFound(resource="task", identifier=parent_task_id)

    try:
        # Create child assistance task (main.py:1694-1696)
        child_task_id = _generate_task_id()
        child_task_title = f"Assistance for {parent_task_id}: {parent_task_current_data.get('title', 'Untitled Task')}"
        timestamp_iso = datetime.datetime.now().isoformat()

        # Create notification for admin (main.py:1699-1710)
        # This part used file system notifications.
        notification_id = _generate_notification_id()
        notification_data = {
            "id": notification_id,
            "type": "assistance_request",
            "source_agent_id": requesting_agent_id,
            "task_id": parent_task_id,  # Parent task
            "child_task_id": child_task_id,  # The new assistance task
            "timestamp": timestamp_iso,
            "description": assistance_description,
            "status": "pending",  # Notification status
        }

        # Save notification file (main.py:1713-1718)
        project_dir_env = os.environ.get("MCP_PROJECT_DIR")
        if not project_dir_env:
            logger.error(
                "MCP_PROJECT_DIR not set. Cannot save assistance notification file."
            )
            # Decide if this is critical enough to stop. Original didn't explicitly stop.
        else:
            try:
                notifications_pending_dir = (
                    Path(project_dir_env) / ".agent" / "notifications" / "pending"
                )
                notifications_pending_dir.mkdir(parents=True, exist_ok=True)
                notification_file_path = (
                    notifications_pending_dir / f"{notification_id}.json"
                )
                with open(notification_file_path, "w", encoding="utf-8") as f:
                    json.dump(notification_data, f, indent=2)
                logger.info(
                    f"Assistance request notification saved to {notification_file_path}"
                )
            except Exception as e_notify:
                logger.error(
                    f"Failed to save assistance notification file: {e_notify}",
                    exc_info=True,
                )

        # Build the parent's updated child_tasks + notes lists once,
        # before the write transaction, so the atomic block reads only
        # in-memory dicts (no extra SELECTs inside the seam).
        parent_child_tasks_list = json.loads(
            parent_task_current_data.get("child_tasks") or "[]"
        )
        parent_child_tasks_list.append(child_task_id)

        parent_notes_list = json.loads(parent_task_current_data.get("notes") or "[]")
        parent_notes_list.append(
            {
                "timestamp": timestamp_iso,
                "author": requesting_agent_id,
                "content": f"Requested assistance: {assistance_description}. Assistance task created: {child_task_id}",
            }
        )

        # PR A (round 2): the child INSERT + parent UPDATE + audit-log
        # INSERT collapse into a single atomic_with_audit block. The
        # writes were already atomic on the legacy path (one cursor,
        # one commit); the seam now names the audit-row identity at
        # the call site.
        from ..db.atomic import atomic_with_audit
        from ..repositories import task_repo
        with atomic_with_audit(
            operation="request_assistance",
            actor=requesting_agent_id,
            task_id=parent_task_id,
            details={
                "description": assistance_description,
                "child_task_id": child_task_id,
            },
        ) as cursor:
            # Insert the child (assistance) task. PR 7 (Task flip): write
            # flows through task_repo.create with the caller's cursor.
            task_repo.create(
                {
                    "task_id": child_task_id,
                    "title": child_task_title,
                    "description": assistance_description,
                    "status": "pending",
                    "assigned_to": None,
                    "priority": "high",  # Assistance tasks are high priority
                    "parent_task": parent_task_id,
                    "depends_on_tasks": [],
                    "created_by": requesting_agent_id,
                    "child_tasks": [],
                    "notes": [],
                },
                connection=cursor,
            )

            # PR 7 (Task flip): parent-task UPDATE goes through
            # task_repo with the caller's cursor; allowlist + JSON
            # serialisation rule now live in one place.
            task_repo.update_fields(
                parent_task_id,
                {
                    "child_tasks": parent_child_tasks_list,
                    "notes": parent_notes_list,
                },
                connection=cursor,
            )

        # Build the in-memory cache shape (matches the post-commit dict
        # the repo would have produced on the standalone path).
        child_task_db_data = {
            "task_id": child_task_id,
            "title": child_task_title,
            "description": assistance_description,
            "status": "pending",
            "assigned_to": None,
            "priority": "high",
            "created_at": timestamp_iso,
            "updated_at": timestamp_iso,
            "parent_task": parent_task_id,
            "depends_on_tasks": json.dumps([]),
            "created_by": requesting_agent_id,
            "child_tasks": json.dumps([]),
            "notes": json.dumps([]),
        }

        # Update in-memory caches (g.tasks)
        # Parent task
        if parent_task_id in g.tasks:
            g.tasks[parent_task_id]["child_tasks"] = parent_child_tasks_list
            g.tasks[parent_task_id]["notes"] = parent_notes_list
            g.tasks[parent_task_id]["updated_at"] = timestamp_iso
        # New child task
        child_task_mem_data = child_task_db_data.copy()
        child_task_mem_data["depends_on_tasks"] = []  # from json.dumps([])
        child_task_mem_data["child_tasks"] = []
        child_task_mem_data["notes"] = []
        g.tasks[child_task_id] = child_task_mem_data

        # Send direct message to admin via new communication system
        try:
            admin_message = (
                f"🚨 Assistance Request from {requesting_agent_id}\n\n"
                f"Task: {parent_task_id} - {parent_task_current_data.get('title', 'Untitled Task')}\n"
                f"Description: {assistance_description}\n\n"
                f"Child assistance task created: {child_task_id}\n"
                f"Notification ID: {notification_id}"
            )

            # Send message to admin using the new communication system
            message_result = await send_agent_message_tool_impl(
                {
                    "token": agent_auth_token,
                    "recipient_id": "admin",
                    "message": admin_message,
                    "message_type": "assistance_request",
                    "priority": "high",
                    "deliver_method": "both",
                }
            )
            logger.info(f"Assistance request message sent to admin: {message_result}")
        except Exception as e_msg:
            logger.error(
                f"Failed to send assistance request message to admin: {e_msg}",
                exc_info=True,
            )
            # Don't fail the entire operation if messaging fails

        # Original code also wrote parent and child task JSON files (main.py:1766-1771)
        # This was part of an older file-based task system. We are now DB-centric.
        # For 1-to-1, if those files are still used by something, they'd need to be written.
        # However, the primary task store is now the DB.
        # We will skip writing these individual task JSON files as they are redundant with the DB.
        # If get_task_file_path was used elsewhere, that system needs re-evaluation.

        log_audit(
            requesting_agent_id,
            "request_assistance",
            {
                "parent_task_id": parent_task_id,
                "child_task_id": child_task_id,
                "description": assistance_description,
            },
        )
        logger.info(
            f"Agent '{requesting_agent_id}' requested assistance for task '{parent_task_id}'. Child task '{child_task_id}' created."
        )
        return Ok(
            message=(
                f"Assistance requested for task {parent_task_id}. Child "
                f"assistance task {child_task_id} created. Admin notified "
                f"via file notification and direct message."
            ),
            data={
                "parent_task_id": parent_task_id,
                "child_task_id": child_task_id,
                "notification_id": notification_id,
            },
        )

    except sqlite3.Error as e_sql:
        # atomic_with_audit already rolled back + closed before re-raising.
        logger.error(
            f"Database error requesting assistance for task {parent_task_id}: {e_sql}",
            exc_info=True,
        )
        return Failed(message=f"Database error requesting assistance: {e_sql}")
    except Exception as e:
        logger.error(
            f"Unexpected error requesting assistance for task {parent_task_id}: {e}",
            exc_info=True,
        )
        return Failed(message=f"Unexpected error requesting assistance: {e}")


# --- bulk_task_operations tool ---
# Wave 9 PR 2: @requires("any") → @requires_capability("tasks.update").
# The bulk surface fans out to update_status / update_priority /
# add_note (all ``tasks.update`` operations); the ``reassign`` op is
# gated by an in-body ``is_admin_request`` check that PR 3 will
# migrate to ``has_capability("tasks.assign")``. Workers + managers
# carry ``tasks.update`` via :data:`AGENT_ROLE_BUNDLES`; operator-
# tier callers carry it via :data:`PROJECT_ROLE_BUNDLES`; sysadmins
# wildcard-admit.
@requires_capability("tasks.update")
async def bulk_task_operations_tool_impl(
    arguments: Dict[str, Any],
    *,
    principal: Optional[Principal] = None,
) -> ToolResult:
    agent_auth_token = arguments.get("token")
    operations = arguments.get("operations", [])  # List of operation objects

    # Wave 9 PR 3: ``is_admin_request`` admits the supervision-tier
    # via ``tasks.assign`` (operator + manager-role bundles +
    # sysadmin wildcard). Workers (and viewer-tier operators) lack
    # the cap and stay constrained by the per-op ownership check in
    # the loop below.
    is_admin_request = principal.has_capability("tasks.assign")
    requesting_agent_id = (
        principal.agent_id or principal.user_id or "admin"
    )

    if not operations or not isinstance(operations, list):
        return Invalid(
            field="operations",
            message="operations list is required and must be a non-empty array",
        )

    # Process operations in a single transaction. PR A (round 2): the
    # whole "open conn → loop writes → log audit → commit → close"
    # boilerplate collapses into one `atomic_with_audit` block. Per-op
    # validation failures still go into `results` via `continue`; only
    # an actual sqlite/Python exception aborts the transaction.
    from ..db.atomic import atomic_with_audit
    results: List[str] = []
    updated_at_iso = datetime.datetime.now().isoformat()
    # Mutated in-place during the loop; the seam reads it at block
    # exit so the final success_count lands on the audit row.
    audit_details: Dict[str, Any] = {
        "operations_count": len(operations),
        "success_count": 0,
    }
    # BL-R26-1: the reconcile/notify tail the single ``update_task_status``
    # path runs must also run on the bulk path or bulk-mutated tasks stall
    # dependents / never wake their assignee / leave RAG stale. Track the
    # ids to reconcile: ``completed_task_ids`` (status→completed, dependency
    # advance INSIDE the txn) and ``mutated_task_ids`` (status/reassign +
    # advanced dependents, wake + reindex AFTER commit).
    completed_task_ids: List[str] = []
    mutated_task_ids: List[str] = []
    try:
        with atomic_with_audit(
            operation="bulk_task_operations",
            actor=requesting_agent_id,
            details=audit_details,
        ) as cursor:
            for i, op in enumerate(operations):
                if not isinstance(op, dict):
                    results.append(
                        f"Operation {i+1}: Invalid operation format (must be object)"
                    )
                    continue

                operation_type = op.get("type")
                task_id = op.get("task_id")

                if not task_id or not operation_type:
                    results.append(
                        f"Operation {i+1}: Missing required fields 'type' and 'task_id'"
                    )
                    continue

                # Verify task exists and permissions
                cursor.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
                task_row = cursor.fetchone()
                if not task_row:
                    results.append(f"Operation {i+1}: Task '{task_id}' not found")
                    continue

                task_data = dict(task_row)

                # Permission check.
                #
                # SECURITY (PF-1): mirror the not-found branch above so a
                # non-owner without ``tasks.assign`` cannot distinguish a
                # foreign existing task from a nonexistent one (the 403-
                # vs-404 existence oracle). Same wording as the
                # missing-row case; never name the owner.
                if (
                    task_data.get("assigned_to") != requesting_agent_id
                    and not is_admin_request
                ):
                    results.append(f"Operation {i+1}: Task '{task_id}' not found")
                    continue

                try:
                    if operation_type == "update_status":
                        new_status = op.get("status")
                        notes_content = op.get("notes")

                        if not new_status:
                            results.append(
                                f"Operation {i+1}: Missing 'status' for update_status operation"
                            )
                            continue

                        valid_statuses = [
                            "pending",
                            "in_progress",
                            "completed",
                            "cancelled",
                            "failed",
                        ]
                        if new_status not in valid_statuses:
                            results.append(
                                f"Operation {i+1}: Invalid status '{new_status}'"
                            )
                            continue

                        # SECURITY (AZ-R16-1): honor the
                        # config_allow_worker_update_own_status policy the
                        # single path enforces via
                        # @requires_policy("config_allow_worker_update_own_status").
                        # The decorator can't wrap the bulk surface (it
                        # mixes op types in one call), so replicate the
                        # toggle check inline per-op: a non-admin worker
                        # is denied the status transition when the
                        # operator has turned the toggle OFF. Admin/
                        # manager callers (is_admin_request, i.e.
                        # tasks.assign) always pass, mirroring the
                        # decorator's operator-tier bypass.
                        if not is_admin_request and not _access._get_config_bool(
                            "config_allow_worker_update_own_status",
                            default=True,
                        ):
                            results.append(
                                f"Operation {i+1}: worker status updates "
                                f"disabled by project policy "
                                f"(config_allow_worker_update_own_status="
                                f"false)"
                            )
                            continue

                        # Terminal-state / transition guard (mirrors
                        # _update_single_task): terminal states are sinks
                        # so the bulk surface can't double-complete /
                        # resurrect a task.
                        old_status = task_data.get("status")
                        if not _is_status_transition_allowed(
                            old_status, new_status
                        ):
                            results.append(
                                f"Operation {i+1}: Invalid status "
                                f"transition '{old_status}' -> "
                                f"'{new_status}' for task '{task_id}'"
                            )
                            continue

                        # PR 7 (Task flip): bulk status+notes update flows
                        # through task_repo.update_fields with the caller's
                        # cursor. The repo's _MUTABLE_FIELDS allowlist
                        # replaces the inline `allowed_bulk_fields` guard.
                        from ..repositories import task_repo as _task_repo

                        # Handle notes (append-only, like the non-bulk path)
                        current_notes = json.loads(task_data.get("notes") or "[]")
                        if notes_content:
                            current_notes.append(
                                {
                                    "timestamp": updated_at_iso,
                                    "author": requesting_agent_id,
                                    "content": notes_content,
                                }
                            )

                        _task_repo.update_fields(
                            task_id,
                            {"status": new_status, "notes": current_notes},
                            connection=cursor,
                        )

                        # Update in-memory cache
                        if task_id in g.tasks:
                            g.tasks[task_id]["status"] = new_status
                            g.tasks[task_id]["updated_at"] = updated_at_iso
                            g.tasks[task_id]["notes"] = current_notes

                        # Mirror the terminal-status agents.current_task
                        # clear that `_update_single_task` does on the
                        # non-bulk path, so the bulk surface doesn't
                        # leak the same stale-current_task bug.
                        if new_status in ["completed", "cancelled", "failed"]:
                            # PR 8 (Agent flip): filter-based bulk UPDATE
                            # goes through agent_repo.clear_current_task_for
                            # (same as the non-bulk path in
                            # _update_single_task). Cache mirror is owned
                            # by the repo.
                            from ..repositories import agent_repo as _agent_repo
                            _agent_repo.clear_current_task_for(
                                task_id, connection=cursor,
                            )

                        # BL-R26-1: this task's status changed — reconcile
                        # its assignee wake + RAG reindex post-commit, and
                        # (on completion) advance its dependents like the
                        # single path does.
                        mutated_task_ids.append(task_id)
                        if new_status == "completed":
                            completed_task_ids.append(task_id)

                        results.append(
                            f"Operation {i+1}: Task '{task_id}' status updated to '{new_status}'"
                        )

                    elif operation_type == "update_priority":
                        # SECURITY (AZ-R16-1): priority is an admin/
                        # manager-only field on the single path —
                        # _update_single_task only copies new_priority
                        # into fields_to_update when is_admin_request is
                        # True (dropping it for a non-admin). Mirror that
                        # gate here so a worker can't escalate its own
                        # task's priority via the bulk surface. Same
                        # is_admin_request marker (tasks.assign) and same
                        # "requires admin privileges" refusal shape the
                        # sibling reassign op already uses.
                        if not is_admin_request:
                            results.append(
                                f"Operation {i+1}: Update priority "
                                f"operation requires admin privileges"
                            )
                            continue

                        new_priority = op.get("priority")

                        if not new_priority or new_priority not in [
                            "low",
                            "medium",
                            "high",
                        ]:
                            results.append(
                                f"Operation {i+1}: Invalid priority '{new_priority}'"
                            )
                            continue

                        # PR 7 (Task flip): bulk priority update flows
                        # through task_repo.update_fields with the caller's
                        # cursor.
                        from ..repositories import task_repo as _task_repo
                        _task_repo.update_fields(
                            task_id,
                            {"priority": new_priority},
                            connection=cursor,
                        )

                        if task_id in g.tasks:
                            g.tasks[task_id]["priority"] = new_priority
                            g.tasks[task_id]["updated_at"] = updated_at_iso

                        results.append(
                            f"Operation {i+1}: Task '{task_id}' priority updated to '{new_priority}'"
                        )

                    elif operation_type == "add_note":
                        note_content = op.get("content")

                        if not note_content:
                            results.append(
                                f"Operation {i+1}: Missing 'content' for add_note operation"
                            )
                            continue

                        current_notes = json.loads(task_data.get("notes") or "[]")
                        current_notes.append(
                            {
                                "timestamp": updated_at_iso,
                                "author": requesting_agent_id,
                                "content": note_content,
                            }
                        )

                        # PR 7 (Task flip): bulk add_note flows through
                        # task_repo.update_fields with the caller's cursor.
                        from ..repositories import task_repo as _task_repo
                        _task_repo.update_fields(
                            task_id,
                            {"notes": current_notes},
                            connection=cursor,
                        )

                        if task_id in g.tasks:
                            g.tasks[task_id]["notes"] = current_notes
                            g.tasks[task_id]["updated_at"] = updated_at_iso

                        results.append(f"Operation {i+1}: Note added to task '{task_id}'")

                    elif operation_type == "reassign" and is_admin_request:
                        new_assigned_to = op.get("assigned_to")

                        if not new_assigned_to:
                            results.append(
                                f"Operation {i+1}: Missing 'assigned_to' for reassign operation"
                            )
                            continue

                        # SECURITY (BL-R25-1): terminal-sink guard on the
                        # ASSIGN axis. Terminal states are sinks — a
                        # completed/cancelled/failed task must not be
                        # re-pinned onto a live agent, which would silently
                        # resurrect finished work onto an active worker.
                        # The single path (_update_single_task) and the
                        # dashboard composition reassign both refuse a
                        # terminal-task reassignment; mirror that here.
                        # Deny this one op (append error + continue) rather
                        # than aborting the whole bulk transaction, matching
                        # the _agent_assignable failure handling below.
                        current_status = task_data.get("status")
                        if current_status in _TERMINAL_TASK_STATUSES:
                            results.append(
                                f"Operation {i+1}: cannot reassign task "
                                f"'{task_id}' — its status "
                                f"'{current_status}' is terminal "
                                f"(completed/cancelled/failed)"
                            )
                            continue

                        # Reassignment target validation — reject a
                        # free-string ``assigned_to`` that names a
                        # non-existent or terminated agent.
                        if not _agent_assignable(cursor, new_assigned_to):
                            results.append(
                                f"Operation {i+1}: Cannot reassign task "
                                f"'{task_id}' to '{new_assigned_to}': "
                                f"agent does not exist or is terminated"
                            )
                            continue

                        # SECURITY (AZ-R26-1): capability-routing parity.
                        # The canonical assign path
                        # (``_assign_to_existing_tasks``) refuses to pin a
                        # capability-tagged task onto an under-capable
                        # agent; the bulk reassign op must enforce the SAME
                        # control or it is a bypass. Deny this one op
                        # (per-op error + continue), matching the
                        # terminal-sink / assignability handling above.
                        missing_caps = _missing_capabilities(
                            cursor,
                            task_data.get("required_capabilities"),
                            new_assigned_to,
                        )
                        if missing_caps:
                            results.append(
                                f"Operation {i+1}: Cannot reassign task "
                                f"'{task_id}' to '{new_assigned_to}': "
                                f"agent lacks required capabilities "
                                f"{missing_caps}"
                            )
                            continue

                        # PR 7 (Task flip): bulk reassign flows through
                        # task_repo.update_fields with the caller's cursor.
                        from ..repositories import task_repo as _task_repo
                        _task_repo.update_fields(
                            task_id,
                            {"assigned_to": new_assigned_to},
                            connection=cursor,
                        )

                        if task_id in g.tasks:
                            g.tasks[task_id]["assigned_to"] = new_assigned_to
                            g.tasks[task_id]["updated_at"] = updated_at_iso

                        # BL-R26-1: wake the new assignee post-commit like
                        # the single reassign path does.
                        mutated_task_ids.append(task_id)

                        results.append(
                            f"Operation {i+1}: Task '{task_id}' reassigned to '{new_assigned_to}'"
                        )

                    else:
                        if operation_type == "reassign" and not is_admin_request:
                            results.append(
                                f"Operation {i+1}: Reassign operation requires admin privileges"
                            )
                        else:
                            results.append(
                                f"Operation {i+1}: Unknown operation type '{operation_type}'"
                            )

                except Exception as e:
                    # SECURITY (round 12, SD-R12-1): this per-op line is
                    # appended to ``results`` and returned as
                    # ``Ok(message=...)``, which the renderer emits
                    # VERBATIM — only the ``Failed`` variant is
                    # genericised at the choke-point (SD-R8-1). So
                    # interpolating ``str(e)`` here leaked raw SQLite
                    # table/column names + OSError paths to any worker
                    # holding ``tasks.update``. Keep the detail
                    # server-side (logger, exc_info); the client-facing
                    # line is STATIC (op index + caller-supplied op type
                    # only — no ``str(e)``). Retains the "Error" token so
                    # the ``success_count`` filter below still excludes
                    # it. Same class as SD-R9-1 (RAG ``Ok``-body bypass).
                    results.append(
                        f"Operation {i+1}: Error processing "
                        f"{operation_type}: internal error"
                    )
                    logger.error(
                        f"Error in bulk operation {i+1} "
                        f"({operation_type}): {e}",
                        exc_info=True,
                    )

            # BL-R26-1 Phase-3: advance dependents of every task this
            # batch drove to ``completed`` — inside the SAME transaction,
            # via the helper shared with the single path, so a bulk
            # completion unblocks its dependents identically. Runs after
            # the op loop so all completions in the batch are visible
            # before the advance (a dependent gated on two batch-completed
            # tasks advances correctly).
            for _done_id in completed_task_ids:
                _advanced = await _advance_dependents_after_completion(
                    cursor,
                    _done_id,
                    requesting_agent_id,
                    is_admin_request,
                )
                mutated_task_ids.extend(
                    r["task_id"] for r in _advanced if r.get("success")
                )

            # Final success_count is patched onto the audit-row details
            # dict; `atomic_with_audit` reads the same dict at block
            # exit and emits one agent_actions row before committing.
            audit_details["success_count"] = len(
                [r for r in results if "Error" not in r]
            )

        # BL-R26-1 Phase-2 + 4: POST-commit (the ``with`` block committed
        # and closed its connection) wake each mutated task's assignee and
        # re-index the mutated tasks, via the helpers shared with the
        # single path. Reached only on the success path — an aborted
        # transaction raises out of the ``with`` and skips this.
        _wake_task_assignees(mutated_task_ids)
        _reindex_tasks(mutated_task_ids)

        response_text = (
            f"Bulk Task Operations Results ({len(operations)} operations):\n\n"
            + "\n".join(results)
        )

        log_audit(
            requesting_agent_id,
            "bulk_task_operations",
            {"operations_count": len(operations)},
        )
        return Ok(message=response_text)

    except sqlite3.Error as e_sql:
        # `atomic_with_audit` already rolled back + closed before
        # re-raising; we just translate to a user-facing error.
        logger.error(f"Database error in bulk task operations: {e_sql}", exc_info=True)
        return Failed(message=f"Database error in bulk operations: {e_sql}")
    except Exception as e:
        logger.error(f"Unexpected error in bulk task operations: {e}", exc_info=True)
        return Failed(message=f"Unexpected error in bulk operations: {e}")


# --- search_tasks tool ---
# Wave 9 PR 2: @requires("any") → @requires_capability("tasks.view").
# Same shape as ``view_tasks`` — the verb is read-shaped (full-text
# search across the task corpus). Workers + managers carry
# ``tasks.view``; viewer + operator project roles carry it; sysadmins
# wildcard-admit.
@requires_capability("tasks.view")
async def search_tasks_tool_impl(
    arguments: Dict[str, Any],
    *,
    principal: Optional[Principal] = None,
) -> ToolResult:
    agent_auth_token = arguments.get("token")
    search_query = arguments.get("search_query")
    status_filter = arguments.get("status_filter")
    max_results = arguments.get("max_results", 20)
    # OBS-R28-PF: coerce to int BEFORE the [:max_results] slices below.
    # Same numeric-coercion sibling as view_tasks — an integral float
    # (max_results=2.0) validates against the ``integer`` schema then
    # raises ``TypeError: slice indices must be integers`` at the slice.
    max_results = int(max_results)
    include_notes = arguments.get("include_notes", True)

    # Wave 9 PR 3: per-row filter sources ``is_admin_request`` from
    # ``tasks.assign`` (supervision-tier marker shared by operator +
    # manager-role bundles + sysadmin wildcard). Workers (and
    # viewer-tier operators) lack the cap and only see their own
    # rows.
    is_admin_request = principal.has_capability("tasks.assign")
    requesting_agent_id = (
        principal.agent_id or principal.user_id or "admin"
    )

    # `search_query` is optional as of v5.0.22 — callers may supply
    # only `status_filter` to list tasks by status without text
    # scoring (filter-only mode). When both are absent, the response
    # surfaces a guidance error rather than crashing.
    has_query = bool(search_query and search_query.strip())

    # Prepare search terms (only when a query is present)
    search_terms: List[str] = []
    if has_query:
        search_terms = [
            term.strip().lower()
            for term in search_query.split()
            if len(term.strip()) > 2
        ]
        if not search_terms:
            return Invalid(
                field="search_query",
                message="Search query must contain terms longer than 2 characters.",
            )

    # Guidance error: with neither a usable query nor a filter, there
    # is no implicit "everything" semantic — ask the caller to narrow.
    # `view_tasks` is the right tool for unbounded listing.
    if not has_query and not status_filter:
        return Invalid(
            message=(
                "search_tasks requires either a search_query or a "
                "status_filter. For an unfiltered listing of tasks, "
                "use view_tasks instead."
            )
        )

    # Get tasks user can see
    candidate_tasks = []
    for task_id, task_data in g.tasks.items():
        # Permission check
        if not is_admin_request and task_data.get("assigned_to") != requesting_agent_id:
            continue

        # Status filter
        if status_filter and task_data.get("status") != status_filter:
            continue

        candidate_tasks.append(task_data)

    if not candidate_tasks:
        return Ok(message="No tasks found matching the criteria.")

    # Filter-only path: no query, only filters. Skip the scoring loop
    # and return the candidate tasks ordered by (updated_at DESC).
    if not has_query:
        candidate_tasks.sort(
            key=lambda t: t.get("updated_at", ""), reverse=True
        )
        truncated = candidate_tasks[:max_results]
        filter_descr_parts: list[str] = []
        if status_filter:
            filter_descr_parts.append(f"status={status_filter}")
        filter_descr = ", ".join(filter_descr_parts) or "no filters"

        response_parts = [
            f"Tasks matching filters ({filter_descr}) — "
            f"{len(truncated)} of {len(candidate_tasks)} shown:\n"
        ]
        current_tokens = len("\n".join(response_parts)) // 4
        for i, task in enumerate(truncated):
            task_text = (
                f"\n{i+1}. **{task.get('title', 'Untitled')}** "
                f"(ID: {task.get('task_id', 'N/A')})"
            )
            task_text += (
                f"\n   Status: {task.get('status', 'N/A')} | "
                f"Priority: {task.get('priority', 'medium')} | "
                f"Assigned: {task.get('assigned_to', 'None')}"
            )
            desc = task.get("description", "No description")
            if len(desc) > 200:
                desc = desc[:200] + "..."
            task_text += f"\n   Description: {desc}"

            task_tokens = estimate_tokens(task_text)
            safety_buffer = 1000
            if current_tokens + task_tokens <= (20000 - safety_buffer):
                response_parts.append(task_text)
                current_tokens += task_tokens
            else:
                remaining = len(truncated) - i
                response_parts.append(
                    f"\n⚠️  Response truncated - {remaining} more "
                    f"results available"
                )
                break

        response_parts.append("\n\n💡 Tips:")
        response_parts.append(
            "• Use view_tasks(task_id='ID') for full task details"
        )
        response_parts.append(
            "• Add search_query to score results by text relevance"
        )
        response_parts.append(
            "• Use max_results to control response size"
        )

        log_audit(
            requesting_agent_id,
            "search_tasks",
            {
                "query": None,
                "status_filter": status_filter,
                "results": len(truncated),
            },
        )
        return Ok(message="\n".join(response_parts))

    # Score tasks by relevance
    scored_results = []
    for task in candidate_tasks:
        score = 0.0
        matched_fields = []

        # Search in title (highest weight)
        title = (task.get("title") or "").lower()
        title_matches = sum(1 for term in search_terms if term in title)
        if title_matches > 0:
            score += title_matches * 3.0
            matched_fields.append(f"title ({title_matches} terms)")

        # Search in description (medium weight)
        description = (task.get("description") or "").lower()
        desc_matches = sum(1 for term in search_terms if term in description)
        if desc_matches > 0:
            score += desc_matches * 2.0
            matched_fields.append(f"description ({desc_matches} terms)")

        # Search in notes (lower weight)
        if include_notes:
            notes = task.get("notes", [])
            if isinstance(notes, str):
                try:
                    notes = json.loads(notes)
                except:
                    notes = []

            notes_content = " ".join(
                [note.get("content", "") for note in notes if isinstance(note, dict)]
            ).lower()
            notes_matches = sum(1 for term in search_terms if term in notes_content)
            if notes_matches > 0:
                score += notes_matches * 1.0
                matched_fields.append(f"notes ({notes_matches} terms)")

        # Exact phrase bonus
        full_text = f"{title} {description}".lower()
        if search_query.lower() in full_text:
            score += 2.0
            matched_fields.append("exact phrase")

        if score > 0:
            scored_results.append((task, score, matched_fields))

    if not scored_results:
        return Ok(message=f"No tasks found containing '{search_query}'.")

    # Sort by relevance (score descending, then by updated_at descending)
    scored_results.sort(key=lambda x: (x[1], x[0].get("updated_at", "")), reverse=True)

    # Limit results
    scored_results = scored_results[:max_results]

    # Format response with token awareness
    response_parts = [
        f"Search Results for '{search_query}' ({len(scored_results)} found):\n"
    ]
    current_tokens = len("\n".join(response_parts)) // 4  # Simple token estimation

    for i, (task, score, matched_fields) in enumerate(scored_results):
        if current_tokens >= 20000:  # Leave room for truncation message
            remaining = len(scored_results) - i
            response_parts.append(
                f"\n⚠️  Response truncated - {remaining} more results available"
            )
            response_parts.append(
                "Use max_results parameter or refine search to see more"
            )
            break

        # Format task result
        task_text = f"\n{i+1}. **{task.get('title', 'Untitled')}** (ID: {task.get('task_id', 'N/A')})"
        task_text += f"\n   Status: {task.get('status', 'N/A')} | Priority: {task.get('priority', 'medium')} | Assigned: {task.get('assigned_to', 'None')}"
        task_text += (
            f"\n   Relevance Score: {score:.1f} | Matched: {', '.join(matched_fields)}"
        )

        # Add truncated description
        desc = task.get("description", "No description")
        if len(desc) > 200:
            desc = desc[:200] + "..."
        task_text += f"\n   Description: {desc}"

        # Check token limit with safety buffer
        task_tokens = estimate_tokens(task_text)
        safety_buffer = 1000
        if current_tokens + task_tokens <= (20000 - safety_buffer):
            response_parts.append(task_text)
            current_tokens += task_tokens
        else:
            remaining = len(scored_results) - i
            response_parts.append(
                f"\n⚠️  Response truncated - {remaining} more results available"
            )
            break

    # Add usage tips
    response_parts.append(f"\n\n💡 Tips:")
    response_parts.append("• Use view_tasks(task_id='ID') for full task details")
    response_parts.append("• Add status_filter to narrow results")
    response_parts.append("• Use max_results to control response size")

    log_audit(
        requesting_agent_id,
        "search_tasks",
        {"query": search_query, "results": len(scored_results)},
    )
    return Ok(message="\n".join(response_parts))


# --- Register all task tools ---
def register_task_tools():
    register_tool(
        name="assign_task",
        description="Multi-mode task assignment tool. Mode 1: Create single task + assign agent. Mode 2: Create multiple tasks + assign agent. Mode 3: Assign agent to existing unassigned tasks. Includes workload analysis, intelligent parent suggestions, and coordination features.",
        input_schema={
            "type": "object",
            "properties": {
                "token": {
                    "type": "string",
                    "description": "Admin authentication token. Optional if Authorization: Bearer header is supplied (recommended).",
                },
                "agent_token": {
                    "type": "string",
                    "description": "Agent token to assign the task(s) to (optional - if not provided, creates unassigned tasks)",
                },
                "agent_id": {
                    "type": "string",
                    "description": (
                        "Admin-only alternative to `agent_token` — resolves "
                        "to the agent's token server-side by name. Workers "
                        "must use `agent_token`. Ignored (no error) when "
                        "`agent_token` is also provided."
                    ),
                },
                # Mode 1: Single task creation (existing behavior)
                "task_title": {
                    "type": "string",
                    "description": "Title of the task (Mode 1: single task creation)",
                },
                "task_description": {
                    "type": "string",
                    "description": "Detailed description of the task (Mode 1: single task creation)",
                },
                "priority": {
                    "type": "string",
                    "description": "Task priority (low, medium, high) - for single task mode",
                    "enum": ["low", "medium", "high"],
                    "default": "medium",
                },
                "depends_on_tasks": {
                    "type": "array",
                    "description": "List of task IDs this task depends on (Mode 1 only)",
                    "items": {"type": "string"},
                },
                "parent_task_id": {
                    "type": "string",
                    "description": "ID of the parent task (Mode 1 only)",
                },
                # Event-coord PR-1: capability routing for unassigned tasks.
                # Subset match — an agent receives the
                # `unassigned_task_appeared` event (PR-2) iff
                # agent.capabilities ⊇ task.required_capabilities. Empty
                # list / null ⇒ broadcast (everyone wakes). Server
                # normalizes (strip + lowercase + dedupe) at write time.
                "required_capabilities": {
                    "type": "array",
                    "description": (
                        "Capability labels a worker must satisfy to be "
                        "considered for this task (PR-1 event-coord). "
                        "Subset match against agent.capabilities. Empty/"
                        "missing = no capability gate. Free-text strings; "
                        "lowercase-normalized at write time."
                    ),
                    "items": {"type": "string"},
                },
                # Mode 2: Multiple task creation
                "tasks": {
                    "type": "array",
                    "description": "Array of tasks to create and assign (Mode 2: multiple task creation)",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string", "description": "Task title"},
                            "description": {
                                "type": "string",
                                "description": "Task description",
                            },
                            "priority": {
                                "type": "string",
                                "description": "Task priority",
                                "enum": ["low", "medium", "high"],
                                "default": "medium",
                            },
                            "parent_task_id": {
                                "type": "string",
                                "description": "Parent task ID for this task",
                            },
                            "required_capabilities": {
                                "type": "array",
                                "description": (
                                    "Per-task capability gate; overrides the "
                                    "top-level required_capabilities for this "
                                    "entry only (PR-1 event-coord)."
                                ),
                                "items": {"type": "string"},
                            },
                        },
                        "required": ["title", "description"],
                        "additionalProperties": False,
                    },
                },
                # Mode 3: Existing task assignment
                "task_ids": {
                    "type": "array",
                    "description": "Array of existing task IDs to assign to agent (Mode 3: existing task assignment)",
                    "items": {"type": "string"},
                },
                # Smart coordination features (apply to all modes)
                "auto_suggest_parent": {
                    "type": "boolean",
                    "description": "Use AI to suggest optimal parent task based on content similarity (default: true)",
                    "default": True,
                },
                "validate_agent_workload": {
                    "type": "boolean",
                    "description": "Analyze agent capacity and provide workload warnings (default: true)",
                    "default": True,
                },
                "auto_schedule": {
                    "type": "boolean",
                    "description": "Auto-schedule task based on dependencies and agent availability (default: false)",
                    "default": False,
                },
                "coordination_notes": {
                    "type": "string",
                    "description": "Optional coordination context for team awareness and handoffs",
                },
                "estimated_hours": {
                    "type": "number",
                    "description": "Optional workload estimation in hours for capacity planning",
                },
                # RAG validation options
                "accept_suggestions": {
                    "type": "boolean",
                    "description": (
                        "When validator returns suggestions, auto-apply "
                        "them. Default false: suggestions surface as text "
                        "for caller to evaluate before re-submitting. "
                        "VULN-004: this defaults to false because the "
                        "validator's RAG corpus is writeable by any "
                        "agent (via project_context), so silent "
                        "auto-application is a prompt-injection vector."
                    ),
                    "default": False,
                },
                "override_rag": {
                    "type": "boolean",
                    "description": "Override RAG validation suggestions (optional, defaults to false - accepts suggestions)",
                    "default": False,
                },
                "override_reason": {
                    "type": "string",
                    "description": "Reason for overriding RAG validation (required if override_rag is true)",
                },
            },
            "required": [],
            "additionalProperties": False,
        },
        implementation=assign_task_tool_impl,
        visibility=(
            "worker-if-toggled:config_allow_worker_self_assign,"
            "config_allow_worker_create_unassigned"
        ),
    )

    register_tool(
        name="create_self_task",  # main.py:1726
        description="Agent tool to create a task for themselves. IMPORTANT: parent_task_id is REQUIRED - agents cannot create root tasks.",
        input_schema={  # From main.py:1727-1750
            "type": "object",
            "properties": {
                "token": {
                    "type": "string",
                    "description": "Agent authentication token. Optional if Authorization: Bearer header is supplied (recommended).",
                },
                "task_title": {"type": "string", "description": "Title of the task"},
                "task_description": {
                    "type": "string",
                    "description": "Detailed description of the task",
                },
                "priority": {
                    "type": "string",
                    "description": "Task priority (low, medium, high)",
                    "enum": ["low", "medium", "high"],
                    "default": "medium",
                },
                "depends_on_tasks": {
                    "type": "array",
                    "description": "List of task IDs this task depends on (optional)",
                    "items": {"type": "string"},
                },
                "parent_task_id": {
                    "type": "string",
                    "description": "ID of the parent task (defaults to agent's current task if not specified, but MUST have a parent)",
                },
                "accept_suggestions": {
                    "type": "boolean",
                    "description": (
                        "When validator returns suggestions, auto-apply "
                        "them. Default false: suggestions surface as text "
                        "for caller to evaluate before re-submitting. "
                        "VULN-004: this defaults to false because the "
                        "validator's RAG corpus is writeable by any "
                        "agent (via project_context), so silent "
                        "auto-application is a prompt-injection vector."
                    ),
                    "default": False,
                },
            },
            "required": ["task_title", "task_description"],
            "additionalProperties": False,
        },
        implementation=create_self_task_tool_impl,
    )

    register_tool(
        name="update_task_status",
        description="Smart task status update tool with bulk operations, dependency management, and cascade features. Supports single task or bulk updates with intelligent automation.",
        input_schema={
            "type": "object",
            "properties": {
                "token": {
                    "type": "string",
                    "description": "Authentication token (agent or admin). Optional if Authorization: Bearer header is supplied (recommended).",
                },
                "task_id": {
                    "type": "string",
                    "description": "ID of the task to update (for single task operations)",
                },
                "task_ids": {
                    "type": "array",
                    "description": "List of task IDs for bulk operations (alternative to task_id)",
                    "items": {"type": "string"},
                },
                "status": {
                    "type": "string",
                    "description": "New status for the task(s)",
                    "enum": [
                        "pending",
                        "in_progress",
                        "completed",
                        "cancelled",
                        "failed",
                    ],
                },
                "notes": {
                    "type": "string",
                    "description": "Optional notes about the status update to be appended.",
                },
                # Admin Only Optional Fields
                "title": {
                    "type": "string",
                    "description": "(Admin Only) New title for the task",
                },
                "description": {
                    "type": "string",
                    "description": "(Admin Only) New description for the task",
                },
                "priority": {
                    "type": "string",
                    "description": "(Admin Only) New priority",
                    "enum": ["low", "medium", "high"],
                },
                "assigned_to": {
                    "type": "string",
                    "description": "(Admin Only) New agent ID to assign the task to",
                },
                "depends_on_tasks": {
                    "type": "array",
                    "description": "(Admin Only) New list of task IDs this task depends on",
                    "items": {"type": "string"},
                },
                # Smart Features
                "auto_update_dependencies": {
                    "type": "boolean",
                    "description": "Automatically advance dependent tasks when their dependencies are completed (default: true)",
                    "default": True,
                },
                "cascade_to_children": {
                    "type": "boolean",
                    "description": "Cascade status changes to child tasks (only for failed/cancelled states, default: false)",
                    "default": False,
                },
                "validate_dependencies": {
                    "type": "boolean",
                    "description": "Validate dependency constraints before updating (default: true)",
                    "default": True,
                },
            },
            "required": ["status"],
            "additionalProperties": False,
        },
        implementation=update_task_status_tool_impl,
        visibility=(
            "worker-if-toggled:config_allow_worker_update_own_status"
        ),
    )

    register_tool(
        name="view_tasks",
        description=(
            "Smart task viewer with dependency analysis, health metrics, "
            "and advanced filtering. For an overview against a project "
            "with many tasks, prefer summary=true (and limit=50) to keep "
            "the response well under the per-call token cap."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "token": {"type": "string", "description": "Authentication token. Optional if Authorization: Bearer header is supplied (recommended)."},
                "agent_id": {
                    "type": "string",
                    "description": "Filter tasks by agent ID (optional). If non-admin, can only be self.",
                },
                "status": {
                    "type": "string",
                    "description": "Filter tasks by status (optional)",
                    "enum": [
                        "pending",
                        "in_progress",
                        "completed",
                        "cancelled",
                        "failed",
                    ],
                },
                "max_tokens": {
                    "type": "integer",
                    "description": "Maximum response tokens (default: 25000)",
                    "minimum": 1000,
                    "maximum": 25000,
                },
                "start_after": {
                    "type": "string",
                    "description": "Task ID to start after (for pagination)",
                },
                "summary_mode": {
                    "type": "boolean",
                    "description": "If true, show only summary info to fit more tasks (default: false)",
                },
                "summary": {
                    "type": "boolean",
                    "description": (
                        "If true, return only task_id, title, status, "
                        "priority, and assigned_to per task (omits "
                        "description, notes, child_tasks, depends_on_tasks, "
                        "and other large fields). Recommended default "
                        "for any 'give me an overview' call — a project "
                        "with 40 tasks fits comfortably under the "
                        "per-call token cap. Alias of summary_mode."
                    ),
                    "default": False,
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        "Max number of tasks to return after filters + "
                        "sort. When set, response includes a Total: "
                        "<N> line so the caller knows if more pages "
                        "exist. Suggested: 50 for overview calls."
                    ),
                    "minimum": 1,
                },
                "offset": {
                    "type": "integer",
                    "description": (
                        "Pagination offset — skip the first N tasks "
                        "(applied after filters + sort). Pairs with "
                        "limit. Default: 0."
                    ),
                    "minimum": 0,
                    "default": 0,
                },
                # Smart filtering options
                "filter_priority": {
                    "type": "string",
                    "description": "Filter by priority level",
                    "enum": ["low", "medium", "high"],
                },
                "filter_parent_task": {
                    "type": "string",
                    "description": "Filter by parent task ID",
                },
                "show_blocked_tasks": {
                    "type": "boolean",
                    "description": "Show only blocked/waiting tasks (default: false)",
                },
                # Analysis and insights
                "show_dependencies": {
                    "type": "boolean",
                    "description": "Include dependency chain analysis for each task (default: false)",
                },
                "show_health_analysis": {
                    "type": "boolean",
                    "description": "Include overall task health metrics and analysis (default: false)",
                },
                # Sorting options
                "sort_by": {
                    "type": "string",
                    "description": "Sort tasks by specified field (default: created_at)",
                    "enum": ["created_at", "updated_at", "priority", "status"],
                    "default": "created_at",
                },
            },
            "required": [],
            "additionalProperties": False,
        },
        implementation=view_tasks_tool_impl,
    )

    register_tool(
        name="search_tasks",
        description="Full-text search across task titles, descriptions, and notes — or filter-only listing when no query is supplied. Critical for finding related work, avoiding duplication, and quickly listing tasks by status.",
        input_schema={
            "type": "object",
            "properties": {
                "token": {"type": "string", "description": "Authentication token. Optional if Authorization: Bearer header is supplied (recommended)."},
                "search_query": {
                    "type": "string",
                    "description": "Search terms to find in tasks. Optional — when omitted, the tool returns tasks matching the other filter arguments (e.g. status_filter) without text scoring.",
                },
                "status_filter": {
                    "type": "string",
                    "description": "Optional status filter",
                    "enum": [
                        "pending",
                        "in_progress",
                        "completed",
                        "cancelled",
                        "failed",
                    ],
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum results to return (default: 20)",
                    "minimum": 1,
                    "maximum": 100,
                },
                "include_notes": {
                    "type": "boolean",
                    "description": "Include notes content in search (default: true)",
                },
            },
            "required": [],
            "additionalProperties": False,
        },
        implementation=search_tasks_tool_impl,
    )

    register_tool(
        name="request_assistance",  # main.py:1808
        description="Request assistance with a task. This creates a child task assigned to 'None' and notifies admin.",
        input_schema={  # From main.py:1809-1823
            "type": "object",
            "properties": {
                "token": {
                    "type": "string",
                    "description": "Agent authentication token. Optional if Authorization: Bearer header is supplied (recommended).",
                },
                "task_id": {
                    "type": "string",
                    "description": "ID of the task for which assistance is needed (parent task).",
                },
                "description": {
                    "type": "string",
                    "description": "Description of the assistance required.",
                },
            },
            "required": ["task_id", "description"],
            "additionalProperties": False,
        },
        implementation=request_assistance_tool_impl,
    )

    register_tool(
        name="bulk_task_operations",
        description="Perform multiple task operations in a single atomic transaction. Supports update_status, update_priority, add_note, and reassign (admin only) operations. Critical for efficient batch task management.",
        input_schema={
            "type": "object",
            "properties": {
                "token": {
                    "type": "string",
                    "description": "Authentication token (agent or admin). Optional if Authorization: Bearer header is supplied (recommended).",
                },
                "operations": {
                    "type": "array",
                    "description": "List of operations to perform",
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {
                                "type": "string",
                                "description": "Operation type",
                                "enum": [
                                    "update_status",
                                    "update_priority",
                                    "add_note",
                                    "reassign",
                                ],
                            },
                            "task_id": {
                                "type": "string",
                                "description": "Task ID to operate on",
                            },
                            "status": {
                                "type": "string",
                                "description": "New status for update_status operation",
                                "enum": [
                                    "pending",
                                    "in_progress",
                                    "completed",
                                    "cancelled",
                                    "failed",
                                ],
                            },
                            "priority": {
                                "type": "string",
                                "description": "New priority for update_priority operation",
                                "enum": ["low", "medium", "high"],
                            },
                            "content": {
                                "type": "string",
                                "description": "Note content for add_note operation",
                            },
                            "notes": {
                                "type": "string",
                                "description": "Notes for update_status operation",
                            },
                            "assigned_to": {
                                "type": "string",
                                "description": "New assignee for reassign operation (admin only)",
                            },
                        },
                        "required": ["type", "task_id"],
                        "additionalProperties": False,
                    },
                    "minItems": 1,
                },
            },
            "required": ["operations"],
            "additionalProperties": False,
        },
        implementation=bulk_task_operations_tool_impl,
        # @requires_capability("tasks.update") on the impl (admin gets
        # full control; per-op ownership check rejects worker writes on
        # non-owned tasks), but the tool's purpose is admin-orchestrated
        # batch — workers have no use case that justifies tools/list
        # advertisement.
        visibility="operator",
    )

    register_tool(
        name="delete_task",
        description="Delete a task permanently with cascade handling for related tasks. Admin-only operation with comprehensive safety checks.",
        input_schema={
            "type": "object",
            "properties": {
                "token": {
                    "type": "string",
                    "description": "Admin authentication token. Optional if Authorization: Bearer header is supplied (recommended).",
                },
                "task_id": {
                    "type": "string",
                    "description": "ID of the task to delete",
                },
                "force_delete": {
                    "type": "boolean",
                    "description": "Force deletion even if task has children or dependencies (default: false)",
                    "default": False,
                },
            },
            "required": ["task_id"],
            "additionalProperties": False,
        },
        implementation=delete_task_tool_impl,
        visibility="operator",
    )


# Wave 9 PR 2: @requires_role("operator") → @requires_capability("tasks.delete").
# Operator-tier callers carry ``tasks.delete`` via
# :data:`PROJECT_ROLE_BUNDLES["operator"]`; sysadmins wildcard-admit.
# Workers / viewers / agent-role bearers do NOT carry it and are
# rejected at the decorator layer — same admit/deny matrix the
# legacy operator gate enforced.
@requires_capability("tasks.delete")
async def delete_task_tool_impl(
    arguments: Dict[str, Any],
    *,
    principal: Optional[Principal] = None,
) -> ToolResult:
    """
    Delete a task permanently with cascade handling for related tasks.
    Operator-only operation with comprehensive safety checks.
    """
    task_id = arguments.get("task_id")
    force_delete = arguments.get("force_delete", False)

    if not task_id:
        return Invalid(field="task_id", message="task_id is required")

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Check if task exists
        cursor.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
        task_row = cursor.fetchone()

        if not task_row:
            return NotFound(resource="task", identifier=task_id)

        task_data = dict(task_row)

        from ..repositories import task_repo as _task_repo

        # BL-2: enumerate children authoritatively from the
        # ``parent_task`` FK column — the source of truth — NOT the
        # ``child_tasks`` JSON mirror (which historically drifted when a
        # creation path failed to append the back-reference). A cascade
        # driven by the stale mirror missed real children, so the main
        # DELETE tripped the ``tasks.parent_task`` self-FK and
        # ``force_delete`` did not actually force.
        cursor.execute(
            "SELECT task_id FROM tasks WHERE parent_task = ?", (task_id,)
        )
        direct_child_ids = [r["task_id"] for r in cursor.fetchall()]

        # Check for child tasks
        if direct_child_ids and not force_delete:
            return Conflict(
                reason=(
                    f"Task '{task_id}' has {len(direct_child_ids)} child tasks: "
                    f"{direct_child_ids}. Use force_delete=true to cascade delete."
                )
            )

        # Check for tasks that depend on this one
        cursor.execute(
            "SELECT task_id, title FROM tasks WHERE json_extract(depends_on_tasks, '$') LIKE ?",
            (f'%"{task_id}"%',),
        )
        dependent_tasks = cursor.fetchall()

        if dependent_tasks and not force_delete:
            dependent_list = [
                f"{row['task_id']} ({row['title']})" for row in dependent_tasks
            ]
            return Conflict(
                reason=(
                    f"{len(dependent_tasks)} tasks depend on '{task_id}': "
                    f"{dependent_list}. Use force_delete=true to cascade delete."
                )
            )

        # BL-3: the ``agents.current_task → tasks.task_id`` FK. A task
        # referenced by some agent's ``current_task`` cannot be DELETEd
        # while the pointer stands. Non-force: refuse with a clear message
        # (mirrors the child / dependent refusals above) instead of letting
        # the DELETE trip a raw ``FOREIGN KEY constraint failed``. Force:
        # the pointers across the whole delete set are NULLed below, in the
        # same transaction, before any DELETE.
        cursor.execute(
            "SELECT agent_id FROM agents WHERE current_task = ?", (task_id,)
        )
        agents_on_task = [r["agent_id"] for r in cursor.fetchall()]
        if agents_on_task and not force_delete:
            return Conflict(
                reason=(
                    f"Task '{task_id}' is the current task of "
                    f"{len(agents_on_task)} agent(s): {agents_on_task}. "
                    f"Use force_delete=true to clear it and cascade delete."
                )
            )

        # Begin cascade deletion operations
        cascade_operations = []
        # BL-1: (task_id, assigned_to) rows to evict from g.tasks +
        # publish ``task.deleted`` for, AFTER commit. ``task_repo.delete``
        # on the connection= path defers cache + publish to the caller so
        # a subscriber never observes an uncommitted / rolled-back delete.
        deleted_events: List[tuple] = [
            (task_id, task_data.get("assigned_to"))
        ]
        parent_to_refresh: Optional[str] = None
        deps_to_refresh: set = set()

        # Update parent task to remove this child (JSON mirror upkeep).
        if task_data.get("parent_task"):
            parent_id = task_data["parent_task"]
            cursor.execute(
                "SELECT child_tasks FROM tasks WHERE task_id = ?", (parent_id,)
            )
            parent_row = cursor.fetchone()

            if parent_row:
                parent_children = json.loads(parent_row["child_tasks"] or "[]")
                if task_id in parent_children:
                    parent_children.remove(task_id)
                    _task_repo.update_fields(
                        parent_id,
                        {"child_tasks": parent_children},
                        connection=cursor,
                    )
                    parent_to_refresh = parent_id
                    cascade_operations.append(
                        f"Updated parent task '{parent_id}' to remove child reference"
                    )

        # Handle child tasks — force-cascade the whole subtree, deepest
        # descendant first (authoritative FK order) so no DELETE trips the
        # self-FK. Route each through task_repo.delete so the cache +
        # publish contract is honoured post-commit.
        if force_delete:
            descendants = _collect_task_descendants(cursor, task_id)

            # BL-3: NULL ``agents.current_task`` for every agent whose
            # pointer is anywhere in the delete set (target + descendants)
            # BEFORE the DELETEs. Otherwise the
            # ``agents.current_task → tasks.task_id`` FK aborts the
            # ``DELETE FROM tasks`` and ``force_delete`` fails to force.
            # Routed through the repo (one UPDATE ... IN (...)) so the
            # in-memory agent cache mirror stays consistent with the DB.
            from ..repositories import agent_repo as _agent_repo
            delete_set_ids = [task_id] + [d_id for d_id, _ in descendants]
            _agent_repo.clear_current_task_for_many(
                delete_set_ids, connection=cursor
            )

            for descendant_id, descendant_assignee in descendants:
                if _task_repo.delete(descendant_id, connection=cursor):
                    deleted_events.append(
                        (descendant_id, descendant_assignee)
                    )
                    cascade_operations.append(
                        f"Deleted child task '{descendant_id}'"
                    )

        # Handle dependent tasks.
        #
        # BL-R19-1: reconcile dangling ``depends_on_tasks`` references
        # across the WHOLE deleted set (root + every cascade-deleted
        # descendant), not just the root. Previously only tasks depending
        # on the ROOT were cleaned; an OUTSIDE task depending on a
        # cascade-deleted DESCENDANT kept a reference to a now-absent id.
        # ``auto_update_dependencies`` only advances a dependent when a
        # dependency *completes* — it never fires for a *deleted*
        # dependency — so that outside task stalled at ``pending`` forever
        # (silent workflow stall). Same "delete must reconcile references"
        # class as BL-2 / BL-R4-1, extended from the root to the subtree.
        reeval_candidates: set = set()
        if force_delete:
            deleted_id_set = set(delete_set_ids)  # root + all descendants

            # Gather every OUTSIDE task referencing any deleted id in its
            # deps. Read all rows first (before any write) so a task that
            # references two deleted ids is captured with its ORIGINAL
            # dep list exactly once.
            affected_deps: Dict[str, List[str]] = {}
            for deleted_id in delete_set_ids:
                cursor.execute(
                    "SELECT task_id, depends_on_tasks FROM tasks "
                    "WHERE json_extract(depends_on_tasks, '$') LIKE ?",
                    (f'%"{deleted_id}"%',),
                )
                for dep_row in cursor.fetchall():
                    dep_id = dep_row["task_id"]
                    if dep_id in deleted_id_set:
                        continue  # itself being deleted — no reconcile
                    affected_deps.setdefault(
                        dep_id,
                        json.loads(dep_row["depends_on_tasks"] or "[]"),
                    )

            for dep_id, dep_dependencies in affected_deps.items():
                pruned = [
                    d for d in dep_dependencies if d not in deleted_id_set
                ]
                if pruned != dep_dependencies:
                    _task_repo.update_fields(
                        dep_id,
                        {"depends_on_tasks": pruned},
                        connection=cursor,
                    )
                    deps_to_refresh.add(dep_id)
                    reeval_candidates.add(dep_id)
                    cascade_operations.append(
                        f"Updated task '{dep_id}' to remove dependency on "
                        f"deleted task(s) in the '{task_id}' cascade"
                    )

            # BL-R19-1: re-evaluate each unblocked task. A ``pending`` task
            # whose remaining deps are all completed advances to
            # ``in_progress`` — mirrors the auto-advance in the
            # update_task_status path (auto_update_dependencies). Without
            # this a task whose last blocking dependency was deleted would
            # never progress on its own (deletion, unlike completion, never
            # triggers the advance).
            for dep_id in reeval_candidates:
                cursor.execute(
                    "SELECT status, depends_on_tasks FROM tasks "
                    "WHERE task_id = ?",
                    (dep_id,),
                )
                row = cursor.fetchone()
                if row is None or row["status"] != "pending":
                    continue
                remaining = json.loads(row["depends_on_tasks"] or "[]")
                all_completed = True
                for rid in remaining:
                    cursor.execute(
                        "SELECT status FROM tasks WHERE task_id = ?", (rid,)
                    )
                    r2 = cursor.fetchone()
                    if r2 is None or r2["status"] != "completed":
                        all_completed = False
                        break
                if all_completed:
                    advance = await _update_single_task(
                        cursor,
                        dep_id,
                        "in_progress",
                        "admin",
                        True,
                        "Auto-advanced: blocking dependency deleted",
                    )
                    if advance.get("success"):
                        cascade_operations.append(
                            f"Auto-advanced task '{dep_id}' to in_progress "
                            f"(blocking dependency deleted)"
                        )

        # Delete the main task through the repo (cache + publish deferred
        # to post-commit per the connection= contract).
        if not _task_repo.delete(task_id, connection=cursor):
            return Failed(message=f"Failed to delete task '{task_id}'")

        # BL-R4-1: prune each deleted task's RAG chunk + hash watermark
        # in the SAME transaction as the row delete. The incremental
        # indexer keys on ``updated_at`` and never sweeps orphans, so a
        # deleted task's ``source_type='task'`` chunk would otherwise
        # stay queryable via ``ask_project_rag`` forever. ``deleted_events``
        # holds the whole delete set (target + every force-cascaded
        # descendant), so this covers the cascade too. Clearing the hash
        # lets a future task with the same id re-index cleanly.
        from ..repositories import rag_repo as _rag_repo
        for deleted_id, _ in deleted_events:
            _rag_repo.purge_source("task", deleted_id, connection=cursor)

        # Log the deletion action
        log_agent_action_to_db(
            cursor=cursor,
            agent_id="admin",
            action_type="deleted_task",
            task_id=task_id,
            details={
                "task_title": task_data.get("title"),
                "force_delete": force_delete,
                "cascade_operations": cascade_operations,
            },
        )

        conn.commit()

        # BL-1: post-commit cache reconciliation + EventBus publish. Evict
        # every deleted row from g.tasks and publish ``task.deleted``;
        # refresh the parent + dependents whose JSON mirrors we mutated
        # (update_fields(connection=) also deferred their cache write).
        for deleted_id, deleted_assignee in deleted_events:
            _task_repo.evict_from_cache(deleted_id)
            _publish_task_event(
                deleted_assignee,
                "task.deleted",
                {"task_id": deleted_id, "assigned_to": deleted_assignee},
            )
        _refresh_parent_cache(parent_to_refresh)
        for dep_id in deps_to_refresh:
            fresh_dep = _task_repo.get_by_id(dep_id)
            if fresh_dep is not None:
                _task_repo.upsert_cache(fresh_dep)

        # Prepare response
        response_parts = [
            f"Task '{task_id}' ({task_data.get('title', 'Untitled')}) deleted successfully."
        ]

        if cascade_operations:
            response_parts.append("\nCascade Operations:")
            for op in cascade_operations:
                response_parts.append(f"  • {op}")

        response_parts.append(
            f"\nDeletion completed at: {datetime.datetime.now().isoformat()}"
        )

        return Ok(message="\n".join(response_parts))

    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Error in delete_task_tool_impl: {e}", exc_info=True)
        return Failed(message=f"Error deleting task: {str(e)}")
    finally:
        if conn:
            conn.close()


# Call registration when this module is imported
register_task_tools()
