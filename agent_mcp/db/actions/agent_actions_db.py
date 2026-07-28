# Agent-MCP/mcp_template/mcp_server_src/db/actions/agent_actions_db.py
import sqlite3
import json
import datetime
from typing import Any, Optional

# Import the central logger and database connection function
from ...core.config import logger
# get_db_connection is not directly used here, as _log_agent_action expects a cursor.
# However, the calling code (e.g., tool functions) will use get_db_connection.


# --- Live dashboard updates -------------------------------------------------
# Every mutating tool logs an action here (this is the Recent-activity
# source), which makes this the single choke point for "project data
# changed". Pushing a `notifications/resources/updated` from here means
# every current AND future mutation drives live dashboard refetches with
# no per-site sprinkling. The fan-out reaches runtime-queue subscribers
# — the dashboard's SSE stream — while agents parked in wait_for_events
# POSTs have no runtime queue, so they aren't spammed. It is best-effort
# telemetry: it must NEVER disrupt the mutation that logged the action.

# (substring, dashboard-scope) — first match wins. Lets a future
# fine-grained dashboard invalidate just the touched slice; today the
# dashboard refetches the whole all-data envelope regardless.
_ACTION_SCOPE_HINTS = (
    ("task", "tasks"),
    ("directive", "tasks"),
    ("message", "messages"),
    ("broadcast", "messages"),
    ("agent", "agents"),
    ("context", "memories"),
    ("memory", "memories"),
    ("config", "settings"),
    ("setting", "settings"),
    ("file", "agents"),
)


def _dashboard_scope_for_action(action_type: Optional[str]) -> str:
    a = (action_type or "").lower()
    for needle, scope in _ACTION_SCOPE_HINTS:
        if needle in a:
            return scope
    return "activity"


def _push_dashboard_data_changed(action_type: Optional[str]) -> None:
    """Best-effort: ping dashboard SSE subscribers that project data
    changed so open pages refetch live. Never raises."""
    try:
        # Late import: keep the db-layer import graph shallow and avoid
        # any import-time coupling to the transport layer.
        from ...core import session_registry

        scope = _dashboard_scope_for_action(action_type)
        session_registry.fanout_to_all(
            {
                "jsonrpc": "2.0",
                "method": "notifications/resources/updated",
                "params": {
                    "uri": f"agent-mcp://{scope}",
                    "action_type": action_type,
                },
            }
        )
    except Exception:  # noqa: BLE001 — telemetry-grade, never disrupt the write
        pass


# Original location: main.py lines 256-263 (_log_agent_action function)
def log_agent_action_to_db(
    cursor: sqlite3.Cursor,
    agent_id: Optional[str] = None,
    action_type: Optional[str] = None,
    task_id: str = None,
    details: dict = None,
    *,
    principal: Any = None,
) -> None:
    """
    Internal helper to insert an entry into the agent_actions table.
    This function expects an active database cursor. The caller is responsible
    for connection management (commit/rollback, close).

    Args:
        cursor: An active sqlite3.Cursor object.
        agent_id: (DEPRECATED — Wave 6 PR 0 — pass ``principal=`` instead)
            The ID of the agent performing the action (or 'admin'). Kept
            accepting during the bridge window so unmigrated callers keep
            working; PR 6 removes the kwarg.
        action_type: A string describing the type of action.
        task_id: Optional ID of the task related to this action.
        details: Optional dictionary containing additional details about
            the action (will be JSON serialized). When ``principal=`` is
            supplied, identity fields derived from it
            (``source_principal_kind``, ``source_user_id``,
            ``source_token_suffix``, ``project_name``) are merged in for
            audit-log attribution.
        principal: (Wave 6 PR 0) The
            :class:`agent_mcp.core.principal.Principal` for the calling
            request. When passed, ``agent_id`` defaults to
            ``principal.actor_label()`` and ``details`` gains a
            ``"principal"`` envelope describing the auth surface that
            admitted the call. Old ``agent_id=`` kwarg still wins when
            both are supplied — gives migrating call sites room to
            transition without flipping every audit assertion at once.
    """
    if action_type is None:
        # Defensive — the historical signature made action_type
        # positional + required; with the keyword refactor a stray
        # ``log_agent_action_to_db(cursor)`` call would silently insert a
        # row with NULL action_type. Reject loudly instead.
        raise TypeError("log_agent_action_to_db: action_type is required")

    # Derive identity from principal when one is supplied AND the
    # legacy ``agent_id`` kwarg wasn't (legacy always wins to keep
    # back-compat with callers mid-migration).
    if agent_id is None and principal is not None:
        try:
            agent_id = principal.actor_label()
        except Exception:  # pragma: no cover - defensive
            agent_id = None

    # Merge principal attribution into the details envelope when a
    # principal was supplied. We don't overwrite caller-supplied
    # ``principal`` keys in ``details`` (defensive — keeps the explicit
    # caller intent if anyone passes their own envelope).
    if principal is not None:
        attribution = {
            "kind": getattr(principal, "kind", None),
            "user_id": getattr(principal, "user_id", None),
            "agent_id": getattr(principal, "agent_id", None),
            "project_name": getattr(principal, "project_name", None),
        }
        source_token = getattr(principal, "source_token", None)
        if isinstance(source_token, str) and source_token:
            # Store only the last 4 chars (sha256 of the bearer is
            # what session_registry stores; the suffix is enough for
            # operator-facing audit-log filters without re-leaking
            # the full bearer into the log).
            attribution["source_token_suffix"] = source_token[-4:]
        if details is None:
            details = {"principal": attribution}
        elif isinstance(details, dict) and "principal" not in details:
            details = {**details, "principal": attribution}

    timestamp = datetime.datetime.now().isoformat() # main.py:258
    details_json = None
    if details is not None:
        try:
            details_json = json.dumps(details) # main.py:259
        except TypeError as e:
            logger.error(f"Failed to serialize 'details' for agent action logging (agent: {agent_id}, action: {action_type}): {e}. Storing as string.")
            details_json = str(details) # Fallback to string representation

    try:
        # Original main.py lines 260-262
        cursor.execute("""
            INSERT INTO agent_actions (agent_id, action_type, task_id, timestamp, details)
            VALUES (?, ?, ?, ?, ?)
        """, (agent_id, action_type, task_id, timestamp, details_json))
        # logger.debug(f"Logged action: {agent_id} - {action_type}") # Original main.py:263 (optional debug log)
        # Live dashboard update (best-effort; see _push_dashboard_data_changed).
        # The wire push is a "something changed, refetch soon" hint; the
        # dashboard debounces it so the refetch lands after the caller's
        # commit even though this fires pre-commit.
        _push_dashboard_data_changed(action_type)
    except sqlite3.Error as e:
        # Log error but don't crash the primary operation that called this.
        # Original main.py line 266
        logger.error(f"Failed to log agent action '{action_type}' for agent '{agent_id}' (task_id: {task_id}) to DB: {e}")
    except Exception as e: # Original main.py line 268
        logger.error(f"Unexpected error logging agent action '{action_type}' for agent '{agent_id}': {e}", exc_info=True)

# No other functions were solely dedicated to agent_actions table in the original main.py.
# If other specific queries/updates for agent_actions arise, they can be added here.