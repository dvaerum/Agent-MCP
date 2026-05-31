"""Per-tool access classification used by `tools/list` filtering.

The MCP framework's `tools/list` returns every registered tool by
default. That worked while a router-side rewrite hid admin-only tools
from worker bearers; Phase 7f deleted that rewrite (per Q7.1: routers
do not manipulate the MCP protocol), so workers started seeing the
full upstream catalogue. They then attempt to call admin-only tools,
the tool's own `verify_token(.., "admin")` short-circuit returns
isError=true (PR #15) — but the worker still wasted a tool call and,
worse, the model often loops trying alternatives based on what it can
see in tools/list.

The fix is to filter `tools/list` at the backend itself, based on the
calling bearer's role:

* "admin"  — admin sees, worker does not, unauthenticated does not.
* "any"    — everyone sees (admin + worker + unauthenticated alike).
* "worker-if-toggled:<config_key>[,<config_key>...]" — admin always
  sees; worker sees iff at least one listed key resolves truthy in
  `project_context` (using the helper's own default, which is set per
  key in `_TOGGLE_DEFAULTS` below).

The classification is intentionally a hand-maintained mapping rather
than a `register_tool` parameter: it (a) keeps the diff small, (b)
co-locates the *whole* policy in one auditable file, and (c) lets
`test_tools_list_filter::test_every_registered_tool_has_access_
classification` catch newly-registered tools that haven't been
classified.

If you add a new tool, you MUST add a classification here.

Source-of-truth for the classifications is each tool's own auth gate
in `agent_mcp/tools/*.py`:

* admin-only tools call `verify_token(token, "admin")` and reject
  unconditionally on failure.
* "any" tools call `get_agent_id(token)` (rejects no-token but
  accepts any active agent's token).
* toggle-gated tools branch on `verify_token(..., "admin")` and then
  read `_get_config_bool("config_allow_worker_*", default=...)` to
  decide whether the worker path is permitted.
"""
from __future__ import annotations

from typing import Dict

from ..core.config import logger

# --- The classification ---

#: Maps tool name → access level string. See module docstring for the
#: grammar of access levels.
TOOL_ACCESS: Dict[str, str] = {
    # --- admin_tools.py — all gate on verify_token(token, "admin") ---
    "create_agent": "admin",
    "view_status": "admin",
    "terminate_agent": "admin",
    "view_audit_log": "admin",
    "get_agent_tokens": "admin",
    "relaunch_agent": "admin",
    # --- agent_communication_tools.py ---
    # Admin-only broadcast.
    "broadcast_admin_message": "admin",
    # Workers can read their own inbox unconditionally.
    "get_agent_messages": "any",
    # Worker→worker delivery gated on config_allow_worker_to_worker
    # (default deny per PR #16 / Q6b.1). Admin always permitted.
    "send_agent_message": "worker-if-toggled:config_allow_worker_to_worker",
    # --- task_tools.py ---
    # Admin-only batch ops + destructive delete.
    "bulk_task_operations": "admin",
    "delete_task": "admin",
    # Workers may always read tasks + file self-tasks + request help.
    "view_tasks": "any",
    "search_tasks": "any",
    "create_self_task": "any",
    "request_assistance": "any",
    # Workers can update task status when the toggle is on (default
    # true per PR #18). Wired here for tools/list visibility — the
    # call-time enforcement lives in update_task_status_tool_impl /
    # _update_single_task ownership check.
    "update_task_status": (
        "worker-if-toggled:config_allow_worker_update_own_status"
    ),
    # assign_task workers paths: Mode 0 (file unassigned) gated on
    # config_allow_worker_create_unassigned (default true); Mode 3
    # (self-claim) gated on config_allow_worker_self_assign (default
    # true). With either toggle truthy the worker can do something
    # useful with the tool, so it stays visible.
    "assign_task": (
        "worker-if-toggled:config_allow_worker_self_assign,"
        "config_allow_worker_create_unassigned"
    ),
    # --- project_context_tools.py ---
    # Admin-only backup tool (write to backup_name).
    "backup_project_context": "admin",
    # Read + analysis + per-key creator-ownership writes (PR #52) —
    # workers always *see* these; the impl rejects ownership misuse
    # at call time.
    "view_project_context": "any",
    "update_project_context": "any",
    "bulk_update_project_context": "any",
    "delete_project_context": "any",
    "validate_context_consistency": "any",
    # --- rag_tools.py ---
    "ask_project_rag": "any",
    # --- agent_tools.py ---
    "get_system_prompt": "any",
    # --- file_management_tools.py ---
    "check_file_status": "any",
    "update_file_status": "any",
    # --- file_metadata_tools.py ---
    "view_file_metadata": "any",
    "update_file_metadata": "any",
    # --- utility_tools.py ---
    "test": "any",
}

# Default truthiness for each toggle when the project_context row is
# absent. Mirrors what each tool's own impl passes to
# `_get_config_bool(..., default=...)`; keeping the defaults here means
# the tools/list filter and the call-time gate agree on "is this on?"
# without crossing module boundaries.
_TOGGLE_DEFAULTS: Dict[str, bool] = {
    # Default-deny worker→worker (PR #16).
    "config_allow_worker_to_worker": False,
    # Default-allow worker self-assign / self-file / own-status updates.
    "config_allow_worker_self_assign": True,
    "config_allow_worker_create_unassigned": True,
    "config_allow_worker_update_own_status": True,
}


def _get_config_bool(key: str, default: bool) -> bool:
    """Read a boolean toggle from project_context.

    Identical semantics to the per-module helpers in
    `agent_communication_tools.py` / `task_tools.py`; kept here so the
    filter doesn't reach across modules into private helpers. If the
    project_context store is unreachable (e.g. during very early
    bootstrap before the DB exists), defaults are returned.
    """
    try:
        from ..db.connection import get_db_connection

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT value FROM project_context WHERE context_key = ?",
            (key,),
        )
        row = cursor.fetchone()
        conn.close()
    except Exception:
        return default
    if not row:
        return default
    raw = row["value"]
    if isinstance(raw, str):
        s = raw.strip().strip('"').lower()
        if s in ("true", "1", "yes", "on"):
            return True
        if s in ("false", "0", "no", "off"):
            return False
    return default


def is_visible_to_role(tool_name: str, role: str) -> bool:
    """Return True if `tool_name` should appear in `tools/list` for the
    given `role` ("admin" | "worker" | "anonymous").

    Unknown tool names default to visible — registry callers should
    not silently hide tools the policy file forgot to classify; the
    invariant test catches the omission instead.
    """
    level = TOOL_ACCESS.get(tool_name)
    if level is None:
        # Defensive: see test_every_registered_tool_has_access_classification.
        # We log so the gap is loud in dev/CI but stay permissive at
        # runtime so a forgotten classification doesn't break a worker.
        logger.warning(
            "tools/list filter: tool %r has no access classification; "
            "defaulting to visible. Add an entry to "
            "agent_mcp/tools/access.py::TOOL_ACCESS.",
            tool_name,
        )
        return True

    if role == "admin":
        return True

    if level == "admin":
        return False
    if level == "any":
        return True
    if level.startswith("worker-if-toggled:"):
        if role != "worker":
            # Anonymous: only "any" tools.
            return False
        keys = [
            k.strip()
            for k in level[len("worker-if-toggled:"):].split(",")
            if k.strip()
        ]
        # Any-truthy semantics: if the worker can do *anything* with
        # the tool under the current toggles, surface it.
        return any(
            _get_config_bool(k, _TOGGLE_DEFAULTS.get(k, False)) for k in keys
        )

    # Unrecognised level string — log and stay visible (same rationale
    # as the unknown-tool default).
    logger.warning(
        "tools/list filter: tool %r has unrecognised access level %r; "
        "defaulting to visible.",
        tool_name,
        level,
    )
    return True
