# Agent-MCP/agent_mcp/tools/agent_communication_tools.py
import asyncio
import json
import datetime
import secrets
import sqlite3
from typing import List, Dict, Any, Optional
import os

from .registry import register_tool
from . import access as _access  # Canonical home for _get_config_bool
from ..core.config import logger
from ..core import globals as g
from ..core.principal import Principal
from ..core.principal_builder import (
    build_agent_bearer_principal,
    is_operator_tier,
)
from ..core.tool_result import (
    Failed,
    Invalid,
    NotFound,
    Ok,
    PermissionDenied,
    ToolResult,
)
from ..features.aoe_notify import notify_aoe as _aoe_notify
from ..repositories.message_repository import ParentMessageNotFound
from ..utils.audit_utils import log_audit
from ..db.connection import get_db_connection
from ..db.unit_of_work import unit_of_work


class _MessageStoreFailed(RuntimeError):
    """Internal signal: ``message_repo.send`` returned ``None`` inside the
    ``atomic_with_audit`` block (PF-R32-1).

    Raised so the exception propagates out of the atomic block and rolls
    back the whole unit (no orphan audit/delivery row for a message that
    was never stored). Caught in ``send_agent_message_tool_impl`` and
    mapped to a ``Failed`` result — never a false success.
    """

    def __init__(self, message_id: str) -> None:
        self.message_id = message_id
        super().__init__(f"message store returned None for {message_id!r}")


# ── Wave 6 PR 6 helpers ────────────────────────────────────────────────
#
# The production dispatcher always supplies an explicit ``principal=``
# kwarg. Direct in-process / test call sites (a couple dozen tests,
# plus ``broadcast_admin_message_tool_impl`` fanning out to
# ``send_agent_message_tool_impl``) invoke the impl as a plain Python
# function and may not have one in scope; the helper below derives a
# Principal from ``arguments["token"]`` (a per-agent bearer) so those
# callers keep working without an explicit kwarg.


def _resolve_principal(
    arguments: Dict[str, Any],
    principal: Optional[Principal],
) -> Optional[Principal]:
    """Return the effective Principal for this call.

    Order:

    1. ``principal`` kwarg (the dispatcher path).
    2. ``arguments["token"]`` resolved to an active agent row (covers
       direct-impl test calls that seed ``token=<bearer>`` in the
       args dict).

    Returns ``None`` if no identity can be derived; the calling tool
    surfaces that as :class:`PermissionDenied` per the new contract.

    arch-B: the ``arguments["token"]`` fallback delegates to the shared
    :func:`agent_mcp.core.principal_builder.build_agent_bearer_principal`
    so a synthesized identity resolves its capabilities through the exact
    same path the middleware seam uses.
    """
    if principal is not None:
        return principal

    token = arguments.get("token")
    if token:
        return build_agent_bearer_principal(token)

    return None


# arch-B: the operator-tier predicate is defined once in
# ``core.principal_builder``. Bound to the historical private name so the
# call sites below (and the tests pinning it) keep working through the
# single shared definition — this copy had DRIFTED from the one in
# ``core.authorize`` (only this one honoured the ``agent_id == "admin"``
# label), which the shared definition now reconciles.
_is_operator_tier = is_operator_tier


def _sender_label(principal: Principal) -> str:
    """Pick the sender attribution string for an outgoing message.

    Mirrors the spec from the Wave 6 PR 2 brief: ``principal.agent_id
    or "operator"`` is the headline form. For operator-session callers
    without an agent_id, we prefer the operator's user_id (audit-log
    attribution stays specific) and only fall back to the literal
    ``"operator"`` when neither is set.
    """
    return principal.agent_id or principal.user_id or "operator"


def _generate_message_id() -> str:
    """Generate a unique message ID."""
    return f"msg_{secrets.token_hex(8)}"


def _agents_active_by_id() -> set[str]:
    """Set of agent_ids currently registered as active.

    PR-W2c: routed through AgentRepository so non-live agents are
    excluded by the canonical DB-level filter (LIVE_AGENT_SQL —
    excludes 'terminated' AND 'tombstone') and tokens
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

    # Admin agent can always be contacted. Match the canonical "admin"
    # identity EXACTLY — a startswith wildcard would let a worker message
    # any agent whose id merely begins with "admin" (e.g.
    # "admin-impersonator"), bypassing the worker→worker default-deny.
    if recipient_id.lower() == "admin":
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


async def send_agent_message_tool_impl(
    arguments: Dict[str, Any],
    *,
    principal: Optional[Principal] = None,
) -> ToolResult:
    """
    Send a message from one agent to another with permission checks.
    Messages can be delivered via tmux session or stored for later retrieval.

    Wave 6 PR 2: migrated to the Principal + ToolResult contract.
    Replaces the prior ``@requires_policy("config_allow_worker_to_worker",
    default=False)`` decorator with an inline gate driven by the
    Principal: operator-tier (operator session, sysadmin, or the legacy
    ``"admin"`` agent label per :func:`_is_operator_tier`) always passes;
    agent-bearer workers pass iff the worker-to-worker toggle is on. The
    sender attribution captured on the message row comes from
    :func:`_sender_label`.
    """
    principal = _resolve_principal(arguments, principal)
    if principal is None:
        return PermissionDenied(
            reason="Valid token or operator session required"
        )

    sender_id = _sender_label(principal)
    is_admin = _is_operator_tier(principal)

    # Auth gate (formerly @requires_policy("config_allow_worker_to_worker"))
    # Admin / operator: always permitted, no toggle read needed.
    # Agent-bearer (worker or manager): gated on the project toggle.
    # Note: pre-Wave-6 the decorator treated agent-role 'manager' as a
    # worker for this gate too (the check was `verify_token(token,
    # "admin")` which is operator-tier-only), so we preserve that.
    if not is_admin:
        if principal.kind != "agent_bearer":
            return PermissionDenied(reason="Valid token required")
        if not _access._get_config_bool(
            "config_allow_worker_to_worker", default=False
        ):
            return PermissionDenied(
                reason=(
                    "worker access denied by project policy "
                    "(config_allow_worker_to_worker is off). Ask admin "
                    "to enable it in dashboard Settings."
                )
            )

    recipient_id = arguments.get("recipient_id")
    message_content = arguments.get("message")
    message_type = arguments.get("message_type", "text")  # text, assistance_request, task_update
    priority = arguments.get("priority", "normal")  # low, normal, high, urgent
    # Wave 7 PR 3 (coordinator transition): ``deliver_method`` is kept
    # in the schema for back-compat but the value is ignored — every
    # message is stored in the DB and delivered via
    # ``wait_for_events`` / ``get_agent_messages``. agent-mcp no
    # longer owns the recipient's claude session, so there is no tmux
    # pane to push the formatted message into.
    arguments.get("deliver_method")
    # v5.0.22 (message threads + subjects). Both optional:
    #   * explicit_subject — caller-provided one-liner; persisted
    #     verbatim. Replies (parent_message_id set) ignore it.
    #   * parent_message_id — link to a root message. Replies always
    #     end up with subject NULL.
    explicit_subject = arguments.get("subject")
    parent_message_id = arguments.get("parent_message_id")

    # Validation
    if not recipient_id:
        return Invalid(
            field="recipient_id",
            message="recipient_id is required",
        )
    if not message_content:
        return Invalid(
            field="message",
            message="message is required",
        )

    if len(message_content) > 4000:  # Reasonable message size limit
        return Invalid(
            field="message",
            message="Message too long (max 4000 characters)",
        )

    # Admin-only check for stop commands. Operator-tier (or the legacy
    # "admin" pseudo-agent) is the only caller permitted to send a
    # stop_command; bridge-derived workers are rejected even if the
    # worker-to-worker toggle is on.
    if message_type == "stop_command" and not is_admin:
        return PermissionDenied(reason="Only admin can send stop commands")

    # Per-pair delivery rules — admin bypass + worker-to-worker active
    # set + admin recipient label.
    can_communicate, reason = _can_agents_communicate(
        sender_id, recipient_id, is_admin
    )

    if not can_communicate:
        return PermissionDenied(reason=f"Communication denied: {reason}")
    
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

    # D2: the send mutation runs on the write-path unit-of-work. The
    # message INSERT drives ``u.cursor``; the recipient inbox wake and
    # the (now unified) audit are only *registered* on ``u`` and flush
    # after a successful commit — so emit-iff-commit is structural. On
    # ANY exception inside the scope (ParentMessageNotFound, unknown
    # recipient LookupError, or send() → None → _MessageStoreFailed) the
    # uow rolls back and fires ZERO effects: no message row, no delivery,
    # no recipient wake, no audit row. That re-guards PF-R32-1's
    # silent-drop contract (a failed send never reports success nor
    # strands orphan audit/delivery state).
    delivery_status = "stored"
    try:
        with unit_of_work() as u:
            # PR 6: message INSERT goes through message_repo on the uow's
            # cursor so it's atomic with the audit row under one commit.
            from ..repositories import message_repo as _msg_repo
            stored = _msg_repo.send(
                message_id=message_id,
                sender_id=sender_id,
                recipient_id=recipient_id,
                message_content=message_content,
                message_type=message_type,
                priority=priority,
                timestamp=timestamp,
                delivered=False,
                read=False,
                subject=effective_subject,
                parent_message_id=parent_message_id,
                connection=u.cursor,
            )
            # PF-R32-1: honor send()'s result. A None return means the
            # INSERT failed for a reason send() swallowed internally (DB
            # error / bad bind). Raise to abort the WHOLE unit so no
            # audit/delivery row is committed for a message that was never
            # stored — never report a false success. (A nonexistent parent
            # no longer reaches here: send() now raises
            # ParentMessageNotFound up front, caught below.)
            if stored is None:
                raise _MessageStoreFailed(message_id)

            # Wave 7 PR 3 (coordinator transition): every message is
            # stored in the DB. Recipients pick it up via
            # ``wait_for_events`` / ``get_agent_messages`` — agent-mcp
            # no longer owns the recipient's claude session and so
            # cannot push directly to a tmux pane.
            delivery_status = "stored"

            # Register the recipient inbox wake to fire AFTER commit, so
            # the waiter's re-query sees the new row. notify_agent_inbox
            # wakes any `wait_for_events` waiter AND fans out
            # `notifications/resources/updated` on every registered GET
            # /mcp stream (per-recipient wake covers broadcasts too — the
            # broadcast tool loops this impl, one notify per recipient).
            # The bus swallows per-adapter errors and the uow's flush
            # additionally isolates it, so a broken wake can't crash a
            # send whose commit already happened.
            u.on_commit(lambda: g.notify_agent_inbox(recipient_id))

            # Unified audit: ONE register writes BOTH sinks (the DB
            # ``agent_actions`` row + the in-memory ``g.audit_log`` entry),
            # and only after commit. Replaces the split
            # ``atomic_with_audit(operation="send_message")`` DB row plus
            # the separate post-commit ``log_audit("send_agent_message")``
            # — the two audit sinks now agree on action + details.
            u.audit(
                sender_id,
                "send_message",
                details={
                    "recipient": recipient_id,
                    "message_type": message_type,
                    "priority": priority,
                    "delivery_status": delivery_status,
                    "message_id": message_id,
                },
            )

        # Clean exit above committed the message and flushed the wake +
        # audit. Everything below is post-commit and only runs on a
        # successful send (an exception inside the scope re-raises out of
        # the ``with`` and skips straight to the handlers below).

        # AoE notification side-channel (best-effort, fire-and-forget).
        # Disabled by default; admins opt in via
        # project_context[config_aoe_notify_enabled]. Async, so it stays
        # OUTSIDE the (synchronous) uow flush — it fires only after a
        # clean commit and never blocks or raises: the message is already
        # persisted, and AoE is just a tmux-pane wake-up call.
        try:
            asyncio.create_task(
                _aoe_notify(recipient_id, sender_id, message_id)
            )
        except RuntimeError:
            # No running event loop (e.g. unit tests calling the impl
            # synchronously). Run inline; still swallows errors.
            await _aoe_notify(recipient_id, sender_id, message_id)

        # Build response. Wave 7 PR 3: stop_command and regular text
        # both land on the "stored" outcome — the recipient's
        # ``wait_for_events`` long-poll surfaces them on the next wake.
        status_messages = {
            "stored": "Message stored for recipient",
        }

        response_text = (
            f"Message sent to {recipient_id}. "
            f"{status_messages.get(delivery_status, 'Unknown status')}"
        )

        response_text += f" (Message ID: {message_id})"

        return Ok(
            data={
                "message_id": message_id,
                "sender": sender_id,
                "recipient_id": recipient_id,
                "message_type": message_type,
                "priority": priority,
                "delivery_status": delivery_status,
                "subject": effective_subject,
                "parent_message_id": parent_message_id,
            },
            message=response_text,
        )

    except ParentMessageNotFound as e:
        # PF-R32-1: the reply named a parent_message_id that doesn't
        # exist. The unit-of-work already rolled back the message INSERT
        # and fired no effects (no audit row, no wake). Distinct from the
        # unknown-recipient LookupError below so the error names the
        # missing PARENT, not the recipient.
        logger.warning(f"send_agent_message rejected (unknown parent): {e}")
        return NotFound(
            resource="parent message",
            identifier=str(parent_message_id),
        )
    except LookupError as e:
        # Repository rejected an unknown recipient (VM e2e fix
        # 2026-06-16). The unit-of-work already rolled back the message
        # INSERT and fired no effects. Surface the repo's message
        # verbatim — it explains live / admin / tombstone semantics.
        logger.warning(f"send_agent_message rejected: {e}")
        return NotFound(resource="agent", identifier=str(recipient_id))
    except _MessageStoreFailed as e:
        # PF-R32-1: send() returned None (store failed for a reason it
        # swallowed). The unit-of-work rolled back and fired no effects,
        # so nothing was committed. Report an error — never a false
        # success.
        logger.error(
            "send_agent_message store failed (send returned None) for "
            "message %s to %s", e.message_id, recipient_id,
        )
        return Failed(message="Failed to send message")
    except sqlite3.Error as e:
        # The unit-of-work already rolled back + closed the conn before
        # re-raising; nothing left for us to do but report.
        logger.error(f"Database error sending message: {e}", exc_info=True)
        return Failed(message=f"Database error sending message: {e}")
    except Exception as e:
        logger.error(f"Unexpected error sending message: {e}", exc_info=True)
        return Failed(message=f"Unexpected error sending message: {e}")


async def get_agent_messages_tool_impl(
    arguments: Dict[str, Any],
    *,
    principal: Optional[Principal] = None,
) -> ToolResult:
    """
    Retrieve messages for an agent.

    Wave 6 PR 2: migrated to the Principal + ToolResult contract.
    The pre-migration ``@requires("any")`` gate admitted any
    valid-agent token; we keep that intent by requiring the Principal
    to identify an agent (either via ``agent_bearer`` or via a legacy
    operator caller carrying a per-agent bearer in
    ``arguments["token"]`` — the latter is resolved by
    :func:`_resolve_principal`'s token fallback).
    """
    principal = _resolve_principal(arguments, principal)
    if principal is None or not principal.agent_id:
        return PermissionDenied(
            reason="Valid agent token required to retrieve messages"
        )

    # Reads require the ``messages.view`` capability — a no-op for worker +
    # manager agents (both bundles carry it) and sysadmin. It DENIES the one
    # over-admit the identity-only gate above let through: an ``agent_bearer``
    # that identified an ``agent_id`` yet held zero caps (``agent_role`` None
    # → empty bundle). Same empty-bearer class ``ask_project_rag`` closed.
    if not principal.has_capability("messages.view"):
        return PermissionDenied(
            reason="messages.view capability required to retrieve messages"
        )

    agent_id = principal.agent_id

    include_sent = arguments.get("include_sent", False)
    include_received = arguments.get("include_received", True)
    mark_as_read = arguments.get("mark_as_read", True)
    limit = arguments.get("limit", 20)
    message_type_filter = arguments.get("message_type")
    unread_only = arguments.get("unread_only", False)

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
            return Invalid(
                field=None,
                message="Must include sent or received messages",
            )

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
        # SECURITY (round-2): the mark-read UPDATE must be scoped to
        # EXACTLY the rows this fetch returned. An unscoped
        # `UPDATE ... WHERE recipient_id = ? AND read = 0` (the
        # 2026-06-02 "item 10" behavior) marks the WHOLE inbox read,
        # including messages excluded by the `message_type` filter,
        # `unread_only`, or truncated by `LIMIT` — so a filtered/paged
        # view silently loses unread control messages the caller never
        # saw. Collect the ids of the RECEIVED rows on this page and
        # flip only those. `mark_read_by_ids` additionally scopes to
        # `recipient_id` (defense-in-depth) and fires the EventBus
        # `message.read` publish so subscribers don't have to poll.
        if mark_as_read and include_received:
            from ..repositories import message_repo
            received_ids = [
                msg["message_id"]
                for msg in messages
                if msg["recipient_id"] == agent_id and not msg["read"]
            ]
            if received_ids:
                message_repo.mark_read_by_ids(
                    received_ids, recipient_id=agent_id
                )
        
        # Format response
        if not messages:
            return Ok(
                data={"agent_id": agent_id, "messages": [], "count": 0},
                message="No messages found",
            )

        response_lines = [f"Messages for {agent_id} (showing {len(messages)} of max {limit}):"]
        response_lines.append("")

        rows_for_payload: List[Dict[str, Any]] = []
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
            rows_for_payload.append({
                "message_id": msg["message_id"],
                "sender_id": msg["sender_id"],
                "recipient_id": msg["recipient_id"],
                "message_content": msg["message_content"],
                "message_type": msg["message_type"],
                "priority": msg["priority"],
                "timestamp": msg["timestamp"],
                "delivered": bool(msg["delivered"]),
                "read": bool(msg["read"]),
                "subject": subj,
                "parent_message_id": parent_id,
            })

        log_audit(agent_id, "get_agent_messages", {
            "messages_retrieved": len(messages),
            "include_sent": include_sent,
            "include_received": include_received
        })

        return Ok(
            data={
                "agent_id": agent_id,
                "count": len(rows_for_payload),
                "messages": rows_for_payload,
            },
            message="\n".join(response_lines),
        )

    except sqlite3.Error as e:
        logger.error(f"Database error retrieving messages: {e}", exc_info=True)
        return Failed(message=f"Database error retrieving messages: {e}")
    except Exception as e:
        logger.error(f"Unexpected error retrieving messages: {e}", exc_info=True)
        return Failed(message=f"Unexpected error retrieving messages: {e}")
    finally:
        if conn:
            conn.close()


async def broadcast_admin_message_tool_impl(
    arguments: Dict[str, Any],
    *,
    principal: Optional[Principal] = None,
) -> ToolResult:
    """
    Admin-only tool to broadcast a message to all active agents.

    Wave 6 PR 2: migrated to the Principal + ToolResult contract.
    The pre-migration ``@requires_role("operator")`` gate is replaced
    with an inline :func:`_is_operator_tier` check (operator session,
    sysadmin, or the legacy ``"admin"`` agent label — see the helper's
    docstring for why the label is treated as operator-tier in the
    harness). Each fan-out call to
    :func:`send_agent_message_tool_impl` carries the SAME principal,
    so sender-id attribution stays consistent across the broadcast.
    """
    # retire-system-token Wave 5: parameter renamed from
    # ``admin_token`` — the value is a manager-tier per-agent token (or
    # the operator session passes via the @requires_role gate without
    # needing a token here). It was never the god-key after Wave 1.
    principal = _resolve_principal(arguments, principal)
    if principal is None or not _is_operator_tier(principal):
        return PermissionDenied(
            reason="Operator role required to broadcast"
        )

    message_content = arguments.get("message")
    message_type = arguments.get("message_type", "broadcast")
    priority = arguments.get("priority", "high")
    # v5.0.22: broadcasts can carry an explicit subject. Each fan-out
    # send is a root message, so the per-recipient send_agent_message
    # call will compute the subject the same way (verbatim if set,
    # Ollama-suggested otherwise, truncated body as last resort).
    explicit_subject = arguments.get("subject")

    if not message_content:
        return Invalid(field="message", message="message is required")

    # Get all active agents
    active_agents = list(g.active_agents.keys())
    if not active_agents:
        return Ok(
            data={"sent_count": 0, "failed_count": 0, "recipients": []},
            message="No active agents to broadcast to",
        )

    # Send to each agent
    sent_count = 0
    failed_count = 0
    recipients: List[str] = []

    for agent_token in active_agents:
        agent_data = g.active_agents[agent_token]
        recipient_id = agent_data.get("agent_id")

        if recipient_id and recipient_id != "admin":  # Don't send to admin itself
            try:
                # Use the send message function
                fanout_args: Dict[str, Any] = {
                    "recipient_id": recipient_id,
                    "message": message_content,
                    "message_type": message_type,
                    "priority": priority,
                    "deliver_method": "both",
                }
                if explicit_subject is not None:
                    fanout_args["subject"] = explicit_subject
                # Forward the caller's Principal so sender attribution
                # stays consistent (the fan-out send derives sender_id
                # from the same identity instead of re-deriving from
                # ContextVars on each call).
                result = await send_agent_message_tool_impl(
                    fanout_args, principal=principal,
                )
                if isinstance(result, Ok):
                    sent_count += 1
                    recipients.append(recipient_id)
                else:
                    failed_count += 1
                    logger.warning(
                        "broadcast fan-out to %s returned %r",
                        recipient_id, result,
                    )
            except Exception as e:
                logger.error(f"Failed to send broadcast to {recipient_id}: {e}")
                failed_count += 1

    log_audit(_sender_label(principal), "broadcast_message", {
        "message_type": message_type,
        "priority": priority,
        "sent_count": sent_count,
        "failed_count": failed_count
    })

    return Ok(
        data={
            "sent_count": sent_count,
            "failed_count": failed_count,
            "recipients": recipients,
            "message_type": message_type,
            "priority": priority,
        },
        message=f"Broadcast sent to {sent_count} agents. {failed_count} failed.",
    )


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

# Per-poll cap on the message backlog the event feed drains at once.
# Matches the `query()` upper bound. When more than this many messages
# have accrued since the cursor, one poll returns a contiguous
# OLDEST-first prefix and the cursor advances only to the prefix
# boundary, so the next poll drains the remainder in order (BL-R20-1).
_MESSAGE_EVENT_QUERY_CAP = 500


def _collect_events_for(
    agent_id: str, since: Optional[str]
) -> List[Dict[str, Any]]:
    """Back-compat shim: return just the event list.

    Retained for callers that don't merge the additional (unbounded)
    event streams — the inbox resource and the BL-R20 tests — and so
    only need the DB-backed events, not the truncation boundary. The
    long-poll / catch-up tool impls call :func:`_collect_events_with_cap`
    directly so they can propagate the clamp (BL-R21-1).
    """
    events, _msg_cap_ts = _collect_events_with_cap(agent_id, since)
    return events


def _collect_events_with_cap(
    agent_id: str, since: Optional[str]
) -> tuple[List[Dict[str, Any]], Optional[str]]:
    """Collect new events for `agent_id` strictly after the ISO-UTC
    timestamp `since`, plus the message-truncation boundary.

    Returns ``(events, msg_cap_ts)`` where ``events`` is a
    chronologically-ordered (ASC) list of dicts
    ``{"type": "<event_type>", "timestamp": "<iso>", "data": {...}}`` and
    ``msg_cap_ts`` is:

      * the timestamp of the newest message in the batch **when the
        message backlog filled the query cap** (the batch is a truncated
        contiguous prefix), or
      * ``None`` when the batch was NOT truncated.

    BL-R21-1: the caller MUST cap its final (merged) cursor to
    ``msg_cap_ts`` when it is not ``None``. The internal clamp below only
    trims THIS function's own events (messages + assigned tasks); the
    unbounded streams the callers merge in afterwards
    (``unassigned_task_appeared`` + the synthetic queue) would otherwise
    drag the global ``max()`` cursor past the un-returned messages 501+.

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

    # --- agent_messages (PR 6: routed through MessageRepository.query) ---
    # The repo's `since` filter uses `>=` while the legacy raw SQL was
    # strict `>`. The cursor-string comparison is over ISO timestamps,
    # so we bump the cursor by an "epsilon" (1 char) so `>= cursor+ε`
    # is equivalent to `> cursor` for ASCII-only ISO timestamps.
    from ..repositories import message_repo
    # BL-R20-1: request the OLDEST messages since the cursor first
    # (timestamp ASC, capped at `_MESSAGE_EVENT_QUERY_CAP`). The batch
    # is then a contiguous prefix starting at the cursor, so advancing
    # the cursor to max(returned) can only skip past messages we
    # actually delivered. The previous DESC-then-reverse fetch returned
    # the NEWEST 500 and let the cursor jump past a >500 backlog's
    # oldest tail — permanent event loss on catch-up plus a
    # control-message-burying / censorship vector (flood 500+ messages
    # right after a critical one so the critical sits in the dropped
    # tail).
    msg_rows = message_repo.query(
        {"to": agent_id, "since": since_iso,
         "limit": _MESSAGE_EVENT_QUERY_CAP},
        oldest_first=True,
    )
    # When the message backlog fills the cap, the batch is truncated:
    # the cursor must NOT advance past the newest message we returned,
    # or messages between the prefix boundary and any newer (unbounded)
    # task event would be skipped. `msg_cap_ts` is that boundary.
    messages_truncated = len(msg_rows) >= _MESSAGE_EVENT_QUERY_CAP
    msg_cap_ts = msg_rows[-1].get("timestamp") if msg_rows else None
    for row in msg_rows:
        # repo `since` is inclusive (`>=`); the legacy SQL was strict
        # `>`. Re-apply the strict filter here to preserve the old
        # behaviour — a message exactly at `since_iso` shouldn't fire
        # again on the next long-poll wake.
        if (row.get("timestamp") or "") <= since_iso:
            continue
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

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

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

    # BL-R20-1: when the message batch was truncated at the cap, clamp
    # the whole batch to the prefix boundary so the cursor (which
    # advances to max(returned)) can't leap past undelivered messages
    # via a newer, unbounded task event. Task events beyond the boundary
    # are re-collected on the next poll (their query is `updated_at >
    # cursor`), so nothing is lost — the backlog just drains in order.
    truncation_boundary: Optional[str] = None
    if messages_truncated and msg_cap_ts:
        events = [
            e for e in events
            if (e.get("timestamp") or "") <= msg_cap_ts
        ]
        # Propagate the boundary so the callers can cap their MERGED
        # cursor (BL-R21-1) — the unbounded streams they add afterwards
        # are not visible to this internal clamp.
        truncation_boundary = msg_cap_ts

    # Merge-sort by timestamp ASC. Stable sort preserves the per-source
    # arrival order on ties (which only happen at sub-millisecond
    # resolution if at all, but be defensive).
    events.sort(key=lambda e: e["timestamp"])
    return events, truncation_boundary


def _cap_events_to_boundary(
    events: List[Dict[str, Any]], msg_cap_ts: Optional[str],
) -> List[Dict[str, Any]]:
    """Clamp a merged event batch to the message-truncation boundary.

    BL-R21-1: when the message backlog was truncated (``msg_cap_ts`` is
    not ``None``), drop every merged event newer than the boundary so
    the returned batch AND the cursor derived from it
    (``max(timestamp)``) never advance past the last delivered message.

    The dropped events are all re-derivable on the next poll:
    ``unassigned_task_appeared`` rows are re-queried by
    :func:`_collect_unassigned_task_events_for` (``updated_at > cursor``),
    and the synthetic-queue copies are wake-edge notifications for those
    same DB rows (see ``state.dispatch_synthetic_event``), so nothing is
    lost — the backlog just drains in timestamp order.

    When ``msg_cap_ts`` is ``None`` (no truncation) the batch is
    returned unchanged.
    """
    if msg_cap_ts is None:
        return events
    return [
        e for e in events
        if (e.get("timestamp") or "") <= msg_cap_ts
    ]


# BL-R31-2: event types that can arrive in BOTH the DB re-query stream
# AND the synthetic in-memory queue for the SAME logical event. These
# are exactly the synthetic event types (``event_bus._SYNTHETIC_EVENT_
# TYPES``): ``notify_unassigned_task_appeared`` fans out a wall-clock-
# timestamped queue copy while ``_collect_unassigned_task_events_for``
# re-queries an ``updated_at``-timestamped DB copy of the same row.
# Kept as a small local frozenset (rather than importing the private bus
# constant into the tool layer); the dedup tests pin the invariant that
# the two sets track each other.
_DEDUP_EVENT_TYPES = frozenset({"unassigned_task_appeared"})


def _event_identity(event: Dict[str, Any]) -> Optional[tuple]:
    """Stable logical identity for an event, or ``None`` when the event
    has no cross-stream identity (never deduped).

    Only the dual-source event types (:data:`_DEDUP_EVENT_TYPES`) get an
    identity: the DB re-query copy and the synthetic queue copy of the
    SAME task must collapse to one delivery. Every other event type
    comes from a single source (one DB query returns each row once), so
    it is left un-keyed and can never be collapsed — genuinely-distinct
    events stay distinct.
    """
    etype = event.get("type")
    if etype not in _DEDUP_EVENT_TYPES:
        return None
    ref = event.get("ref_id")
    if ref is None:
        ref = (event.get("payload") or {}).get("task_id")
    if ref is None:
        return None
    return (etype, ref)


def _dedup_events(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Collapse duplicate copies of the same logical event (BL-R31-2).

    ``unassigned_task_appeared`` is delivered from two sources that
    describe the SAME task: a DB re-query copy timestamped by the task's
    ``updated_at`` and a synthetic in-memory-queue copy timestamped by
    wall-clock ``now()``. Merging them without dedup delivers the task
    twice per envelope, so an auto-claiming worker double-claims it.

    For each stable identity we keep the copy with the EARLIEST
    timestamp. The DB ``updated_at`` is always <= the synthetic wake's
    ``now()`` (the notify fires after the row is committed), so this
    keeps the DB copy — preserving the BL-R20-1 oldest-first ordering
    and the BL-R21-1 clamp/cursor anchor: the surviving event's
    timestamp is the real DB transition time, not a later wall-clock
    instant that could drag the persisted cursor forward or dodge the
    message-truncation clamp. Dedup therefore runs BEFORE
    :func:`_cap_events_to_boundary` at every call site.

    Events with no identity (:func:`_event_identity` returns ``None``)
    are passed through unchanged. Relative order of the kept events is
    otherwise preserved (the caller re-sorts by timestamp afterwards).
    """
    seen: Dict[tuple, int] = {}
    out: List[Dict[str, Any]] = []
    for e in events:
        ident = _event_identity(e)
        if ident is None:
            out.append(e)
            continue
        prev_idx = seen.get(ident)
        if prev_idx is None:
            seen[ident] = len(out)
            out.append(e)
            continue
        # Duplicate identity: keep the earlier-timestamped copy (the DB
        # ``updated_at`` copy over the wall-clock synthetic copy).
        if (e.get("timestamp") or "") < (
            out[prev_idx].get("timestamp") or ""
        ):
            out[prev_idx] = e
    return out


def _envelope(
    events: List[Dict[str, Any]], since: Optional[str]
) -> Ok:
    """Wrap collected events into the standard response envelope.

    `next_cursor` advances to the max timestamp seen, or stays at
    `since` if the call timed out with no activity (preserving the
    caller's progress through the timeline).

    Wave 6 PR 2: returns :class:`Ok` so the MCP wire renderer
    serializes the payload as a JSON text-content block (the
    historical wire shape) while REST consumers see the data field
    directly. The ``message`` field carries the JSON-encoded
    envelope so :func:`render_as_text_content` produces the same
    wire bytes existing clients (``wait_for_events`` / inbox readers)
    already parse.
    """
    if events:
        next_cursor = max((e.get("timestamp") or "") for e in events) or (since or "")
    else:
        next_cursor = since or ""
    payload = {"events": events, "next_cursor": next_cursor}
    return Ok(
        data=payload,
        message=json.dumps(payload, ensure_ascii=False),
    )


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
            "SELECT auto_event_loop, status FROM agents WHERE agent_id = ?",
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
    # AC-R29-1 class-sweep: wait_for_events is the sibling long-lived
    # authenticated channel. It re-auths at the gate on each call and is
    # bounded (≤300s), but mid-flight it only re-checked the
    # auto_event_loop toggle — a token terminated during an in-flight
    # long-poll would keep receiving event content for the rest of the
    # window. Reuse this per-tick DB recheck (run every ~2s) to stop the
    # wake loop the moment the agent is terminated, mirroring the SSE
    # pump's self-validation.
    if str(row["status"]) == "terminated":
        return False, f"agent '{agent_id}' terminated"
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
    # SECURITY (round-2): the cursor MUST advance monotonically. The
    # previous `agent_repo.update_field(..., "last_event_seen_at", ...)`
    # was last-writer-wins, so a slow concurrent `wait_for_events`
    # waiter writing an older cursor could rewind the high-water mark
    # and replay already-delivered events. `advance_event_cursor` does
    # `SET last_event_seen_at = MAX(last_event_seen_at, ?)` so a lower
    # value is a no-op. It keeps the same cache + EventBus side effects
    # `update_field` had for this field.
    try:
        from ..repositories import agent_repo
        ok = agent_repo.advance_event_cursor(agent_id, cursor_value)
        if not ok:
            logger.warning(
                "wait_for_events: failed to persist last_event_seen_at "
                "for %s (unknown agent or DB error)",
                agent_id,
            )
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(
            "wait_for_events: failed to persist last_event_seen_at "
            "for %s: %s",
            agent_id, e,
        )


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

        # BL-R10-2: key on ``updated_at`` (the transition-to-unassigned
        # time), NOT ``created_at``. A task orphaned by terminate/purge
        # (or a manual unassign/reassignment) keeps its ORIGINAL creation
        # time, so a ``created_at``-keyed catch-up past that time would
        # never surface a task that only BECAME available afterwards. The
        # event timestamp below rides ``updated_at`` too, so the caller's
        # cursor advances past the transition and the task is not
        # re-surfaced on the next catch-up. Freshly-created unassigned
        # tasks have ``created_at == updated_at``, so the create case is
        # unchanged.
        cursor.execute(
            """
            SELECT task_id, title, priority, required_capabilities,
                   updated_at
            FROM tasks
            WHERE assigned_to IS NULL
              AND updated_at > ?
            ORDER BY updated_at ASC
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
                "timestamp": trow["updated_at"],
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


def assemble_event_feed(
    agent_id: str,
    cursor: Optional[str],
    *,
    drain_queue: Optional[List[Dict[str, Any]]] = None,
) -> tuple[List[Dict[str, Any]], str]:
    """Single owner of the event-feed stream-merge pipeline.

    Every event-feed surface — both ``wait_for_events`` paths (fast +
    slow), ``fetch_events_since``, and the inbox resource — routes
    through here so the union / dedup / clamp / sort / cursor steps run
    in the ONE correct order exactly once. Copy-pasting this pipeline was
    the BL-R20 / BL-R21 fault line (and let the inbox resource silently
    diverge from ``wait_for_events`` by omitting the unassigned-task
    stream + the merged-boundary clamp).

    The three streams merged, in order:

      1. DB-backed events (direct/broadcast messages + assigned-task
         changes) via :func:`_collect_events_with_cap`, which also
         yields the message-truncation boundary ``msg_cap_ts``.
      2. Matching ``unassigned_task_appeared`` events via
         :func:`_collect_unassigned_task_events_for` (an UNBOUNDED
         query, invisible to the internal clamp inside stream 1).
      3. ``drain_queue`` — synthetic-queue items a ``wait_for_events``
         waiter already popped off its private queue. ``None`` for the
         pure-DB catch-up surfaces (``fetch_events_since``, inbox).

    Ordering invariant (do NOT reorder): dedup runs BEFORE the clamp so
    the surviving copy of each dual-sourced event carries its DB
    ``updated_at`` timestamp (not a later wall-clock synthetic instant),
    keeping the clamp / cursor anchored on the real DB transition time
    (BL-R31-2). The clamp then caps the MERGED batch — and therefore the
    ``max()`` cursor derived from it — to ``msg_cap_ts`` so a newer,
    unbounded stream-2/3 event can't drag the persisted cursor past the
    un-returned messages 501+ (BL-R21-1).

    Returns ``(events, next_cursor)`` where ``events`` is timestamp-ASC
    and ``next_cursor`` is ``max(timestamp)`` over the returned batch, or
    ``cursor or ""`` when the batch is empty (the caller's progress is
    preserved on an empty poll).
    """
    events, msg_cap_ts = _collect_events_with_cap(agent_id, cursor)
    events.extend(_collect_unassigned_task_events_for(agent_id, cursor))
    if drain_queue:
        events.extend(drain_queue)
    # BL-R31-2: collapse the DB re-query + synthetic-queue copies of the
    # same unassigned task to one delivery (keeping the DB updated_at
    # copy) BEFORE the clamp so the clamp/cursor anchor stays on the DB
    # transition time.
    events = _dedup_events(events)
    # BL-R21-1: cap the MERGED batch to the message-truncation boundary
    # so a newer unbounded task/synthetic event can't drag the persisted
    # cursor past the un-returned messages 501+.
    events = _cap_events_to_boundary(events, msg_cap_ts)
    events.sort(key=lambda e: e.get("timestamp") or "")
    if events:
        next_cursor = (
            max((e.get("timestamp") or "") for e in events) or (cursor or "")
        )
    else:
        next_cursor = cursor or ""
    return events, next_cursor


async def wait_for_events_tool_impl(
    arguments: Dict[str, Any],
    *,
    principal: Optional[Principal] = None,
) -> ToolResult:
    """Long-poll for new events for the calling agent.

    Wave 6 PR 2: migrated to the Principal + ToolResult contract.
    The pre-migration ``@requires("any")`` gate admitted any valid
    agent token; we keep that intent by requiring the resolved
    Principal to identify an agent (``principal.agent_id`` set).

    Returns immediately with any events newer than `since`; otherwise
    blocks until `signal_for(agent_id).set()` fires or
    `timeout_seconds` (default 60, max 300) elapses.

    Hardening (PR-2, partially reversed by PR-B / v5.0.24):
      * Reads `config_auto_event_loop_global` + `agents.auto_event_loop`
        on every call; returns ``stop_listening`` if either is OFF.
      * **Fan-out semantics** (PR-B): N concurrent ``wait_for_events``
        callers per agent are supported. Each call allocates its own
        synthetic-event queue via ``state.register_waiter(agent_id)``;
        the EventBus walks every queue on notify so all callers
        observe every event. DB-backed events (messages, task changes)
        are naturally fan-out because each waiter independently
        re-queries SQLite on wake. The previous "HTTP-409 analog" lock
        is gone — see ``docs/adr/0012-wait_for_events_fanout.md``.
      * Rechecks flags during long waits at 2s cadence so a toggle flip
        wakes the call within ~5s with ``stop_listening``.
      * Drains the waiter's private synthetic-event queue on every
        wake; never touches a peer's queue.
      * Persists the high-water cursor to `agents.last_event_seen_at`.
        Multiple waiters racing to write the same cursor is safe — the
        write is idempotent (max-timestamp converges) and SQLite
        serializes the row update under the write queue.
    """
    principal = _resolve_principal(arguments, principal)
    if principal is None or not principal.agent_id:
        return PermissionDenied(
            reason="Valid agent token required to long-poll events"
        )
    agent_id = principal.agent_id

    since = arguments.get("since")
    if since is not None and not isinstance(since, str):
        return Invalid(
            field="since",
            message="since must be an ISO-UTC timestamp string",
        )

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

    # PR-B fan-out: each call owns a private synthetic-event queue so
    # N concurrent waiters per agent each receive every notification.
    # Register on entry; unregister in the finally block on exit.
    waiter_queue = g.register_waiter(agent_id)
    try:
        # Flag gate: if either toggle is OFF, return stop_listening now.
        enabled, reason = _check_auto_event_loop_flags(agent_id)
        if not enabled:
            stop_evt = _stop_listening_event(reason or "auto_event_loop is OFF")
            # Drop anything that landed in our queue before the gate
            # check — agent is opted out, the events are stale.
            g.drain_waiter_queue(waiter_queue)
            return _envelope([stop_evt], since)

        # Fast path — combine DB backlog with synthetic events that
        # arrived between register_waiter() and this point. The single
        # feed owner runs union → dedup → clamp → sort → cursor; drain
        # the waiter's private synthetic queue as stream 3.
        events: List[Dict[str, Any]]
        events, cursor_value = assemble_event_feed(
            agent_id, since, drain_queue=g.drain_waiter_queue(waiter_queue)
        )
        if events:
            env = _envelope(events, since)
            if cursor_value:
                _write_last_event_seen_at(agent_id, cursor_value)
            return env

        # Slow path — block on the per-call queue. The EventBus pushes
        # either a real synthetic event (e.g. ``unassigned_task_appeared``)
        # OR a ``WAITER_WAKE_SENTINEL`` (for DB-backed events) onto the
        # queue, which releases ``queue.get()`` so we re-query the DB.
        # Wake-up is per-waiter — no shared ``asyncio.Event.clear()``
        # race because each waiter owns its queue.
        #
        # We keep the shared ``signal_for(agent_id)`` Event in sync as
        # a secondary wake-edge: ``wake_for_flag_recheck`` and any other
        # legacy callers that fire it directly will still release us
        # because the slice timeout is bounded — the flag re-check
        # happens at most ``_FLAG_RECHECK_INTERVAL_SECONDS`` late.
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                return _envelope([], since)
            slice_timeout = min(_FLAG_RECHECK_INTERVAL_SECONDS, remaining)
            try:
                # Block until either (a) the EventBus puts something on
                # our queue or (b) the slice expires. The item returned
                # by ``.get()`` is the wake trigger — keep it so it
                # joins the rest of the drain below. (Otherwise the
                # event we just consumed gets dropped on the floor
                # because ``drain_waiter_queue`` only sees what's
                # still on the queue.)
                first_item = await asyncio.wait_for(
                    waiter_queue.get(), timeout=slice_timeout
                )
                # Woken — recheck flags first; the wake may have come
                # from `wake_for_flag_recheck` (toggle flip), in which
                # case the new flag state requires stop_listening.
                enabled, reason = _check_auto_event_loop_flags(agent_id)
                if not enabled:
                    stop_evt = _stop_listening_event(
                        reason or "auto_event_loop is OFF"
                    )
                    g.drain_waiter_queue(waiter_queue)
                    return _envelope([stop_evt], since)
                # Drain everything that accumulated — our own private
                # synthetic queue (plus the first_item we already
                # popped to release ``queue.get()``) — and hand it to the
                # single feed owner as stream 3 alongside a fresh re-query
                # of DB-backed events.
                drained: List[Dict[str, Any]] = []
                if first_item is not g.WAITER_WAKE_SENTINEL and first_item is not None:
                    drained.append(first_item)
                drained.extend(g.drain_waiter_queue(waiter_queue))
                events, cursor_value = assemble_event_feed(
                    agent_id, since, drain_queue=drained
                )
                if events:
                    env = _envelope(events, since)
                    if cursor_value:
                        _write_last_event_seen_at(agent_id, cursor_value)
                    return env
                # Wake with no events for this caller's ``since`` cursor —
                # treat as a spurious wake (e.g. flag toggle that flips
                # back) and loop for another slice.
            except asyncio.TimeoutError:
                # Slice expired without a wake — recheck flags so an
                # operator who flips a toggle during a long wait sees
                # it within `_FLAG_RECHECK_INTERVAL_SECONDS`.
                enabled, reason = _check_auto_event_loop_flags(agent_id)
                if not enabled:
                    stop_evt = _stop_listening_event(
                        reason or "auto_event_loop is OFF"
                    )
                    g.drain_waiter_queue(waiter_queue)
                    return _envelope([stop_evt], since)
                # Loop and wait another slice (or until deadline).
                continue
    finally:
        g.unregister_waiter(agent_id, waiter_queue)


async def fetch_events_since_tool_impl(
    arguments: Dict[str, Any],
    *,
    principal: Optional[Principal] = None,
) -> ToolResult:
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

    Wave 6 PR 2: migrated to the Principal + ToolResult contract.
    Same agent-identity gate as :func:`wait_for_events_tool_impl`.
    """
    principal = _resolve_principal(arguments, principal)
    if principal is None or not principal.agent_id:
        return PermissionDenied(
            reason="Valid agent token required to fetch events"
        )
    agent_id = principal.agent_id

    cursor = arguments.get("cursor")
    if cursor is not None and not isinstance(cursor, str):
        return Invalid(
            field="cursor",
            message="cursor must be an ISO-UTC timestamp string or null",
        )

    if cursor is None:
        cursor = _read_last_event_seen_at(agent_id)

    # Gather everything since the cursor via the single feed owner: DB-
    # backed events + matching unassigned tasks. We deliberately do NOT
    # drain the in-memory queue here (``drain_queue`` stays ``None``) —
    # fetch_events_since is the "fresh catch-up" path and the queue
    # contents are only meaningful in the context of a wait_for_events
    # session that established expectations about ordering.
    events, new_cursor = assemble_event_feed(agent_id, cursor)
    if events:
        _write_last_event_seen_at(agent_id, new_cursor)

    body = {"events": events, "cursor": new_cursor}
    return Ok(
        data=body,
        message=json.dumps(body, ensure_ascii=False),
    )


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
                    "description": (
                        "Vestigial since Wave 7 (coordinator transition). "
                        "Every message is stored in the DB and surfaced via "
                        "wait_for_events / get_agent_messages — agent-mcp "
                        "no longer pushes to a tmux session. Accepted for "
                        "back-compat; the value is ignored."
                    ),
                    "enum": ["tmux", "store", "both"],
                    "default": "store"
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
        visibility="operator",
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