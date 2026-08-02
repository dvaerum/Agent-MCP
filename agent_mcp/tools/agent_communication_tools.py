# Agent-MCP/agent_mcp/tools/agent_communication_tools.py
import asyncio
import json
import datetime
import re
import secrets
import sqlite3
from typing import List, Dict, Any, Optional
import os

from .registry import register_tool, request_auth_token
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
from ..repositories.message_repository import (
    ParentMessageNotFound,
    message_subject_view,
)
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
# Principal from the ``request_auth_token`` ContextVar (a per-agent
# bearer) so those callers keep working without an explicit kwarg.


def _resolve_principal(
    arguments: Dict[str, Any],
    principal: Optional[Principal],
) -> Optional[Principal]:
    """Return the effective Principal for this call.

    Order:

    1. ``principal`` kwarg (the dispatcher path).
    2. the bearer on the ``request_auth_token`` ContextVar resolved to
       an active agent row (covers direct-impl test calls that make
       the bearer visible on the ContextVar, e.g. via
       ``tests.harness.with_bearer`` — the same seam the HTTP
       middleware and REST dispatch helper set).

    Returns ``None`` if no identity can be derived; the calling tool
    surfaces that as :class:`PermissionDenied` per the new contract.

    token-retirement PR 2 (Phase B): the fallback sources the bearer
    from the ContextVar, NOT the legacy self-auth token arg — nothing
    reads a token argument for identity here. ``arguments`` stays in
    the signature (callers still pass it) but is no longer read for the
    token.

    arch-B: the ContextVar fallback delegates to the shared
    :func:`agent_mcp.core.principal_builder.build_agent_bearer_principal`
    so a synthesized identity resolves its capabilities through the exact
    same path the middleware seam uses.
    """
    if principal is not None:
        return principal

    token = request_auth_token.get()
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

    arch-r5 #7: one-line delegate to
    :meth:`AgentRepository.active_agent_ids` — the single owner of
    "which agents are active". That owner projects the SAME
    ``state.active_agents`` cache the ``/mcp`` auth gate
    (``main_app._bearer_is_active``) and ``view_status`` read, so this
    predicate can never drift from what a bearer's liveness check
    reports. (Formerly routed through ``agent_repo.list_active()`` — a
    fresh DB query — which is a *different*, DB-authoritative
    projection now reserved for reconciliation/warm/boot; see that
    method's docstring for why the two must stay separate.)
    """
    from ..repositories import agent_repo

    return agent_repo.active_agent_ids()


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
        return False, (
            "you cannot message yourself. To record your own "
            "progress/context on a task, use add_task_note(task_id=..., "
            "text=...) instead."
        )

    # Admin agent can always be contacted. Match the canonical "admin"
    # identity EXACTLY — a startswith wildcard would let a worker message
    # any agent whose id merely begins with "admin" (e.g.
    # "admin-impersonator"), bypassing the worker→worker default-deny.
    if recipient_id.lower() == "admin":
        return True, "Admin agent always contactable"

    # Worker→worker: gated by per-project toggle (issue K).
    # Default-deny preserves upstream behavior; admin opts in via
    # project_context[config_allow_worker_to_worker].
    if not _access._get_config_bool("config_allow_worker_to_worker"):
        return False, "Worker-to-worker messaging disabled by policy"

    # Toggle is on. Permit when both sides are currently active agents.
    active_ids = _agents_active_by_id()
    if sender_id in active_ids and recipient_id in active_ids:
        return True, "Both agents are active"

    # SECURITY: collapse offline / terminated / nonexistent recipients into
    # ONE clause — never an existence oracle. A worker cannot tell whether
    # `recipient_id` is a real-but-offline agent, a terminated one, or was
    # never registered: all three miss the active-set check identically and
    # yield this same denial (which also never echoes the probed id back).
    return False, (
        "the recipient is not a currently-active agent (it may be "
        "offline, terminated, or unknown). Only messages between two "
        "currently-active agents are delivered."
    )


def check_send_message_permission(
    principal: Principal,
    *,
    recipient_id: str,
    message_content: str,
    message_type: str,
) -> Optional[ToolResult]:
    """Shared authorization gate for an outgoing agent message.

    Returns ``None`` when the send is permitted, or the denial
    :data:`ToolResult` (``PermissionDenied`` / ``Invalid``) when a gate
    rejects it.

    OBS6 (one-enforcement-path parity): BOTH the MCP ``send_agent_message``
    tool below AND the dashboard REST create handler
    (:func:`agent_mcp.app.routers.messages.create_message_api_route`) call
    this, so the same gate set runs on ONE code path regardless of surface.
    Before OBS6 the REST handler wrote the message ORM-direct after only
    the operator-session gate and enforced none of these — a future
    router/dep change that let a lower-tier principal reach the handler
    could silently reopen the gap. This mirrors the memories #483 fix
    (routing the REST memory writes through the gated project_context
    tool). Gates, in order:

      * worker-to-worker toggle (``config_allow_worker_to_worker``) for
        non-operator ``agent_bearer`` callers, and an outright deny for a
        non-operator, non-``agent_bearer`` principal (e.g. a forwarding
        viewer that reaches the REST handler);
      * the 4000-character message cap;
      * the ``stop_command`` admin-only gate;
      * the per-pair :func:`_can_agents_communicate` delivery rules.

    Callers validate ``recipient_id`` / ``message_content`` presence
    themselves before calling this — the field-level ``Invalid`` wording
    differs by surface (``message`` vs ``message_content``).
    """
    is_admin = _is_operator_tier(principal)
    sender_id = _sender_label(principal)

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
            "config_allow_worker_to_worker"
        ):
            return PermissionDenied(
                reason=(
                    "Communication denied: direct agent-to-agent "
                    "messaging is disabled for workers by the "
                    "config_allow_worker_to_worker policy (this also "
                    "blocks messaging admins). To escalate to a "
                    "human/admin, use request_assistance(task_id=<your "
                    "task>, description=...), or ask an admin to enable "
                    "worker messaging in dashboard Settings."
                )
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
        return PermissionDenied(
            reason=(
                "message_type 'stop_command' is admin-only. If you need "
                "another agent to stop, send a normal 'text' message "
                "explaining the request, or use request_assistance to "
                "escalate to an admin."
            )
        )

    # Per-pair delivery rules — admin bypass + worker-to-worker active
    # set + admin recipient label.
    can_communicate, reason = _can_agents_communicate(
        sender_id, recipient_id, is_admin
    )
    if not can_communicate:
        return PermissionDenied(reason=f"Communication denied: {reason}")

    return None


# Feature 2 (reply-nudge). A subject that begins with "re:" (any case,
# optional surrounding whitespace, e.g. "RE:", " Re : ") is how agents
# spell a reply when they haven't discovered parent_message_id. Detect it
# to append an advisory hint — RE: only; this product has no forward
# concept.
_RE_SUBJECT_RE = re.compile(r"^\s*re\s*:", re.IGNORECASE)

_REPLY_HINT_TEXT = (
    "This looks like a reply — to thread it correctly, use the reply "
    "function by passing `parent_message_id` (the message you're replying "
    "to) instead of putting 'RE:' in the subject."
)


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

    # OBS6: the five authorization gates (worker-to-worker toggle,
    # non-agent_bearer deny, 4000-char cap, stop_command admin gate, and
    # the per-pair _can_agents_communicate rules) now live once in
    # ``check_send_message_permission`` so the REST create handler
    # enforces the identical set — one enforcement path.
    denial = check_send_message_permission(
        principal,
        recipient_id=recipient_id,
        message_content=message_content,
        message_type=message_type,
    )
    if denial is not None:
        return denial

    # Create message data
    message_id = _generate_message_id()
    timestamp = datetime.datetime.now().isoformat()

    # Phase 1 (null-subject placeholder): compute the effective subject.
    # Three branches:
    #   1. Reply (parent_message_id set) → always NULL. The dashboard
    #      surfaces the root's subject as the thread label; replies
    #      don't carry their own.
    #   2. Explicit subject supplied → persist verbatim (non-null).
    #   3. Root w/o explicit subject → store NULL. NULL is the marker
    #      for "no real subject was ever set". We do NOT call the
    #      (synchronous, RAM-hungry) `suggest_subject` helper on the send
    #      path anymore, and we do NOT persist a truncated-body string:
    #      the read paths compute a 50-char preview on demand via
    #      `message_subject_view` and flag it `subject_is_placeholder`.
    #      The `suggest_subject` helper + /api/messages/suggest-subject
    #      endpoint survive for the dashboard's manual button and the
    #      upcoming Phase 2 backfill.
    effective_subject: Optional[str]
    if parent_message_id:
        effective_subject = None
    elif explicit_subject:
        effective_subject = explicit_subject
    else:
        effective_subject = None

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

        # Phase 1: surface the DISPLAY subject + placeholder flag so the
        # sending agent learns whether a real subject was stored or a
        # computed body preview will stand in. Replies carry no subject.
        if parent_message_id:
            display_subject: Optional[str] = None
            subject_is_placeholder = False
        else:
            display_subject, subject_is_placeholder = message_subject_view(
                effective_subject, message_content
            )

        data = {
            "message_id": message_id,
            "sender": sender_id,
            "recipient_id": recipient_id,
            "message_type": message_type,
            "priority": priority,
            "delivery_status": delivery_status,
            "subject": display_subject,
            "subject_is_placeholder": subject_is_placeholder,
            "parent_message_id": parent_message_id,
        }

        # Reply-nudge (advisory only). Some agents type "RE:" into the
        # subject instead of threading via parent_message_id. When an
        # EXPLICIT subject looks like a reply (leading "re:",
        # case-/whitespace-insensitive) AND this send is NOT already a
        # reply (no parent_message_id), append a gentle hint pointing them
        # at the reply/threading function. RE: only — this product has no
        # forward/"FW:" concept. The send already succeeded; the hint
        # neither blocks nor rewrites it.
        if (
            not parent_message_id
            and isinstance(explicit_subject, str)
            and _RE_SUBJECT_RE.match(explicit_subject)
        ):
            data["reply_hint"] = _REPLY_HINT_TEXT
            response_text += " " + _REPLY_HINT_TEXT

        return Ok(
            data=data,
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
        # INSERT and fired no effects. Return a typed NotFound naming the
        # agent resource. This path is admin/operator-reachable ONLY — a
        # worker's send is gated by _can_agents_communicate's
        # active-recipient check (which collapses unknown/offline/
        # terminated into one non-oracle denial) BEFORE the repo runs, so
        # a worker never reaches here and no recipient id is leaked in a
        # worker-facing denial.
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
    operator caller whose bearer is resolved from the ContextVar by
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
            # v5.0.22 / Phase 1: surface subject (root) or reply-marker
            # (reply). sqlite3.Row supports `in` via .keys(); guard for
            # legacy callers that may not have migrated yet.
            row_keys = set(msg.keys()) if hasattr(msg, "keys") else set()
            raw_subj = msg["subject"] if "subject" in row_keys else None
            parent_id = (
                msg["parent_message_id"]
                if "parent_message_id" in row_keys
                else None
            )
            # Phase 1: a NULL stored subject on a ROOT message means no
            # real subject was ever set — compute a 50-char body preview
            # (never stored) and flag it a placeholder so the agent knows.
            # Replies stay subject-less (thread-labelled instead).
            if parent_id:
                display_subject = None
                subject_is_placeholder = False
            else:
                display_subject, subject_is_placeholder = (
                    message_subject_view(raw_subj, msg["message_content"])
                )
            if display_subject and not subject_is_placeholder:
                response_lines.append(f"   Subject: {display_subject}")
            elif display_subject and subject_is_placeholder:
                # Auto placeholder preview — mark it so a reader can tell
                # it apart from a sender-chosen subject.
                response_lines.append(f"   Subject (auto): {display_subject}")
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
                "subject": display_subject,
                "subject_is_placeholder": subject_is_placeholder,
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
    # v5.0.22 / Phase 1: broadcasts can carry an explicit subject. Each
    # fan-out send is a root message, so the per-recipient
    # send_agent_message call computes the subject the same way (verbatim
    # if set; NULL otherwise — the read paths render a body preview on
    # demand, no synchronous model call).
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

# Event-loop long-hold ceiling for a heartbeat client whose strategy has
# NO per-connection cap (OpenCode / unknown-with-progressToken). Without a
# cap the connection would hold forever; PR2's idle-stop window becomes the
# real terminator. Until then this generous 24h ceiling bounds the hold so
# the loop always terminates cleanly (empty envelope → reconnect).
_UNCAPPED_HOLD_CEILING_SECONDS = 24 * 60 * 60

# Reap a parked hold after this many CONSECUTIVE failed heartbeats. A
# failed progress send means the heartbeat-capable client's transport is
# gone (dropped socket / proxy restart / tailnet blip); nothing on this
# connection can reach it again. The supersede path only reaps on a
# *reconnect*, so a client that simply vanished would otherwise park until
# the deadline — holding the slot and lingering "online" in presence. A
# small threshold (not 1) tolerates a one-off transient send failure
# without ending a still-live hold.
_MAX_HEARTBEAT_MISSES = 2


def _evlog(msg: str, *args) -> None:
    """Event-loop diagnostics. Emits at WARNING (so journald's WARNING-level
    stderr handler captures it) when event-loop debug is enabled — the
    ``config_debug_eventloop`` project setting (toggle in the Settings
    dashboard), falling back to the ``AGENT_MCP_EVENTLOOP_DEBUG`` env var;
    otherwise DEBUG (invisible). The check is TTL-cached so this hot path
    doesn't hit the DB on every line, and a dashboard toggle takes effect
    within a few seconds — no restart needed.

    Traces the full picture the operator asked for: which hold strategy each
    client gets, whether a connection PARKS-and-listens vs RE-REQUESTS every
    slice, whether heartbeats go out, and the events in/out per poll.
    """
    from ..core.debug_flags import debug_enabled

    if debug_enabled("config_debug_eventloop", "AGENT_MCP_EVENTLOOP_DEBUG"):
        logger.warning("EVENTLOOP " + msg, *args)
    else:
        logger.debug("EVENTLOOP " + msg, *args)


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


# Max seconds a skinny message event is HELD waiting for the async AI
# subject backfill to title it (only when subject-gen is ON). Past this,
# the event fires with the 50-char preview so a stalled/failed backfill
# can never strand a message out of its recipient's event stream.
_TITLE_HOLD_MAX_SECONDS = 120


def _within_title_hold(msg_ts: str, now_iso: str) -> bool:
    """True while an untitled root message is still inside the title-hold
    window (its skinny event is held for the AI subject backfill). Any
    parse failure returns False → fire now rather than risk stranding."""
    try:
        age = (
            datetime.datetime.fromisoformat(now_iso)
            - datetime.datetime.fromisoformat(msg_ts)
        ).total_seconds()
    except (ValueError, TypeError):
        return False
    return 0 <= age < _TITLE_HOLD_MAX_SECONDS


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
    # Skinny message events: a `message`/`broadcast` event is a POINTER,
    # not a dump. It carries the subject/title + is_reply + sender so the
    # agent can DECIDE, then call get_agent_messages to READ the body —
    # which is what marks the message read. The full `message_content` is
    # deliberately omitted (events signal "something changed", they don't
    # flood the reader). `message_repo.query` already computes the display
    # `subject` (real, or a 50-char body preview flagged
    # `subject_is_placeholder`) and None-for-replies.
    _gen_on = bool(os.environ.get("AGENT_MCP_SUBJECT_MODEL", "").strip())
    _now_iso = datetime.datetime.now().isoformat()
    _last_emitted_ts: Optional[str] = None
    for row in msg_rows:
        # repo `since` is inclusive (`>=`); the legacy SQL was strict
        # `>`. Re-apply the strict filter here to preserve the old
        # behaviour — a message exactly at `since_iso` shouldn't fire
        # again on the next long-poll wake.
        ts = row.get("timestamp") or ""
        if ts <= since_iso:
            continue
        is_reply = row.get("parent_message_id") is not None
        # Title gate (roots only; replies are threaded and fire at once).
        # An untitled root fires immediately with the preview when AI
        # subject-gen is OFF; when it is ON, HOLD the event so the pointer
        # carries a real title once the async backfill sets it — reusing
        # the truncation-cap boundary so the cursor can't advance past the
        # held row (re-queried next poll). Bounded by _TITLE_HOLD_MAX so a
        # stalled backfill can never strand the message.
        if (
            not is_reply
            and bool(row.get("subject_is_placeholder"))
            and _gen_on
            and _within_title_hold(ts, _now_iso)
        ):
            messages_truncated = True
            msg_cap_ts = _last_emitted_ts or since_iso
            break
        data = {
            "message_id": row["message_id"],
            "sender_id": row["sender_id"],
            "subject": row.get("subject"),
            "is_reply": is_reply,
            "priority": row["priority"],
            "timestamp": ts,
        }
        evt_type = (
            "broadcast"
            if (row["message_type"] or "") in _BROADCAST_MESSAGE_TYPES
            else "message"
        )
        events.append({
            "type": evt_type,
            "timestamp": ts,
            "data": data,
        })
        _last_emitted_ts = ts

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
            # Skinny task event: a pointer, not a dump. task_id + title +
            # status (+ priority for triage) is enough to decide; the
            # agent calls view_tasks to read the description and interact.
            # The full description / relationships are deliberately omitted.
            data = {
                "task_id": row["task_id"],
                "title": row["title"],
                "status": row["status"],
                "priority": row["priority"],
                "updated_at": row["updated_at"],
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
_DEDUP_EVENT_TYPES = frozenset(
    {"unassigned_task_appeared", "agent_profile_updated"}
)


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
    events: List[Dict[str, Any]], since: Optional[str],
    *, profile_review: Optional[Dict[str, Any]] = None,
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
    # Agent self-service profiles (PR3): attach the first-connect/overdue
    # profile-review section when the builder produced one. Rides the loop
    # response, never the system prompt.
    if profile_review is not None:
        payload["profile_review"] = profile_review
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
        "config_auto_event_loop_global",
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
        # The per-agent flag only goes OFF via an operator "Disconnect"
        # (or the equivalent edit) — so the reason names WHY (operator
        # paused you) and WHEN (may resume later), which the agent relays
        # to the human before exiting the loop.
        return False, (
            f"Monitoring paused by operator for agent '{agent_id}'. "
            "You have been disconnected for now; you may be told to "
            "resume later. Exit the event loop and wait for human input."
        )
    return True, None


def _idle_stop_seconds_remaining(agent_id: str) -> Optional[float]:
    """Seconds until this agent's event-loop idle-stop fires, or ``None``
    when idle-stop is disabled (``config_event_idle_stop_seconds == 0``).

    Reads the operator's idle-stop window and the agent's
    ``last_activity_at`` marker. On first use (marker NULL) it SEEDS the
    marker to "now" and grants a full window — so a brand-new agent starts
    its idle clock when it begins listening rather than counting as
    instantly idle. The marker is reset to "now" on every real event
    (see the ``_write_last_activity_at`` call sites in
    ``wait_for_events_tool_impl``), so this measures time-since-last-real-
    event across reconnects. A return <= 0 means the window is already
    exceeded.
    """
    window = _access._get_config_int("config_event_idle_stop_seconds")
    if window <= 0:
        return None  # 0 = infinite / never stop
    now = datetime.datetime.now()
    last = _read_last_activity_at(agent_id)
    if not last:
        _write_last_activity_at(agent_id, now.isoformat())
        return float(window)
    try:
        last_dt = datetime.datetime.fromisoformat(last)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        _write_last_activity_at(agent_id, now.isoformat())
        return float(window)
    return float(window) - (now - last_dt).total_seconds()


def _stop_listening_event(reason: str) -> Dict[str, Any]:
    """Build the canonical ``stop_listening`` event dict."""
    return {
        "type": "stop_listening",
        "ref_id": None,
        "timestamp": datetime.datetime.now().isoformat(),
        "payload": {"reason": reason},
    }


# Newest-wins: message returned to the OLDER wait_for_events call when a
# NEWER one for the same agent supersedes it. Deliberately NOT a
# stop_listening event — the agent must NOT exit the loop (its newer
# connection is carrying it); this only closes the stale duplicate.
_SUPERSEDED_MESSAGE = (
    "This wait_for_events connection was superseded by a newer one for the "
    "same agent, so this (duplicate) call is being closed — you should have "
    "exactly ONE event-loop connection. Do NOT open a second wait_for_events "
    "while one is already parked, and do NOT background it: it is meant to "
    "stay in the foreground as your idle wait for new work. Your newer "
    "connection is still live and carrying the loop; do nothing here."
)


def _superseded_event() -> Dict[str, Any]:
    """Build the ``connection_superseded`` event (returned to a waiter that
    a newer connection replaced). Distinct from stop_listening so the agent
    keeps the loop running on its newer connection."""
    return {
        "type": "connection_superseded",
        "ref_id": None,
        "timestamp": datetime.datetime.now().isoformat(),
        "payload": {"reason": _SUPERSEDED_MESSAGE},
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


def _read_last_activity_at(agent_id: str) -> Optional[str]:
    """Read the persisted idle-stop activity marker, or None if unset."""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT last_activity_at FROM agents WHERE agent_id = ?",
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
    return row["last_activity_at"]


def _write_last_activity_at(agent_id: str, when_iso: str) -> None:
    """Persist ``agents.last_activity_at`` (event-loop idle-stop marker).

    Best-effort: a DB failure here must not fail the tool call. Unlike the
    event cursor this is a wall-clock "now" that always advances forward on
    a real event (or the first-listen seed), so a plain field write is
    correct — no monotonic MAX needed.
    """
    if not when_iso:
        return
    try:
        from ..repositories import agent_repo

        if agent_repo.update_field(agent_id, "last_activity_at", when_iso) is None:
            logger.warning(
                "wait_for_events: failed to persist last_activity_at "
                "for %s (unknown agent or DB error)",
                agent_id,
            )
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(
            "wait_for_events: failed to persist last_activity_at for %s: %s",
            agent_id, e,
        )


def _collect_unassigned_task_events_for(
    agent_id: str, since: Optional[str],
) -> List[Dict[str, Any]]:
    """Find unassigned tasks that transitioned after ``since``.

    Reused by both `wait_for_events_tool_impl` (on wake) and
    `fetch_events_since_tool_impl`. Produces the same skinny payload
    that `notify_unassigned_task_appeared` pushes to the in-memory
    queue, so a worker that misses the push event still picks up the
    same shape on its next catch-up.

    Every unassigned task surfaces to every agent — the structured
    capability-tag routing (``req ⊆ caps``) was retired in PR5 (it was
    already a no-op: an empty required set matched everyone). The
    ``agent_id`` gate below is kept only so a catch-up for an unknown /
    tombstoned agent returns nothing.
    """
    since_iso = since if since else "0000-01-01T00:00:00"
    events: List[Dict[str, Any]] = []
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT 1 FROM agents WHERE agent_id = ?",
            (agent_id,),
        )
        if cursor.fetchone() is None:
            return []

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
            SELECT task_id, title, priority, updated_at
            FROM tasks
            WHERE assigned_to IS NULL
              AND updated_at > ?
            ORDER BY updated_at ASC
            """,
            (since_iso,),
        )
        for trow in cursor.fetchall():
            events.append({
                "type": "unassigned_task_appeared",
                "ref_id": trow["task_id"],
                "timestamp": trow["updated_at"],
                "payload": {
                    "task_id": trow["task_id"],
                    "title": trow["title"],
                    "priority": trow["priority"],
                },
            })
    finally:
        if conn:
            conn.close()
    return events


def _collect_agent_profile_events_for(
    agent_id: str, since: Optional[str],
) -> List[Dict[str, Any]]:
    """Find peer profile changes newer than ``since`` (agent self-service
    profiles, plan §8 PR2).

    Derived from the ``agents`` table on catch-up — the table IS the log,
    so this is disconnect-robust: an agent offline across a peer's edit
    replays it on reconnect (``profile_updated_at > cursor``). Emits one
    ``agent_profile_updated`` event per changed peer.

    Exclusions (all in SQL so catch-up and the in-memory push agree). The
    broadcast excludes the **editor**, not the subject (decision 3):

    * ``profile_updated_by != :self`` — the recipient never sees a change
      IT authored (its own self-edit, or a manager viewing its own
      curation of a worker). This is the editor exclusion.
    * ``NOT (agent_id = :self AND profile_updated_by IS NULL)`` — a
      recipient does not get its OWN seed (the manager charter, which has
      a NULL editor) echoed back to itself. A seed for SOMEONE ELSE (a
      newly-registered manager's charter) still surfaces — a new manager
      is a roster change worth learning about.
    * tombstone / terminated / system rows are never a profile source.

    Consequence (verifies the locked cases): a manager editing a worker
    (``updated_by = manager``) DOES reach the worker subject
    (``updated_by != worker``) but not the manager editor; a self-edit
    reaches every peer but not the author.
    """
    since_iso = since if since else "0000-01-01T00:00:00"
    events: List[Dict[str, Any]] = []
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT agent_id, agent_role, profile, profile_updated_at,
                   profile_updated_by
            FROM agents
            WHERE profile_updated_at IS NOT NULL
              AND profile_updated_at > ?
              AND (profile_updated_by IS NULL OR profile_updated_by != ?)
              AND NOT (agent_id = ? AND profile_updated_by IS NULL)
              AND status NOT IN ('tombstone', 'terminated', 'system')
            ORDER BY profile_updated_at ASC
            """,
            (since_iso, agent_id, agent_id),
        )
        for row in cursor.fetchall():
            events.append({
                "type": "agent_profile_updated",
                "ref_id": row["agent_id"],
                "timestamp": row["profile_updated_at"],
                "data": {
                    "agent_id": row["agent_id"],
                    "agent_role": row["agent_role"],
                    "profile": row["profile"],
                    "updated_by": row["profile_updated_by"],
                },
            })
    finally:
        if conn:
            conn.close()
    return events


def notify_agent_profile_updated(
    subject_id: str, editor_id: Optional[str],
) -> None:
    """Push an ``agent_profile_updated`` event to every live agent except
    the editor, for low latency (agent self-service profiles, plan §8 PR2).

    Best-effort in-memory fan-out — the ``agents`` table is the source of
    truth, so a dropped push is replayed on the recipient's next
    ``fetch_events_since`` / ``wait_for_events`` catch-up via
    :func:`_collect_agent_profile_events_for`. Wrapped in a broad
    try/except so a notification side effect can never poison the source
    write (the profile is already committed by the time we're called).

    The event carries the subject's freshly-committed profile row and
    rides ``profile_updated_at`` as its timestamp so the deduped copy
    (DB re-query vs this push) anchors the cursor on the real DB
    transition time (matches the ``unassigned_task_appeared`` contract).
    """
    try:
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT agent_id, agent_role, profile, profile_updated_at, "
                "profile_updated_by FROM agents WHERE agent_id = ?",
                (subject_id,),
            )
            subj = cur.fetchone()
            if subj is None:
                return
            # arch: function-level import avoids the state<->comm module
            # cycle; LIVE_AGENT_SQL excludes terminated + tombstone.
            from ..repositories.agent_repository import LIVE_AGENT_SQL

            cur.execute(f"SELECT agent_id FROM agents WHERE {LIVE_AGENT_SQL}")
            recipients = [r["agent_id"] for r in cur.fetchall()]
        finally:
            conn.close()

        event = {
            "type": "agent_profile_updated",
            "ref_id": subj["agent_id"],
            "timestamp": subj["profile_updated_at"],
            "data": {
                "agent_id": subj["agent_id"],
                "agent_role": subj["agent_role"],
                "profile": subj["profile"],
                "updated_by": subj["profile_updated_by"],
            },
        }
        for rid in recipients:
            # Exclude the EDITOR (not the subject): a self-edit's editor
            # is the subject, so it's excluded; a manager's curation
            # excludes the manager but reaches the worker subject.
            if rid == editor_id:
                continue
            g.dispatch_synthetic_event(rid, event)
    except Exception:  # pragma: no cover - defensive; catch-up is truth
        pass


# Priority-aware feed ordering (plan §11). The feed historically sorted by
# timestamp only; a poke's "highest priority" needs urgent events to sort
# AHEAD of ordinary ones. Rank is a STABLE secondary key (priority, then
# timestamp) so ordinary same-priority events keep their timestamp order.
_PRIORITY_RANK: Dict[str, int] = {
    "urgent": 0,
    "high": 1,
    "normal": 2,
    "low": 3,
}


def _event_priority_rank(event: Dict[str, Any]) -> int:
    """Rank an event by priority (lower sorts first). Reads the top-level
    ``priority`` (directive events) then ``data.priority`` (messages),
    defaulting to ``normal``."""
    prio = event.get("priority")
    if prio is None:
        prio = (event.get("data") or {}).get("priority")
    return _PRIORITY_RANK.get(prio or "normal", _PRIORITY_RANK["normal"])


def _sort_events_priority_then_time(
    events: List[Dict[str, Any]],
) -> None:
    """In-place stable sort: priority ASC-rank (urgent first), then
    timestamp ASC."""
    events.sort(
        key=lambda e: (_event_priority_rank(e), e.get("timestamp") or "")
    )


# ---------------------------------------------------------------------------
# Scheduled directives (event-loop scheduler, plan §4) — wait-loop-native
# firing. The `scheduled_directive` row is pure state (`next_due_at`); this
# tool's slice loop is the sole driver (no background sweeper). When a
# schedule is due AND the agent is checking in, the collector emits a
# `directive` event, bumps run_count, and resets next_due = now + interval
# (interval-reset-from-delivery). Reads back the soonest due time so the
# idle hold can wake exactly at next_due.
# ---------------------------------------------------------------------------


def _collect_scheduled_directive_events_for(
    agent_id: str,
) -> List[Dict[str, Any]]:
    """Fire every due scheduled directive for ``agent_id`` and return the
    ``directive`` events.

    Opens its own short-lived connection + transaction: the collector
    MUTATES (bumps run_count, resets next_due, flips terminal schedules to
    completed) so it must commit. Called only from the check-in paths
    (``wait_for_events`` / ``fetch_events_since``) — NOT from the passive
    inbox resource — via ``assemble_event_feed(fire_scheduled=True)``.
    Best-effort: a DB failure yields no events (the schedule fires on the
    next check-in) rather than failing the whole poll.
    """
    conn = None
    try:
        from ..repositories import scheduled_directive_repository as _sched
        now_iso = datetime.datetime.now().isoformat()
        conn = get_db_connection()
        cursor = conn.cursor()
        events = _sched.collect_due_and_fire(
            agent_id, now_iso, connection=cursor
        )
        conn.commit()
        return events
    except Exception as e:  # pragma: no cover - defensive; fires next time
        logger.warning(
            "wait_for_events: scheduled-directive fire failed for %s: %s",
            agent_id, e,
        )
        return []
    finally:
        if conn:
            conn.close()


def _collect_pending_pokes_for(agent_id: str) -> List[Dict[str, Any]]:
    """Collect + mark-delivered every undelivered operator/admin poke for
    ``agent_id`` and return the ``directive`` events (source="poke").

    Mutates ``pending_directive.delivered_at`` (own connection + commit) so
    each poke fires exactly once. Called only from the check-in paths via
    ``assemble_event_feed(fire_scheduled=True)``. Best-effort: a DB failure
    yields no events (the poke waits for the next check-in) rather than
    failing the whole poll.
    """
    conn = None
    try:
        from ..repositories import pending_directive_repository as _poke
        now_iso = datetime.datetime.now().isoformat()
        conn = get_db_connection()
        cursor = conn.cursor()
        events = _poke.collect_undelivered(
            agent_id, connection=cursor, now_iso=now_iso
        )
        conn.commit()
        return events
    except Exception as e:  # pragma: no cover - defensive; retries next time
        logger.warning(
            "wait_for_events: pending-poke collection failed for %s: %s",
            agent_id, e,
        )
        return []
    finally:
        if conn:
            conn.close()


def _soonest_schedule_due_at(agent_id: str, now_iso: str) -> Optional[str]:
    """Soonest ``next_due_at`` over the agent's fireable schedules, or
    ``None``. Used to bound the idle hold's slice so it wakes at next_due.
    """
    conn = None
    try:
        from ..repositories import scheduled_directive_repository as _sched
        conn = get_db_connection()
        return _sched.soonest_due_at(
            agent_id, now_iso, connection=conn.cursor()
        )
    except Exception:  # pragma: no cover - defensive
        return None
    finally:
        if conn:
            conn.close()


def _agent_has_active_schedule(agent_id: str, now_iso: str) -> bool:
    """True iff the agent has an enabled, in-window schedule.

    Decision 9: an enabled schedule SUPPRESSES idle-stop — the agent must
    stay present to receive fires (dormant-agent re-wake is out of scope).
    """
    return _soonest_schedule_due_at(agent_id, now_iso) is not None


def assemble_event_feed(
    agent_id: str,
    cursor: Optional[str],
    *,
    drain_queue: Optional[List[Dict[str, Any]]] = None,
    fire_scheduled: bool = False,
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
    # Peer profile changes (agent self-service profiles, PR2). Unbounded
    # stream like unassigned tasks — invisible to the internal clamp
    # inside stream 1, deduped against the in-memory push copy below.
    events.extend(_collect_agent_profile_events_for(agent_id, cursor))
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
    # Scheduled directives (event-loop scheduler). Fire due schedules ONLY
    # when the message backlog is NOT truncated (``msg_cap_ts is None``):
    # firing MUTATES state (resets next_due), so it must never emit an
    # event that the clamp would then drop — that would advance next_due
    # for a fire the agent never received. With 500+ pending messages the
    # agent drains those first; the schedule fires on a later poll. The
    # directive event's timestamp is "now" (the latest), so it joins the
    # sort at the end and the cursor advances to it cleanly.
    if fire_scheduled and msg_cap_ts is None:
        # Ad-hoc pokes first (highest-priority delivery), then scheduled
        # fires. Both mutate delivery state (delivered_at / next_due), so
        # they run only when the message backlog isn't truncated — a fired
        # event must never be clamped away.
        events.extend(_collect_pending_pokes_for(agent_id))
        events.extend(_collect_scheduled_directive_events_for(agent_id))
    # Priority-aware ordering (plan §11): urgent directives / pokes sort
    # ahead of ordinary events; same-priority events keep timestamp order.
    _sort_events_priority_then_time(events)
    if events:
        # The cursor stays anchored to max TIMESTAMP (not sort position) so
        # priority reordering never rewinds or over-advances progress.
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
    # No explicit cursor from the caller → resume from the agent's
    # persisted high-water cursor (`agents.last_event_seen_at`), mirroring
    # fetch_events_since. Without this, a no-arg reconnect — which the
    # wake-loop recovery guidance explicitly tells agents to do — resolves
    # `since` to the epoch and re-dumps the ENTIRE message backlog on every
    # call, blowing the tool output cap and wedging the loop.
    if not since:
        since = _read_last_event_seen_at(agent_id)

    # Event-loop long-hold (plan: event-loop-longlived-connections).
    # Resolve the per-connection hold strategy from the client's identity
    # (recorded at its initialize handshake) with a progressToken
    # feature-detect fallback, then derive how long THIS connection may
    # hold and whether we emit heartbeats while it does.
    from ..core.client_info_registry import get_client_name
    from ..core.client_hold_strategy import (
        resolve_hold_strategy,
        HEARTBEAT_INTERVAL_SECONDS,
        NO_HEARTBEAT_HOLD_SECONDS,
    )
    from ..core.mcp_progress import current_progress_token

    progress_token = current_progress_token()
    _client_name = get_client_name(agent_id)
    strategy = resolve_hold_strategy(
        _client_name,
        has_progress_token=progress_token is not None,
    )
    strategy_cap = (
        strategy.hold_cap
        if strategy.hold_cap is not None
        else _UNCAPPED_HOLD_CEILING_SECONDS
    )
    if strategy.heartbeat:
        # Heartbeat clients hold long — up to their cap (Claude Code 24h)
        # or the uncapped ceiling (OpenCode) — and we keep the client's
        # idle timer alive with periodic progress frames.
        base_hold = strategy_cap
    else:
        # No-heartbeat clients get a silent ~55s hold under the universal
        # 60s SDK default, then reconnect.
        base_hold = min(NO_HEARTBEAT_HOLD_SECONDS, strategy_cap)

    # An explicit caller `timeout_seconds` still caps the hold (agents in
    # the wake loop normally omit it and let the strategy decide). Absent /
    # non-positive / non-numeric → strategy-driven only.
    requested: Optional[int] = None
    raw_timeout = arguments.get("timeout_seconds")
    if raw_timeout is not None:
        try:
            parsed = int(raw_timeout)
        except (TypeError, ValueError):
            parsed = None
        if parsed is not None and parsed > 0:
            requested = parsed
    timeout = base_hold if requested is None else min(base_hold, requested)

    # Adaptive hold ladder — only for heartbeat-capable clients that sent a
    # progressToken (the connections we can safely keep parked; without
    # heartbeats a long silent hold would let the client's own idle watchdog
    # kill it). When such an agent repeatedly caps itself with a short
    # timeout_seconds and gets only empty returns, ADVISE it, escalate, then
    # OVERRIDE (ignore the cap, park it). A one-off long monitor never trips
    # this — only a run of short empty polls does. See core/hold_ladder.py.
    from ..core import hold_ladder
    _ladder_eligible = (
        strategy.heartbeat
        and progress_token is not None
        and requested is not None
        and requested < base_hold
    )
    _ladder_advisory: Optional[str] = None
    _ladder_override = False
    _ladder_phase = "n/a"
    if _ladder_eligible:
        _decision = hold_ladder.decide(hold_ladder.get_count(agent_id))
        _ladder_phase = _decision.phase
        _ladder_advisory = _decision.advisory
        _ladder_override = _decision.override_hold
        if _ladder_override:
            timeout = base_hold  # ignore the agent's short cap; park it
    else:
        # Not self-capping (or the client can't be safely parked) — fine
        # behaviour, so reset the ladder.
        hold_ladder.reset(agent_id)

    _poll_start = asyncio.get_event_loop().time()
    _evlog(
        "poll START agent=%s client=%r heartbeat=%s hold_cap=%s "
        "progress_token=%s requested=%s hold_budget=%ss ladder=%s",
        agent_id, _client_name, strategy.heartbeat, strategy.hold_cap,
        progress_token is not None, requested, timeout, _ladder_phase,
    )

    # Agent self-service profiles (PR3): compute the profile-review section
    # ONCE per call (marks the connection greeted as a side effect) and
    # ride it on whichever envelope this call returns — greet on the first
    # loop call of a connection, or when the profile is overdue.
    from ..core.profile_review import build_profile_review_section
    review_section = build_profile_review_section(principal)

    # PR-B fan-out: each call owns a private synthetic-event queue so
    # N concurrent waiters per agent each receive every notification.
    # Register on entry; unregister in the finally block on exit.
    waiter_queue = g.register_waiter(agent_id)
    # Newest-wins: this new connection supersedes any prior parked
    # wait_for_events for the same agent (a backgrounded/stale duplicate).
    # The old waiter(s) wake on the supersede sentinel and return a
    # connection_superseded event, so an agent keeps exactly ONE
    # event-loop connection instead of accumulating parked ones.
    _superseded_n = g.supersede_prior_waiters(agent_id, waiter_queue)
    if _superseded_n:
        _evlog(
            "supersede agent=%s evicted %d prior wait_for_events connection(s)",
            agent_id, _superseded_n,
        )
    try:
        # Flag gate: if either toggle is OFF, return stop_listening now.
        enabled, reason = _check_auto_event_loop_flags(agent_id)
        if not enabled:
            stop_evt = _stop_listening_event(reason or "auto_event_loop is OFF")
            # Drop anything that landed in our queue before the gate
            # check — agent is opted out, the events are stale.
            g.drain_waiter_queue(waiter_queue)
            return _envelope([stop_evt], since, profile_review=review_section)

        # Fast path — combine DB backlog with synthetic events that
        # arrived between register_waiter() and this point. The single
        # feed owner runs union → dedup → clamp → sort → cursor; drain
        # the waiter's private synthetic queue as stream 3.
        events: List[Dict[str, Any]]
        events, cursor_value = assemble_event_feed(
            agent_id, since, drain_queue=g.drain_waiter_queue(waiter_queue),
            fire_scheduled=True,
        )
        if events:
            _evlog(
                "poll END agent=%s returned %d event(s) types=%s (fast path, "
                "backlog present at entry)",
                agent_id, len(events),
                [e.get("type") for e in events],
            )
            hold_ladder.reset(agent_id)  # a real event resets the ladder
            env = _envelope(events, since, profile_review=review_section)
            if cursor_value:
                _write_last_event_seen_at(agent_id, cursor_value)
            # Idle-stop: a real event resets the per-agent idle clock.
            _write_last_activity_at(agent_id, datetime.datetime.now().isoformat())
            return env

        # Idle-stop (event-loop wind-down): no events pending. Read how
        # long until this agent's idle window expires (seeds the marker on
        # first listen). If already exceeded, tell the agent to stop
        # reconnecting instead of holding again. `None` ⇒ window is 0
        # (infinite / never stop). This is the ONLY thing that ends the
        # connection for a no-cap heartbeat client.
        idle_remaining = _idle_stop_seconds_remaining(agent_id)
        # Decision 9: an enabled schedule SUPPRESSES idle-stop — the agent
        # must stay present to receive its fires (dormant re-wake is out of
        # scope). Only stop when the window is exceeded AND no enabled
        # schedule exists.
        if (
            idle_remaining is not None
            and idle_remaining <= 0
            and not _agent_has_active_schedule(
                agent_id, datetime.datetime.now().isoformat()
            )
        ):
            stop_evt = _stop_listening_event(
                "event-loop idle-stop window exceeded (no events)"
            )
            g.drain_waiter_queue(waiter_queue)
            return _envelope([stop_evt], since, profile_review=review_section)

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
        # Idle-stop deadline (monotonic). `idle_remaining` was computed
        # above from the persisted marker; within a single holding call no
        # real event is delivered without returning, so the marker is
        # constant here and we can convert it to a fixed monotonic deadline
        # rather than re-reading the DB each slice. Crossing it returns
        # stop_listening (vs the hold `deadline`, which returns empty →
        # reconnect). `None` ⇒ idle-stop disabled (window 0).
        idle_deadline: Optional[float] = (
            asyncio.get_event_loop().time() + idle_remaining
            if idle_remaining is not None
            else None
        )
        # Heartbeat bookkeeping (heartbeat clients only). We emit a
        # `notifications/progress` frame every HEARTBEAT_INTERVAL_SECONDS
        # of silence so the client resets its idle timeout; the flag
        # re-check still runs at the tighter 2s slice cadence. `progress`
        # must increase monotonically across the request (MCP spec).
        heartbeat_enabled = strategy.heartbeat and progress_token is not None
        next_heartbeat = (
            asyncio.get_event_loop().time() + HEARTBEAT_INTERVAL_SECONDS
        )
        heartbeat_progress = 0.0
        heartbeat_misses = 0

        # Idle backlog reminder — periodically nudge an idle agent that still
        # has unread messages / open tasks. Per-agent timer seeded on first
        # sight (a fresh connection waits a full interval, not fires now).
        from ..core import idle_reminder
        from ..tools.access import _get_config_bool, _get_config_int

        _reminder_enabled = _get_config_bool("config_idle_reminder_enabled", True)
        _reminder_interval = _get_config_int(
            "config_idle_reminder_interval_seconds", 3600
        )
        _reminder_deadline: Optional[float] = None
        if _reminder_enabled and _reminder_interval > 0:
            _rnow = asyncio.get_event_loop().time()
            _reminder_deadline = _rnow + idle_reminder.seconds_until_due(
                agent_id, float(_reminder_interval), _rnow
            )

        while True:
            now_mono = asyncio.get_event_loop().time()
            now_iso = datetime.datetime.now().isoformat()
            # Soonest scheduled-directive fire for this agent (a new wake
            # condition alongside flag-recheck / heartbeat / idle-stop). A
            # schedule created or re-armed mid-hold is picked up here (the
            # query is re-run each slice), so a create's waiter-wake and
            # this poll both converge on firing it.
            soonest_due = _soonest_schedule_due_at(agent_id, now_iso)
            has_schedule = soonest_due is not None
            # Idle-stop wins over the hold deadline: a long-holding
            # (esp. no-cap heartbeat) connection winds the agent down when
            # the idle window is crossed mid-hold — UNLESS an enabled
            # schedule keeps it alive (decision 9, re-checked each slice so
            # the last schedule ending resumes normal idle-stop).
            if (
                idle_deadline is not None
                and now_mono >= idle_deadline
                and not has_schedule
            ):
                stop_evt = _stop_listening_event(
                    "event-loop idle-stop window exceeded (no events)"
                )
                g.drain_waiter_queue(waiter_queue)
                return _envelope(
                    [stop_evt], since, profile_review=review_section
                )
            # Idle backlog reminder due → if the agent still has unread
            # messages / open tasks, wake it with a listed summary. No backlog
            # → just advance the timer and keep holding for free.
            if _reminder_deadline is not None and now_mono >= _reminder_deadline:
                idle_reminder.mark_checked(agent_id, now_mono)
                _reminder_deadline = now_mono + float(_reminder_interval)
                _backlog = idle_reminder.collect_backlog(agent_id)
                if _backlog is not None:
                    _evlog(
                        "reminder agent=%s unread=%d open_tasks=%d "
                        "(idle backlog nudge)",
                        agent_id, _backlog["unread_count"],
                        _backlog["task_count"],
                    )
                    hold_ladder.reset(agent_id)
                    g.drain_waiter_queue(waiter_queue)
                    return _envelope(
                        [idle_reminder.reminder_event(_backlog)], since,
                        profile_review=review_section,
                    )
            # A schedule is due now → fire it (wait-loop-native) and return.
            if has_schedule and soonest_due <= now_iso:
                events, cursor_value = assemble_event_feed(
                    agent_id, since, fire_scheduled=True,
                )
                if events:
                    hold_ladder.reset(agent_id)  # real event resets the ladder
                    env = _envelope(
                        events, since, profile_review=review_section
                    )
                    if cursor_value:
                        _write_last_event_seen_at(agent_id, cursor_value)
                    _write_last_activity_at(agent_id, now_iso)
                    return env
            remaining = deadline - now_mono
            if remaining <= 0:
                _evlog(
                    "poll END agent=%s EMPTY after %.0fs -> client must "
                    "RECONNECT (heartbeat=%s). Short (~55s) empties in a loop "
                    "= NOT parking (no-heartbeat path); a long hold = parking.",
                    agent_id, now_mono - _poll_start, heartbeat_enabled,
                )
                # Adaptive hold ladder: an eligible short empty poll climbs the
                # run counter (skip when we already overrode — that empty is a
                # long park, not a wasteful short poll) and rides the escalating
                # advisory back to the agent.
                extra_events: List[Dict[str, Any]] = []
                if _ladder_eligible and not _ladder_override:
                    _n = hold_ladder.note_empty_short_poll(agent_id)
                    _evlog(
                        "ladder agent=%s empty-short-poll run=%d phase=%s",
                        agent_id, _n, _ladder_phase,
                    )
                    if _ladder_advisory:
                        extra_events.append(
                            hold_ladder.advisory_event(_ladder_advisory)
                        )
                return _envelope(
                    extra_events, since, profile_review=review_section
                )
            slice_timeout = min(_FLAG_RECHECK_INTERVAL_SECONDS, remaining)
            if idle_deadline is not None:
                # Don't overshoot the idle-stop by more than a slice.
                slice_timeout = min(slice_timeout, idle_deadline - now_mono)
            if _reminder_deadline is not None:
                # Wake at the reminder boundary so the nudge is prompt.
                slice_timeout = min(
                    slice_timeout, max(0.0, _reminder_deadline - now_mono)
                )
            if has_schedule:
                # Wake right at the soonest next_due so the fire is prompt
                # (bounded below by 0; a due-in-the-past schedule was
                # already handled above).
                try:
                    due_dt = datetime.datetime.fromisoformat(soonest_due)
                    secs_until_due = (
                        due_dt - datetime.datetime.now()
                    ).total_seconds()
                    if secs_until_due > 0:
                        slice_timeout = min(slice_timeout, secs_until_due)
                except (TypeError, ValueError):  # pragma: no cover
                    pass
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
                # Newest-wins: a newer wait_for_events for this agent
                # superseded us. Close this (stale/duplicate) call with a
                # connection_superseded event — NOT stop_listening: the loop
                # keeps running on the newer connection.
                if first_item is g.WAITER_SUPERSEDE_SENTINEL:
                    _evlog(
                        "poll END agent=%s SUPERSEDED by a newer connection",
                        agent_id,
                    )
                    g.drain_waiter_queue(waiter_queue)
                    return _envelope(
                        [_superseded_event()], since,
                        profile_review=review_section,
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
                    return _envelope([stop_evt], since, profile_review=review_section)
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
                    agent_id, since, drain_queue=drained, fire_scheduled=True,
                )
                if events:
                    _evlog(
                        "poll END agent=%s returned %d event(s) types=%s "
                        "(WOKE after %.0fs of holding)",
                        agent_id, len(events),
                        [e.get("type") for e in events],
                        asyncio.get_event_loop().time() - _poll_start,
                    )
                    hold_ladder.reset(agent_id)  # a real event resets the ladder
                    env = _envelope(events, since, profile_review=review_section)
                    if cursor_value:
                        _write_last_event_seen_at(agent_id, cursor_value)
                    # Idle-stop: a real event resets the idle clock.
                    _write_last_activity_at(
                        agent_id, datetime.datetime.now().isoformat()
                    )
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
                    return _envelope([stop_evt], since, profile_review=review_section)
                # Heartbeat: keep a heartbeat-capable client's idle timer
                # alive across the long silent hold. Sent at most once per
                # HEARTBEAT_INTERVAL_SECONDS regardless of the tighter flag
                # slice.
                if heartbeat_enabled and (
                    asyncio.get_event_loop().time() >= next_heartbeat
                ):
                    from ..core.mcp_progress import send_progress_heartbeat

                    heartbeat_progress += 1.0
                    sent = await send_progress_heartbeat(
                        progress_token, heartbeat_progress
                    )
                    if sent:
                        heartbeat_misses = 0
                        _evlog(
                            "heartbeat SENT agent=%s n=%.0f (idle %.0fs, "
                            "connection PARKED and listening)",
                            agent_id, heartbeat_progress,
                            asyncio.get_event_loop().time() - _poll_start,
                        )
                    else:
                        # The progress frame could not be delivered ⇒ the
                        # client's transport is gone. Reap after a few
                        # consecutive misses so a vanished client (which
                        # never reconnects to trigger supersede) doesn't
                        # park until the deadline, holding the slot and
                        # lingering "online" in presence.
                        heartbeat_misses += 1
                        _evlog(
                            "heartbeat MISS agent=%s (%d/%d) — client "
                            "transport unreachable (idle %.0fs)",
                            agent_id, heartbeat_misses,
                            _MAX_HEARTBEAT_MISSES,
                            asyncio.get_event_loop().time() - _poll_start,
                        )
                        if heartbeat_misses >= _MAX_HEARTBEAT_MISSES:
                            _evlog(
                                "poll END agent=%s REAPED half-open "
                                "connection after %d consecutive heartbeat "
                                "misses",
                                agent_id, heartbeat_misses,
                            )
                            g.drain_waiter_queue(waiter_queue)
                            return _envelope(
                                [], since, profile_review=review_section
                            )
                    next_heartbeat = (
                        asyncio.get_event_loop().time()
                        + HEARTBEAT_INTERVAL_SECONDS
                    )
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
    events, new_cursor = assemble_event_feed(
        agent_id, cursor, fire_scheduled=True,
    )
    if events:
        _write_last_event_seen_at(agent_id, new_cursor)

    body = {"events": events, "cursor": new_cursor}
    # Agent self-service profiles (PR3): the catch-up path is often the
    # first event-loop call of a session, so carry the profile-review
    # greet/overdue section here too (rides the loop, not the system
    # prompt). Shared greet flag with wait_for_events — whichever runs
    # first greets.
    from ..core.profile_review import build_profile_review_section
    review_section = build_profile_review_section(principal)
    if review_section is not None:
        body["profile_review"] = review_section
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
                        "For a reply, use parent_message_id rather than an "
                        "'RE:' subject — replies are threaded, not "
                        "subject-bearing (subject is ignored / forced NULL "
                        "when parent_message_id is set). If omitted on a "
                        "root message, an Ollama-backed helper proposes one "
                        "when AGENT_MCP_SUBJECT_MODEL is configured; "
                        "otherwise the body is truncated to 50 chars + "
                        "'...' as a fallback."
                    ),
                },
                "parent_message_id": {
                    "type": ["string", "null"],
                    "description": (
                        "How you reply / thread a message: set this to the "
                        "message_id you are replying to and this message is "
                        "linked as a reply under that thread. Prefer this "
                        "over typing 'RE:' into the subject. Replies always "
                        "have subject = NULL (the thread shows the root's "
                        "subject as the conversation title)."
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
                        "Optional cap (seconds) on how long this call may "
                        "block before returning an empty envelope. Normally "
                        "OMIT this — the server picks a client-appropriate "
                        "hold (heartbeat long-hold for capable clients, a "
                        "short silent hold otherwise). When provided it only "
                        "SHORTENS the server's hold, never extends it."
                    ),
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