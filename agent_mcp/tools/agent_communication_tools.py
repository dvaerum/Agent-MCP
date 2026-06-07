# Agent-MCP/agent_mcp/tools/agent_communication_tools.py
import asyncio
import json
import datetime
import secrets
import sqlite3
from typing import List, Dict, Any, Optional
from pathlib import Path
import os

import mcp.types as mcp_types

from .registry import register_tool
from . import access as _access  # Canonical home for _get_config_bool
from ..core.config import logger
from ..core import globals as g
from ..core.auth import verify_token, get_agent_id
from ..core.authorize import requires, requires_policy
from ..features.aoe_notify import notify_aoe as _aoe_notify
from ..utils.audit_utils import log_audit
from ..db.connection import get_db_connection
from ..db.actions.agent_actions_db import log_agent_action_to_db
from ..utils.tmux_utils import send_prompt_async, session_exists, sanitize_session_name, send_command_to_session


def _generate_message_id() -> str:
    """Generate a unique message ID."""
    return f"msg_{secrets.token_hex(8)}"


def _agents_active_by_id() -> set[str]:
    """Set of agent_ids currently registered as active.

    PR-W2c: routed through AgentRepository so terminated agents are
    excluded by DB-level filter (status != 'terminated') and tokens
    aren't part of the projection. The cache shape (token-keyed) is
    irrelevant to callers — they want the set of *agent_ids* for
    membership tests.
    """
    from ..core.repositories import agent_repo

    return {row.get("agent_id") for row in agent_repo.list_active_agents()}


def _can_agents_communicate(sender_id: str, recipient_id: str, is_admin: bool) -> tuple[bool, str]:
    """
    Check if two agents are allowed to communicate.

    Args:
        sender_id: ID of the sending agent
        recipient_id: ID of the receiving agent
        is_admin: Whether the sender has admin privileges

    Returns:
        Tuple of (allowed: bool, reason: str)
    """
    # Admin can always communicate with anyone
    if is_admin:
        return True, "Admin privileges"

    # Self-communication not allowed (use internal methods)
    if sender_id == recipient_id:
        return False, "Self-communication not allowed"

    # Admin agent can always be contacted
    if recipient_id == "admin" or recipient_id.lower().startswith("admin"):
        return True, "Admin agent always contactable"

    # Worker→worker: gated by per-project toggle (issue K).
    # Default-deny preserves upstream behavior; admin opts in via
    # project_context[config_allow_worker_to_worker].
    if not _access._get_config_bool("config_allow_worker_to_worker", default=False):
        return False, "Worker-to-worker messaging disabled by policy"

    # Toggle is on. Permit when both sides are currently active agents.
    active_ids = _agents_active_by_id()
    if sender_id in active_ids and recipient_id in active_ids:
        return True, "Both agents are active"

    return False, "Communication not permitted between these agents"


@requires_policy("config_allow_worker_to_worker", default=False)
async def send_agent_message_tool_impl(arguments: Dict[str, Any]) -> List[mcp_types.TextContent]:
    """
    Send a message from one agent to another with permission checks.
    Messages can be delivered via tmux session or stored for later retrieval.
    """
    sender_token = arguments.get("token")
    recipient_id = arguments.get("recipient_id")
    message_content = arguments.get("message")
    message_type = arguments.get("message_type", "text")  # text, assistance_request, task_update
    priority = arguments.get("priority", "normal")  # low, normal, high, urgent
    deliver_method = arguments.get("deliver_method", "tmux")  # tmux, store, both
    # v5.0.22 (message threads + subjects). Both optional:
    #   * explicit_subject — caller-provided one-liner; persisted
    #     verbatim. Replies (parent_message_id set) ignore it.
    #   * parent_message_id — link to a root message. Replies always
    #     end up with subject NULL.
    explicit_subject = arguments.get("subject")
    parent_message_id = arguments.get("parent_message_id")

    # The @requires_policy decorator already guaranteed `sender_token`
    # resolves to either admin or a worker permitted under
    # config_allow_worker_to_worker. We still need `sender_id` (and
    # `is_admin` below) for the message metadata and the per-pair
    # delivery rules in `_can_agents_communicate`.
    sender_id = get_agent_id(sender_token)

    # Validation
    if not recipient_id or not message_content:
        return [mcp_types.TextContent(type="text", text="Error: recipient_id and message are required")]
    
    if len(message_content) > 4000:  # Reasonable message size limit
        return [mcp_types.TextContent(type="text", text="Error: Message too long (max 4000 characters)")]
    
    # Admin-only check for stop commands
    is_admin = verify_token(sender_token, "admin")
    if message_type == "stop_command" and not is_admin:
        return [mcp_types.TextContent(type="text", text="Error: Only admin can send stop commands")]
    
    # Permission check
    can_communicate, reason = _can_agents_communicate(sender_id, recipient_id, is_admin)
    
    if not can_communicate:
        return [mcp_types.TextContent(type="text", text=f"Communication denied: {reason}")]
    
    # Create message data
    message_id = _generate_message_id()
    timestamp = datetime.datetime.now().isoformat()

    # v5.0.22: compute the effective subject. Three branches:
    #   1. Reply (parent_message_id set) → always NULL. The dashboard
    #      surfaces the root's subject as the thread label; replies
    #      don't carry their own.
    #   2. Explicit subject supplied → persist verbatim.
    #   3. Root w/o explicit subject → ask the Ollama helper
    #      (`suggest_subject`). If unconfigured or it returns None,
    #      fall back to a truncated body.
    effective_subject: Optional[str]
    if parent_message_id:
        effective_subject = None
    elif explicit_subject:
        effective_subject = explicit_subject
    else:
        # Two-stage gate so tests / operators can disable Ollama
        # without paying the import + helper-call cost:
        #   1. AGENT_MCP_SUBJECT_MODEL unset → straight to fallback.
        #   2. Model set → call helper; if it returns None (HTTP
        #      failure, empty completion, etc.) fall back too.
        suggested: Optional[str] = None
        if os.environ.get("AGENT_MCP_SUBJECT_MODEL", "").strip():
            from ..features.message_suggestions import suggest_subject

            suggested = await suggest_subject(message_content)

        if suggested:
            effective_subject = suggested
        else:
            # Truncated-body fallback. 50-char + "..." matches the
            # locked-in plan and the backfill script's "no Ollama"
            # branch — so a backfill run on a host without the model
            # leaves the same shape as the live send path.
            effective_subject = (
                message_content[:50] + "..."
                if len(message_content) > 50
                else message_content
            )

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Store message in database
        cursor.execute(
            """
            INSERT INTO agent_messages (message_id, sender_id, recipient_id,
                                        message_content, message_type, priority,
                                        timestamp, delivered, read,
                                        subject, parent_message_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id, sender_id, recipient_id, message_content,
                message_type, priority, timestamp, False, False,
                effective_subject, parent_message_id,
            ),
        )
        
        # Attempt delivery based on method
        delivery_status = "stored"
        
        if deliver_method in ["tmux", "both"]:
            # Try to deliver to recipient's tmux session
            if recipient_id in g.agent_tmux_sessions:
                session_name = g.agent_tmux_sessions[recipient_id]
                if session_exists(session_name):
                    # Handle stop commands differently
                    if message_type == "stop_command":
                        # Send control sequence to interrupt the agent
                        try:
                            import subprocess
                            clean_session_name = sanitize_session_name(session_name)
                            
                            # Send Escape 4 times with 1 second intervals to stop current operation
                            import time
                            success = True
                            for i in range(4):
                                result = subprocess.run(['tmux', 'send-keys', '-t', clean_session_name, 'Escape'], 
                                                      capture_output=True, text=True, timeout=5)
                                if result.returncode != 0:
                                    success = False
                                    break
                                logger.debug(f"Sent Escape {i+1}/4 to agent {recipient_id}")
                                if i < 3:  # Don't sleep after the last one
                                    time.sleep(1)
                            
                            if success:
                                delivery_status = "delivered_stop_command"
                                logger.info(f"Stop command (4x Escape) sent to agent {recipient_id} in session {session_name}")
                            else:
                                delivery_status = "stop_command_failed"
                                logger.error(f"Failed to send stop command: {result.stderr}")
                            
                            # Mark as delivered in database
                            cursor.execute("UPDATE agent_messages SET delivered = ? WHERE message_id = ?", 
                                         (success, message_id))
                                         
                        except Exception as e:
                            logger.error(f"Failed to send stop command to tmux session '{session_name}': {e}")
                            delivery_status = "stop_command_failed"
                    else:
                        # Format regular message for delivery
                        formatted_message = f"\n💬 Message from {sender_id} ({priority}): {message_content}\n"
                        
                        # Send message to tmux session
                        try:
                            send_prompt_async(session_name, formatted_message, delay_seconds=1)
                            delivery_status = "delivered_tmux"
                            
                            # Mark as delivered in database
                            cursor.execute("UPDATE agent_messages SET delivered = ? WHERE message_id = ?", 
                                         (True, message_id))
                            
                        except Exception as e:
                            logger.error(f"Failed to deliver message to tmux session '{session_name}': {e}")
                            delivery_status = "delivery_failed"
                else:
                    delivery_status = "session_not_found"
            else:
                delivery_status = "no_session"
        
        # Log the communication
        log_agent_action_to_db(cursor, sender_id, "send_message", 
                               details={
                                   "recipient": recipient_id,
                                   "message_type": message_type,
                                   "priority": priority,
                                   "delivery_status": delivery_status
                               })
        
        conn.commit()

        # Wake any `wait_for_events` waiter for the recipient AND fan
        # out `notifications/resources/updated` on every registered
        # GET /mcp stream for them. Single helper so both sinks fire
        # in lockstep (per-recipient wake covers broadcasts too — they
        # call this impl in a loop, one notify per recipient row).
        # Called AFTER commit so the waiter's re-query sees the new row.
        try:
            g.notify_agent_inbox(recipient_id)
        except Exception as e:  # pragma: no cover - defensive
            logger.warning(
                "notify_agent_inbox(%s) raised after send_agent_message: %s",
                recipient_id, e,
            )

        # AoE notification side-channel (best-effort, fire-and-forget).
        # Disabled by default; admins opt in via
        # project_context[config_aoe_notify_enabled]. Never blocks or
        # raises — the message is already persisted, and AoE is just
        # a tmux-pane wake-up call.
        try:
            asyncio.create_task(
                _aoe_notify(recipient_id, sender_id, message_id)
            )
        except RuntimeError:
            # No running event loop (e.g. unit tests calling the impl
            # synchronously). Run inline; still swallows errors.
            await _aoe_notify(recipient_id, sender_id, message_id)

        # Audit log
        log_audit(sender_id, "send_agent_message", {
            "recipient": recipient_id,
            "message_type": message_type,
            "priority": priority,
            "delivery_status": delivery_status,
            "message_id": message_id
        })
        
        # Build response
        status_messages = {
            "stored": "Message stored for recipient",
            "delivered_tmux": "Message delivered to recipient's session",
            "delivery_failed": "Message stored but delivery failed",
            "session_not_found": "Message stored; recipient session not active",
            "no_session": "Message stored; recipient has no active session",
            "delivered_stop_command": "Stop command sent to recipient's session",
            "stop_command_failed": "Stop command failed to send"
        }
        
        response_text = f"Message sent to {recipient_id}. {status_messages.get(delivery_status, 'Unknown status')}"
        
        if delivery_status not in ["delivered_tmux", "delivered_stop_command"]:
            response_text += f" (Message ID: {message_id})"
        
        return [mcp_types.TextContent(type="text", text=response_text)]
        
    except sqlite3.Error as e:
        if conn: conn.rollback()
        logger.error(f"Database error sending message: {e}", exc_info=True)
        return [mcp_types.TextContent(type="text", text=f"Database error sending message: {e}")]
    except Exception as e:
        if conn: conn.rollback()
        logger.error(f"Unexpected error sending message: {e}", exc_info=True)
        return [mcp_types.TextContent(type="text", text=f"Unexpected error sending message: {e}")]
    finally:
        if conn:
            conn.close()


@requires("any")
async def get_agent_messages_tool_impl(arguments: Dict[str, Any]) -> List[mcp_types.TextContent]:
    """
    Retrieve messages for an agent.
    """
    agent_token = arguments.get("token")
    include_sent = arguments.get("include_sent", False)
    include_received = arguments.get("include_received", True)
    mark_as_read = arguments.get("mark_as_read", True)
    limit = arguments.get("limit", 20)
    message_type_filter = arguments.get("message_type")
    unread_only = arguments.get("unread_only", False)

    # @requires("any") guaranteed agent_token resolves; we still need
    # the id for filtering & audit.
    agent_id = get_agent_id(agent_token)

    # Validation
    try:
        limit = int(limit)
        if not (1 <= limit <= 100):
            limit = 20
    except (ValueError, TypeError):
        limit = 20
    
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Build query
        query_conditions = []
        query_params = []
        
        if include_received and include_sent:
            query_conditions.append("(recipient_id = ? OR sender_id = ?)")
            query_params.extend([agent_id, agent_id])
        elif include_received:
            query_conditions.append("recipient_id = ?")
            query_params.append(agent_id)
        elif include_sent:
            query_conditions.append("sender_id = ?")
            query_params.append(agent_id)
        else:
            return [mcp_types.TextContent(type="text", text="Error: Must include sent or received messages")]
        
        if message_type_filter:
            query_conditions.append("message_type = ?")
            query_params.append(message_type_filter)
        
        if unread_only:
            query_conditions.append("read = ?")
            query_params.append(False)
        
        where_clause = " AND ".join(query_conditions)
        
        query = f"""
            SELECT message_id, sender_id, recipient_id, message_content, message_type,
                   priority, timestamp, delivered, read,
                   subject, parent_message_id
            FROM agent_messages
            WHERE {where_clause}
            ORDER BY timestamp DESC
            LIMIT ?
        """
        query_params.append(limit)
        
        cursor.execute(query, query_params)
        messages = cursor.fetchall()
        
        # Mark received messages as read if requested.
        #
        # Pre-2026-06-02 behavior: filter the LIMIT-bounded result set
        # in Python (`msg["recipient_id"] == agent_id and not
        # msg["read"]`), then issue an UPDATE with an IN-list of
        # message_ids. Two problems:
        #   1. Unread messages beyond `limit` stayed unread — surprising
        #      since the user asked to mark "as read" without qualifier.
        #   2. The Python filter + IN-list is unnecessary when SQL can
        #      express the same predicate in a single UPDATE.
        #
        # New behavior per the 2026-06-02 database review (item 10):
        # one UPDATE keyed on `(recipient_id, read = 0)` covers every
        # unread message addressed to this agent — including those
        # truncated by the SELECT's LIMIT. The fetched message rows
        # we return still show their pre-update read flag (we don't
        # re-fetch), so the response shape is unchanged from the
        # caller's perspective.
        if mark_as_read and include_received:
            cursor.execute(
                "UPDATE agent_messages SET read = 1 "
                "WHERE recipient_id = ? AND read = 0",
                (agent_id,),
            )
            if cursor.rowcount:
                conn.commit()
        
        # Format response
        if not messages:
            return [mcp_types.TextContent(type="text", text="No messages found")]
        
        response_lines = [f"Messages for {agent_id} (showing {len(messages)} of max {limit}):"]
        response_lines.append("")
        
        for msg in messages:
            direction = "➡️" if msg["sender_id"] == agent_id else "⬅️"
            other_agent = msg["recipient_id"] if msg["sender_id"] == agent_id else msg["sender_id"]
            read_status = "📖" if msg["read"] else "📩"
            priority_icon = {"low": "🔵", "normal": "⚪", "high": "🟡", "urgent": "🔴"}.get(msg["priority"], "⚪")

            response_lines.append(f"{direction} {read_status} {priority_icon} [{msg['message_type']}] {other_agent}")
            response_lines.append(f"   {msg['timestamp']}")
            # v5.0.22: surface subject (root) or reply-marker (reply).
            # sqlite3.Row supports `in` via .keys(); guard for legacy
            # callers that may not have migrated yet.
            row_keys = set(msg.keys()) if hasattr(msg, "keys") else set()
            subj = msg["subject"] if "subject" in row_keys else None
            parent_id = (
                msg["parent_message_id"]
                if "parent_message_id" in row_keys
                else None
            )
            if subj:
                response_lines.append(f"   Subject: {subj}")
            elif parent_id:
                response_lines.append(f"   ↳ reply to: {parent_id}")
            response_lines.append(f"   {msg['message_content']}")
            response_lines.append("")
        
        log_audit(agent_id, "get_agent_messages", {
            "messages_retrieved": len(messages),
            "include_sent": include_sent,
            "include_received": include_received
        })
        
        return [mcp_types.TextContent(type="text", text="\n".join(response_lines))]
        
    except sqlite3.Error as e:
        logger.error(f"Database error retrieving messages: {e}", exc_info=True)
        return [mcp_types.TextContent(type="text", text=f"Database error retrieving messages: {e}")]
    except Exception as e:
        logger.error(f"Unexpected error retrieving messages: {e}", exc_info=True)
        return [mcp_types.TextContent(type="text", text=f"Unexpected error retrieving messages: {e}")]
    finally:
        if conn:
            conn.close()


@requires("admin")
async def broadcast_admin_message_tool_impl(arguments: Dict[str, Any]) -> List[mcp_types.TextContent]:
    """
    Admin-only tool to broadcast a message to all active agents.
    """
    admin_token = arguments.get("token")
    message_content = arguments.get("message")
    message_type = arguments.get("message_type", "broadcast")
    priority = arguments.get("priority", "high")
    # v5.0.22: broadcasts can carry an explicit subject. Each fan-out
    # send is a root message, so the per-recipient send_agent_message
    # call will compute the subject the same way (verbatim if set,
    # Ollama-suggested otherwise, truncated body as last resort).
    explicit_subject = arguments.get("subject")

    if not message_content:
        return [mcp_types.TextContent(type="text", text="Error: message is required")]
    
    # Get all active agents
    active_agents = list(g.active_agents.keys())
    if not active_agents:
        return [mcp_types.TextContent(type="text", text="No active agents to broadcast to")]
    
    # Send to each agent
    sent_count = 0
    failed_count = 0
    
    for agent_token in active_agents:
        agent_data = g.active_agents[agent_token]
        recipient_id = agent_data.get("agent_id")
        
        if recipient_id and recipient_id != "admin":  # Don't send to admin itself
            try:
                # Use the send message function
                fanout_args: Dict[str, Any] = {
                    "token": admin_token,
                    "recipient_id": recipient_id,
                    "message": message_content,
                    "message_type": message_type,
                    "priority": priority,
                    "deliver_method": "both",
                }
                if explicit_subject is not None:
                    fanout_args["subject"] = explicit_subject
                result = await send_agent_message_tool_impl(fanout_args)
                sent_count += 1
            except Exception as e:
                logger.error(f"Failed to send broadcast to {recipient_id}: {e}")
                failed_count += 1
    
    log_audit("admin", "broadcast_message", {
        "message_type": message_type,
        "priority": priority,
        "sent_count": sent_count,
        "failed_count": failed_count
    })
    
    return [mcp_types.TextContent(
        type="text", 
        text=f"Broadcast sent to {sent_count} agents. {failed_count} failed."
    )]


# ---------------------------------------------------------------------------
# wait_for_events long-poll tool (plan Phase 2 + PR-2 event-coord)
# ---------------------------------------------------------------------------

# Default and cap per locked grilling decision #3: 60s default keeps
# round-trips brisk and stays under typical HTTP intermediary
# idle-timeouts; 300s ceiling per the locked-decisions table.
#
# `AGENT_MCP_EVENT_WAIT_TIMEOUT` env var overrides the default at module
# load time. Per-call `timeout_seconds` argument still overrides per
# request, clamped to `WAIT_FOR_EVENTS_MAX_TIMEOUT`.
def _read_default_timeout() -> int:
    raw = os.environ.get("AGENT_MCP_EVENT_WAIT_TIMEOUT", "60")
    try:
        v = int(raw)
        return v if v > 0 else 60
    except (TypeError, ValueError):
        return 60


WAIT_FOR_EVENTS_DEFAULT_TIMEOUT = _read_default_timeout()
WAIT_FOR_EVENTS_MAX_TIMEOUT = 300
# How frequently the wake-loop re-checks the global + per-agent flags
# during a long wait. Set short enough that a toggle flip is visible to
# the operator within a handful of seconds (test 6 requires < 5s).
_FLAG_RECHECK_INTERVAL_SECONDS = 2.0


_BROADCAST_MESSAGE_TYPES = ("broadcast", "announcement", "system_alert")


def _collect_events_for(
    agent_id: str, since: Optional[str]
) -> List[Dict[str, Any]]:
    """Collect new events for `agent_id` strictly after the ISO-UTC
    timestamp `since`.

    Returns a chronologically-ordered (ASC) list of dicts:
    ``{"type": "<event_type>", "timestamp": "<iso>", "data": {...}}``.

    Event types:

    * ``message`` — direct message from `agent_messages` where
      `recipient_id = agent_id` AND `message_type` is NOT a broadcast
      variant.
    * ``broadcast`` — same source, but `message_type` is one of
      ``broadcast`` / ``announcement`` / ``system_alert`` (per the
      `broadcast_admin_message` enum).
    * ``task_assigned`` — task row whose `assigned_to` transitioned
      INTO `agent_id` since `since` (heuristic: `created_at > since`
      AND `assigned_to == agent_id`, OR `updated_at > since` AND
      `created_at <= since` if the row was reassigned — v1 keeps it
      simple by treating any change with `created_at > since` as a
      fresh assignment).
    * ``task_changed`` — any other touched task (`updated_at > since`)
      where `assigned_to == agent_id` and `created_at <= since`.

    The helper is also reused by the Phase 3 inbox resource (the plan
    explicitly factors this out so both surfaces stay in sync).
    """
    # No cursor → "what's there right now" doesn't really make sense
    # for an event stream. Treat absence as "since the epoch" so the
    # first call returns the latest backlog; in practice callers
    # always pass `since` after the first call.
    since_iso = since if since else "0000-01-01T00:00:00"

    events: List[Dict[str, Any]] = []
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # --- agent_messages -------------------------------------------------
        cursor.execute(
            """
            SELECT message_id, sender_id, recipient_id, message_content,
                   message_type, priority, timestamp, delivered, read
            FROM agent_messages
            WHERE recipient_id = ? AND timestamp > ?
            ORDER BY timestamp ASC
            """,
            (agent_id, since_iso),
        )
        for row in cursor.fetchall():
            data = {
                "message_id": row["message_id"],
                "sender_id": row["sender_id"],
                "recipient_id": row["recipient_id"],
                "message_content": row["message_content"],
                "message_type": row["message_type"],
                "priority": row["priority"],
                "timestamp": row["timestamp"],
                "delivered": bool(row["delivered"]),
                "read": bool(row["read"]),
            }
            evt_type = (
                "broadcast"
                if (row["message_type"] or "") in _BROADCAST_MESSAGE_TYPES
                else "message"
            )
            events.append({
                "type": evt_type,
                "timestamp": row["timestamp"],
                "data": data,
            })

        # --- tasks ----------------------------------------------------------
        cursor.execute(
            """
            SELECT task_id, title, description, assigned_to, created_by,
                   status, priority, created_at, updated_at, parent_task
            FROM tasks
            WHERE assigned_to = ? AND updated_at > ?
            ORDER BY updated_at ASC
            """,
            (agent_id, since_iso),
        )
        for row in cursor.fetchall():
            data = {
                "task_id": row["task_id"],
                "title": row["title"],
                "description": row["description"],
                "assigned_to": row["assigned_to"],
                "created_by": row["created_by"],
                "status": row["status"],
                "priority": row["priority"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "parent_task": row["parent_task"],
            }
            # v1 heuristic per the plan: rows created since the
            # cursor are treated as fresh assignments; older rows
            # touched since the cursor are mutations.
            created_at = row["created_at"] or ""
            if created_at > since_iso:
                evt_type = "task_assigned"
            else:
                evt_type = "task_changed"
            events.append({
                "type": evt_type,
                "timestamp": row["updated_at"],
                "data": data,
            })
    finally:
        if conn:
            conn.close()

    # Merge-sort by timestamp ASC. Stable sort preserves the per-source
    # arrival order on ties (which only happen at sub-millisecond
    # resolution if at all, but be defensive).
    events.sort(key=lambda e: e["timestamp"])
    return events


def _envelope(
    events: List[Dict[str, Any]], since: Optional[str]
) -> List[mcp_types.TextContent]:
    """Wrap collected events into the standard response envelope.

    `next_cursor` advances to the max timestamp seen, or stays at
    `since` if the call timed out with no activity (preserving the
    caller's progress through the timeline).
    """
    if events:
        next_cursor = max((e.get("timestamp") or "") for e in events) or (since or "")
    else:
        next_cursor = since or ""
    payload = {"events": events, "next_cursor": next_cursor}
    return [mcp_types.TextContent(
        type="text", text=json.dumps(payload, ensure_ascii=False)
    )]


# ---------------------------------------------------------------------------
# PR-2: flag check + stop_listening
# ---------------------------------------------------------------------------


def _check_auto_event_loop_flags(agent_id: str) -> tuple[bool, Optional[str]]:
    """Return (enabled, reason_when_disabled).

    The wake loop is enabled iff BOTH:
      * `project_context["config_auto_event_loop_global"]` is truthy
        (default TRUE — un-set means "no opt-out").
      * `agents.auto_event_loop` is truthy for `agent_id` (default TRUE
        from the column DEFAULT).

    When either is OFF, `reason_when_disabled` carries a short human
    string the impl drops into the `stop_listening` payload.
    """
    # Global flag — default TRUE (opt-out, not opt-in). Operators who
    # don't know about the toggle should still get the new behavior.
    global_on = _access._get_config_bool(
        "config_auto_event_loop_global", default=True,
    )
    if not global_on:
        return False, "config_auto_event_loop_global is OFF"

    # Per-agent flag — read fresh from DB on every call (cheap; one
    # indexed lookup) so a mid-flight toggle flip wins.
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT auto_event_loop FROM agents WHERE agent_id = ?",
            (agent_id,),
        )
        row = cursor.fetchone()
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(
            "wait_for_events: per-agent flag lookup failed for %s: %s "
            "(treating as ON)",
            agent_id, e,
        )
        return True, None
    finally:
        if conn:
            conn.close()

    if row is None:
        return False, f"agent '{agent_id}' not found"
    # SQLite stores BOOLEAN as INTEGER; both 0/1 and True/False arrive.
    per_agent_on = bool(row["auto_event_loop"])
    if not per_agent_on:
        return False, f"auto_event_loop is OFF for agent '{agent_id}'"
    return True, None


def _stop_listening_event(reason: str) -> Dict[str, Any]:
    """Build the canonical ``stop_listening`` event dict."""
    return {
        "type": "stop_listening",
        "ref_id": None,
        "timestamp": datetime.datetime.now().isoformat(),
        "payload": {"reason": reason},
    }


def _write_last_event_seen_at(agent_id: str, cursor_value: str) -> None:
    """Persist the high-water cursor to `agents.last_event_seen_at`.

    Best-effort: a DB failure here doesn't fail the tool call (the
    envelope is already built and the next call will just re-issue the
    same backlog query against the previous cursor).
    """
    if not cursor_value:
        return
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE agents SET last_event_seen_at = ? WHERE agent_id = ?",
            (cursor_value, agent_id),
        )
        conn.commit()
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(
            "wait_for_events: failed to persist last_event_seen_at "
            "for %s: %s",
            agent_id, e,
        )
    finally:
        if conn:
            conn.close()


def _read_last_event_seen_at(agent_id: str) -> Optional[str]:
    """Read the persisted cursor for `agent_id`, or None if unset."""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT last_event_seen_at FROM agents WHERE agent_id = ?",
            (agent_id,),
        )
        row = cursor.fetchone()
    except Exception:  # pragma: no cover - defensive
        return None
    finally:
        if conn:
            conn.close()
    if row is None:
        return None
    return row["last_event_seen_at"]


def _collect_unassigned_task_events_for(
    agent_id: str, since: Optional[str],
) -> List[Dict[str, Any]]:
    """Find unassigned tasks created after `since` that match this
    agent's capabilities.

    Reused by both `wait_for_events_tool_impl` (on wake) and
    `fetch_events_since_tool_impl`. Produces the same skinny payload
    that `notify_unassigned_task_appeared` pushes to the in-memory
    queue, so a worker that misses the push event still picks up the
    same shape on its next catch-up.
    """
    from ..utils.capability_normalization import normalize_capabilities

    since_iso = since if since else "0000-01-01T00:00:00"
    events: List[Dict[str, Any]] = []
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT capabilities FROM agents WHERE agent_id = ?",
            (agent_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return []
        try:
            agent_caps_raw = row["capabilities"] or "[]"
            agent_caps = set(
                normalize_capabilities(
                    json.loads(agent_caps_raw)
                    if isinstance(agent_caps_raw, str)
                    else list(agent_caps_raw)
                )
            )
        except Exception:
            agent_caps = set()

        cursor.execute(
            """
            SELECT task_id, title, priority, required_capabilities,
                   created_at
            FROM tasks
            WHERE assigned_to IS NULL
              AND created_at > ?
            ORDER BY created_at ASC
            """,
            (since_iso,),
        )
        for trow in cursor.fetchall():
            try:
                req_raw = trow["required_capabilities"] or "[]"
                req = set(
                    normalize_capabilities(
                        json.loads(req_raw)
                        if isinstance(req_raw, str)
                        else list(req_raw)
                    )
                )
            except Exception:
                req = set()
            # Subset match: req ⊆ agent_caps. Empty req → matches everyone.
            if not req.issubset(agent_caps):
                continue
            events.append({
                "type": "unassigned_task_appeared",
                "ref_id": trow["task_id"],
                "timestamp": trow["created_at"],
                "payload": {
                    "task_id": trow["task_id"],
                    "title": trow["title"],
                    "priority": trow["priority"],
                    "required_capabilities": sorted(req),
                },
            })
    finally:
        if conn:
            conn.close()
    return events


@requires("any")
async def wait_for_events_tool_impl(
    arguments: Dict[str, Any],
) -> List[mcp_types.TextContent]:
    """Long-poll for new events for the calling agent.

    Returns immediately with any events newer than `since`; otherwise
    blocks until `signal_for(agent_id).set()` fires or
    `timeout_seconds` (default 60, max 300) elapses.

    PR-2 hardening:
      * Reads `config_auto_event_loop_global` + `agents.auto_event_loop`
        on every call; returns ``stop_listening`` if either is OFF.
      * Per-agent serialization lock — a second concurrent call returns
        ``{"error": "another_wait_in_flight"}`` immediately (HTTP-409
        analog).
      * Rechecks flags during long waits at 2s cadence so a toggle flip
        wakes the call within ~5s with ``stop_listening``.
      * Drains the per-agent out-of-band queue (synthetic events like
        ``unassigned_task_appeared``) on every wake.
      * Persists the high-water cursor to `agents.last_event_seen_at`.
    """
    token = arguments.get("token")
    agent_id = get_agent_id(token)
    if not agent_id:
        # `@requires("any")` should have caught this, but be defensive
        # against contextvar-bridged invocations.
        return [mcp_types.TextContent(
            type="text",
            text="Unauthorized: token does not resolve to an agent",
        )]

    since = arguments.get("since")
    if since is not None and not isinstance(since, str):
        return [mcp_types.TextContent(
            type="text",
            text="Error: since must be an ISO-UTC timestamp string",
        )]

    raw_timeout = arguments.get(
        "timeout_seconds", WAIT_FOR_EVENTS_DEFAULT_TIMEOUT
    )
    try:
        timeout = int(raw_timeout)
    except (TypeError, ValueError):
        timeout = WAIT_FOR_EVENTS_DEFAULT_TIMEOUT
    if timeout <= 0:
        timeout = WAIT_FOR_EVENTS_DEFAULT_TIMEOUT
    if timeout > WAIT_FOR_EVENTS_MAX_TIMEOUT:
        timeout = WAIT_FOR_EVENTS_MAX_TIMEOUT

    # One-call-per-agent enforcement (HTTP 409 analog).
    lock = g.lock_for(agent_id)
    acquired = False
    try:
        # Non-blocking acquire — second concurrent call sees the lock
        # already held and gets the conflict envelope.
        try:
            await asyncio.wait_for(lock.acquire(), timeout=0.001)
            acquired = True
        except asyncio.TimeoutError:
            conflict_body = {
                "error": "another_wait_in_flight",
                "agent_id": agent_id,
                "message": (
                    "Another wait_for_events call is already in flight "
                    "for this agent. Only one concurrent call per agent "
                    "is supported."
                ),
            }
            return [mcp_types.TextContent(
                type="text", text=json.dumps(conflict_body)
            )]

        # Flag gate: if either toggle is OFF, return stop_listening now.
        enabled, reason = _check_auto_event_loop_flags(agent_id)
        if not enabled:
            stop_evt = _stop_listening_event(reason or "auto_event_loop is OFF")
            # Also drain any pending synthetic events so the queue
            # doesn't accumulate while the agent is opted out.
            g.drain_events(agent_id)
            return _envelope([stop_evt], since)

        # Fast path — combine DB backlog with queued synthetic events.
        events: List[Dict[str, Any]] = _collect_events_for(agent_id, since)
        events.extend(_collect_unassigned_task_events_for(agent_id, since))
        events.extend(g.drain_events(agent_id))
        if events:
            events.sort(key=lambda e: e.get("timestamp") or "")
            env = _envelope(events, since)
            cursor_value = (
                max((e.get("timestamp") or "") for e in events) or (since or "")
            )
            if cursor_value:
                _write_last_event_seen_at(agent_id, cursor_value)
            return env

        # Slow path — clear the signal, wait in short slices so flag
        # flips during the wait are caught within ~5s.
        sig = g.signal_for(agent_id)
        sig.clear()
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                return _envelope([], since)
            slice_timeout = min(_FLAG_RECHECK_INTERVAL_SECONDS, remaining)
            try:
                await asyncio.wait_for(sig.wait(), timeout=slice_timeout)
                # Woken — recheck flags first; the wake may have come
                # from `wake_for_flag_recheck` (toggle flip), in which
                # case the new flag state requires stop_listening.
                enabled, reason = _check_auto_event_loop_flags(agent_id)
                if not enabled:
                    stop_evt = _stop_listening_event(
                        reason or "auto_event_loop is OFF"
                    )
                    g.drain_events(agent_id)
                    return _envelope([stop_evt], since)
                # Drain everything that accumulated.
                events = _collect_events_for(agent_id, since)
                events.extend(_collect_unassigned_task_events_for(agent_id, since))
                events.extend(g.drain_events(agent_id))
                if events:
                    events.sort(key=lambda e: e.get("timestamp") or "")
                    env = _envelope(events, since)
                    cursor_value = (
                        max((e.get("timestamp") or "") for e in events)
                        or (since or "")
                    )
                    if cursor_value:
                        _write_last_event_seen_at(agent_id, cursor_value)
                    return env
                # Spurious wake — clear and loop.
                sig.clear()
            except asyncio.TimeoutError:
                # Slice expired without a wake — recheck flags so an
                # operator who flips a toggle during a long wait sees
                # it within `_FLAG_RECHECK_INTERVAL_SECONDS`.
                enabled, reason = _check_auto_event_loop_flags(agent_id)
                if not enabled:
                    stop_evt = _stop_listening_event(
                        reason or "auto_event_loop is OFF"
                    )
                    g.drain_events(agent_id)
                    return _envelope([stop_evt], since)
                # Loop and wait another slice (or until deadline).
                continue
    finally:
        if acquired:
            lock.release()


@requires("any")
async def fetch_events_since_tool_impl(
    arguments: Dict[str, Any],
) -> List[mcp_types.TextContent]:
    """Pure-DB catch-up: return events newer than `cursor` without
    blocking.

    Spec: ``fetch_events_since(cursor: str | None) -> {events, cursor}``.
    Called by a worker on session start (and after recovery from any
    ``wait_for_events`` error) to drain any backlog missed while the
    worker was disconnected. Wakes nobody, blocks on nothing — just a
    SELECT.

    When `cursor` is None, falls back to the persisted
    `agents.last_event_seen_at`. The returned `cursor` advances to the
    max timestamp seen (or the input cursor if no events).
    """
    token = arguments.get("token")
    agent_id = get_agent_id(token)
    if not agent_id:
        return [mcp_types.TextContent(
            type="text",
            text="Unauthorized: token does not resolve to an agent",
        )]

    cursor = arguments.get("cursor")
    if cursor is not None and not isinstance(cursor, str):
        return [mcp_types.TextContent(
            type="text",
            text="Error: cursor must be an ISO-UTC timestamp string or null",
        )]

    if cursor is None:
        cursor = _read_last_event_seen_at(agent_id)

    # Gather everything since the cursor: DB-backed events + matching
    # unassigned tasks. We deliberately do NOT drain the in-memory
    # queue here — fetch_events_since is the "fresh catch-up" path and
    # the queue contents are only meaningful in the context of a
    # wait_for_events session that established expectations about
    # ordering.
    events = _collect_events_for(agent_id, cursor)
    events.extend(_collect_unassigned_task_events_for(agent_id, cursor))
    events.sort(key=lambda e: e.get("timestamp") or "")
    if events:
        new_cursor = max((e.get("timestamp") or "") for e in events) or (cursor or "")
        _write_last_event_seen_at(agent_id, new_cursor)
    else:
        new_cursor = cursor or ""

    body = {"events": events, "cursor": new_cursor}
    return [mcp_types.TextContent(
        type="text", text=json.dumps(body, ensure_ascii=False)
    )]


def register_agent_communication_tools():
    """Register agent communication tools."""
    
    register_tool(
        name="send_agent_message",
        description="Send a message to another agent with permission checks and delivery options.",
        input_schema={
            "type": "object",
            "properties": {
                "token": {
                    "type": "string",
                    "description": "Sender's authentication token. Optional if Authorization: Bearer header is supplied (recommended)."
                },
                "recipient_id": {
                    "type": "string",
                    "description": "ID of the agent to send message to"
                },
                "message": {
                    "type": "string",
                    "description": "Message content (max 4000 characters)"
                },
                "message_type": {
                    "type": "string",
                    "description": "Type of message",
                    "enum": ["text", "assistance_request", "task_update", "notification", "stop_command"],
                    "default": "text"
                },
                "priority": {
                    "type": "string",
                    "description": "Message priority",
                    "enum": ["low", "normal", "high", "urgent"],
                    "default": "normal"
                },
                "deliver_method": {
                    "type": "string",
                    "description": "How to deliver the message",
                    "enum": ["tmux", "store", "both"],
                    "default": "tmux"
                },
                "subject": {
                    "type": ["string", "null"],
                    "description": (
                        "Optional one-line subject for root messages. "
                        "Ignored for replies (parent_message_id set). "
                        "If omitted on a root message, an Ollama-backed "
                        "helper proposes one when AGENT_MCP_SUBJECT_MODEL "
                        "is configured; otherwise the body is truncated "
                        "to 50 chars + '...' as a fallback."
                    ),
                },
                "parent_message_id": {
                    "type": ["string", "null"],
                    "description": (
                        "If set, this message is a reply to the named "
                        "root message_id. Replies always have subject = NULL."
                    ),
                },
            },
            "required": ["recipient_id", "message"],
            "additionalProperties": False
        },
        implementation=send_agent_message_tool_impl,
        visibility=(
            "worker-if-toggled:config_allow_worker_to_worker"
        ),
    )
    
    register_tool(
        name="get_agent_messages",
        description="Retrieve messages for the current agent.",
        input_schema={
            "type": "object",
            "properties": {
                "token": {
                    "type": "string",
                    "description": "Agent's authentication token. Optional if Authorization: Bearer header is supplied (recommended)."
                },
                "include_sent": {
                    "type": "boolean",
                    "description": "Include messages sent by this agent",
                    "default": False
                },
                "include_received": {
                    "type": "boolean",
                    "description": "Include messages received by this agent",
                    "default": True
                },
                "mark_as_read": {
                    "type": "boolean",
                    "description": "Mark retrieved messages as read",
                    "default": True
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of messages to retrieve",
                    "default": 20,
                    "minimum": 1,
                    "maximum": 100
                },
                "message_type": {
                    "type": "string",
                    "description": "Filter by message type",
                    "enum": ["text", "assistance_request", "task_update", "notification", "stop_command"]
                },
                "unread_only": {
                    "type": "boolean",
                    "description": "Only show unread messages",
                    "default": False
                }
            },
            "required": [],
            "additionalProperties": False
        },
        implementation=get_agent_messages_tool_impl
    )
    
    register_tool(
        name="broadcast_admin_message",
        description="Admin-only tool to broadcast a message to all active agents.",
        input_schema={
            "type": "object",
            "properties": {
                "token": {
                    "type": "string",
                    "description": "Admin authentication token. Optional if Authorization: Bearer header is supplied (recommended)."
                },
                "message": {
                    "type": "string",
                    "description": "Message content to broadcast"
                },
                "message_type": {
                    "type": "string",
                    "description": "Type of broadcast message",
                    "enum": ["broadcast", "announcement", "system_alert"],
                    "default": "broadcast"
                },
                "priority": {
                    "type": "string",
                    "description": "Message priority",
                    "enum": ["low", "normal", "high", "urgent"],
                    "default": "high"
                },
                "subject": {
                    "type": ["string", "null"],
                    "description": (
                        "Optional subject applied to every fan-out root "
                        "message. Omit to let each per-recipient send go "
                        "through the standard Ollama/truncated-body path."
                    ),
                },
            },
            "required": ["message"],
            "additionalProperties": False
        },
        implementation=broadcast_admin_message_tool_impl,
        visibility="admin",
    )

    register_tool(
        name="fetch_events_since",
        description=(
            "Pure-DB catch-up: return events addressed to the calling "
            "agent that are newer than `cursor`, without blocking. Use "
            "this on session start (and after recovery from any "
            "wait_for_events error) to drain anything missed while "
            "disconnected. When `cursor` is omitted/null, falls back to "
            "the agent's persisted `last_event_seen_at`. Response is a "
            "JSON envelope {\"events\": [...], \"cursor\": \"<iso-ts>\"}; "
            "pass the returned `cursor` as the next `cursor` (or to "
            "wait_for_events as `since`) to advance through the timeline."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "token": {
                    "type": "string",
                    "description": (
                        "Calling agent's token. Optional if "
                        "Authorization: Bearer header is supplied "
                        "(recommended)."
                    ),
                },
                "cursor": {
                    "type": ["string", "null"],
                    "description": (
                        "ISO-UTC timestamp; only events with timestamp "
                        "> cursor are returned. Null/absent means start "
                        "from the agent's persisted last_event_seen_at."
                    ),
                },
            },
            "required": [],
            "additionalProperties": False,
        },
        implementation=fetch_events_since_tool_impl,
    )

    register_tool(
        name="wait_for_events",
        description=(
            "Long-poll for new events addressed to the calling agent "
            "(direct messages, broadcasts, task assignments / changes). "
            "Returns immediately if events are already pending; otherwise "
            "blocks server-side until something arrives or the timeout "
            "elapses. Response is a JSON envelope "
            "{\"events\": [{type, timestamp, data}, ...], \"next_cursor\": "
            "\"<iso-ts>\"} — pass `next_cursor` as `since` on the next "
            "call to advance through the timeline."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "token": {
                    "type": "string",
                    "description": (
                        "Calling agent's token. Optional if "
                        "Authorization: Bearer header is supplied "
                        "(recommended)."
                    ),
                },
                "since": {
                    "type": "string",
                    "description": (
                        "ISO-UTC timestamp; only events with "
                        "timestamp > since are returned. Pass the "
                        "previous response's `next_cursor` to advance."
                    ),
                },
                "timeout_seconds": {
                    "type": "integer",
                    "description": (
                        "Max seconds to wait for new activity before "
                        "returning an empty envelope. Default 60. "
                        "Values above 900 are silently clamped "
                        "server-side (no validation error)."
                    ),
                    "default": WAIT_FOR_EVENTS_DEFAULT_TIMEOUT,
                    "minimum": 1,
                },
            },
            "required": [],
            "additionalProperties": False,
        },
        implementation=wait_for_events_tool_impl,
    )


# Auto-register when imported
register_agent_communication_tools()