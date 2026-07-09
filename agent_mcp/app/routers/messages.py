"""Messages resource router — ``/api/messages/...``.

Wave 8 PR 1 of prancy-napping-pie: the message handlers
mechanically moved out of ``app/routes.py`` onto this router:
``list_messages`` (POST /query), ``list_participants`` (POST
/participants), ``create_message`` (POST), ``suggest_subject``
(POST /suggest-subject), ``patch_message`` (PATCH/DELETE
/{message_id}).

Auth: handler-level ``Depends(require_operator_session)`` is kept
verbatim on the handlers that currently have it. The router-level
gate is deferred to a follow-up PR.

Route ordering note: ``/query``, ``/participants``, and
``/suggest-subject`` are declared BEFORE the ``/{message_id}``
catch-all so the static paths match first. FastAPI walks routes in
registration order (same as the underlying Starlette router); the
same ordering convention applied in the legacy
``_dashboard_route_specs`` table.
"""

from __future__ import annotations

import datetime
import os
import secrets as _secrets

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from starlette.requests import Request

from .._dispatch_helpers import handle_options
from ..deps import caller_identity, require_operator_session
from ...core.config import logger
from ...db.actions.agent_actions_db import log_agent_action_to_db
from ...db.connection import get_db_connection
from ...utils.json_utils import get_sanitized_json_body


router = APIRouter(
    prefix="/api/messages",
    tags=["messages"],
)


# --- Messages CRUD endpoints (Phase 6 PR #20 / issue P) ---
# agent_messages accumulates indefinitely; reading via get_agent_messages
# marks `read=1` but never deletes. The dashboard's new Messages tab
# needs list+filter+compose+mark-read access. Admin-only.


_MESSAGE_TYPES = ("text", "system", "notification", "task_update",
                  "assistance_request", "stop_command")
_MESSAGE_PRIORITIES = ("low", "normal", "high", "urgent")


@router.api_route("/query", methods=["POST", "OPTIONS"])
async def list_messages_api_route(
    request: Request,
    auth: dict = Depends(require_operator_session),
) -> JSONResponse:
    """POST /api/messages/query with rich filters.

    PR D (prancy-napping-pie): auth via ``require_operator_session``.

    Originally exposed as GET, but browsers strip request bodies from
    GET (per the Fetch spec), which broke the dashboard's Messages tab.
    We use POST + a dedicated /query suffix so that compose
    (POST /api/messages) and listing (POST /api/messages/query) coexist
    without method overloading.

    Body fields:
      from           sender_id filter
      to             recipient_id filter
      between        [a, b] — messages either direction between two agents
      type           message_type filter
      priority       priority filter
      read           bool — read flag filter
      since/until    ISO timestamp window
      q              content substring (LIKE %q%)
      limit/offset   pagination (default 50 / 0)
    """
    if request.method == 'OPTIONS':
        return await handle_options(request)
    if request.method != 'POST':
        return JSONResponse({"error": "Method not allowed"}, status_code=405)

    try:
        data = await get_sanitized_json_body(request)

        limit = int(data.get('limit', 50))
        offset = int(data.get('offset', 0))

        if limit < 1 or limit > 500:
            return JSONResponse(
                {"error": "limit must be 1..500"}, status_code=400
            )

        # PR 6: route through MessageRepository.query / count_query.
        # The repo owns the WHERE-building loop today — one entry
        # point shared with the MCP get_agent_messages tool (Candidate
        # 3 folding). The route just funnels the body through.
        from ...repositories import message_repo

        filters = {
            "from": data.get("from"),
            "to": data.get("to"),
            "between": data.get("between"),
            "type": data.get("type"),
            "priority": data.get("priority"),
            "read": data.get("read"),
            "since": data.get("since"),
            "until": data.get("until"),
            "q": data.get("q"),
            "limit": limit,
            "offset": offset,
        }
        rows = message_repo.query(filters)
        total = message_repo.count_query(filters)

        return JSONResponse({"messages": rows, "total": total,
                             "limit": limit, "offset": offset})
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.error(f"Error listing messages: {e}", exc_info=True)
        # BL-R5-2 / SD-R6-1: generic message — ``str(e)`` on a
        # SQLAlchemyError / sqlite3.*Error embeds SQL text + bound
        # params (schema disclosure). Detail stays in the log above.
        return JSONResponse(
            {"error": "Failed to list messages"}, status_code=500
        )


@router.api_route("/participants", methods=["POST", "OPTIONS"])
async def list_participants_api_route(
    request: Request,
    auth: dict = Depends(require_operator_session),
) -> JSONResponse:
    """POST /api/messages/participants — agents available as filter values.

    PR D (prancy-napping-pie): auth via ``require_operator_session``.

    Returns the set of agent identifiers that should populate the
    Messages tab's From/To filter dropdowns. /api/agents was the
    previous source but returns every row including
    ``status='terminated'``, leaking ghost agents that no longer appear
    on the Agents page.

    Response shape::

        {
          "live": [{"agent_id": "...", "status": "..."}, ...],
          "tombstones": ["[deleted-old-worker-1]", ...]
        }

    ``live`` excludes terminated agents and prepends a synthetic
    ``admin`` entry (the agents table has no admin row, but admin is a
    valid sender/recipient).

    ``tombstones`` are DISTINCT sender_id / recipient_id values that
    begin with ``[deleted-`` — the marker the PR C agent-purge cascade
    writes when an agent is permanently removed. Sorted lexicographically
    so the dropdown order is stable. Empty list until PR C lands.
    """
    if request.method == 'OPTIONS':
        return await handle_options(request)
    if request.method != 'POST':
        return JSONResponse({"error": "Method not allowed"}, status_code=405)

    try:
        _ = await get_sanitized_json_body(request)

        # PR 6: route through MessageRepository.list_participants. The
        # repo owns the filter rules (excludes terminated/tombstone,
        # prepends synthetic admin, mines tombstones from DISTINCT
        # sender/recipient UNION).
        from ...repositories import message_repo
        result = message_repo.list_participants()
        return JSONResponse(result)
    except Exception as e:
        logger.error(f"Error listing participants: {e}", exc_info=True)
        # BL-R5-2 / SD-R6-1: generic message — see list-messages note.
        return JSONResponse(
            {"error": "Failed to list participants"},
            status_code=500,
        )


@router.api_route("/suggest-subject", methods=["POST", "OPTIONS"])
async def suggest_subject_api_route(
    request: Request,
    auth: dict = Depends(require_operator_session),
) -> JSONResponse:
    """POST /api/messages/suggest-subject — Ollama-backed subject helper.

    PR D (prancy-napping-pie): auth via ``require_operator_session``.

    Body: {content}
    Returns: {"subject": "<string>"}   on success
             {"subject": null}          when AGENT_MCP_SUBJECT_MODEL is
                                        unset OR the helper failed.

    Why graceful degrade (200 + null) rather than 503: the dashboard
    treats this as a hint, not a hard requirement. If the helper is
    down, the user types a subject by hand; we don't want to colour
    that "subject is empty" path as an error in the network panel.
    """
    if request.method == 'OPTIONS':
        return await handle_options(request)
    if request.method != 'POST':
        return JSONResponse({"error": "Method not allowed"}, status_code=405)

    try:
        data = await get_sanitized_json_body(request)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    # PF-R8-1: guard non-string ``content`` BEFORE ``.strip()`` — this
    # line sits outside the ValueError try above, so a dict/list body
    # would raise AttributeError and surface as an uncaught 500.
    raw_content = data.get('content')
    if raw_content is not None and not isinstance(raw_content, str):
        return JSONResponse(
            {"error": "content must be a string"}, status_code=400
        )
    content = (raw_content or "").strip()
    if not content:
        return JSONResponse({"subject": None})

    # Short-circuit when no Ollama backend is configured. Saves the
    # helper import + the no-op call. Matches the gate inside
    # send_agent_message_tool_impl.
    if not os.environ.get("AGENT_MCP_SUBJECT_MODEL", "").strip():
        return JSONResponse({"subject": None})

    try:
        from ...features.message_suggestions import suggest_subject
        subject = await suggest_subject(content)
    except Exception as e:  # pragma: no cover — defensive
        logger.warning("suggest_subject endpoint helper raised: %s", e)
        subject = None

    return JSONResponse({"subject": subject})


@router.api_route("", methods=["POST", "OPTIONS"])
async def create_message_api_route(
    request: Request,
    auth: dict = Depends(require_operator_session),
) -> JSONResponse:
    """POST /api/messages — admin composes a message to a recipient.

    PR D (prancy-napping-pie): auth via ``require_operator_session``.

    Body: {recipient_id, message_content, message_type?, priority?,
           subject?, parent_message_id?}
    Returns: {success, message_id, message}

    v5.0.22 (message threads + subjects):
      * `subject` — root-only one-liner; persisted verbatim when
        present. Force-NULLed for replies.
      * `parent_message_id` — when set, this message is a reply to
        the named root; subject is forced NULL regardless of input.
    """
    if request.method == 'OPTIONS':
        return await handle_options(request)
    if request.method != 'POST':
        return JSONResponse({"error": "Method not allowed"}, status_code=405)

    conn = None
    try:
        data = await get_sanitized_json_body(request)
        recipient_id = data.get('recipient_id')
        content = data.get('message_content')
        message_type = data.get('message_type', 'text')
        priority = data.get('priority', 'normal')
        # v5.0.22 — message threads + subjects.
        explicit_subject = data.get('subject')
        parent_message_id = data.get('parent_message_id')

        # PF-R8-1: reject non-string ``recipient_id`` / ``message_content``
        # BEFORE the truthiness gate + SQLite bind. A dict/list passes
        # ``if not x`` (truthy), then a bind raises sqlite ProgrammingError
        # → an uncaught 500; validate to a 400 up front instead.
        if recipient_id is not None and not isinstance(recipient_id, str):
            return JSONResponse(
                {"error": "recipient_id must be a string"}, status_code=400
            )
        if content is not None and not isinstance(content, str):
            return JSONResponse(
                {"error": "message_content must be a string"}, status_code=400
            )
        if not recipient_id:
            return JSONResponse(
                {"error": "recipient_id is required"}, status_code=400
            )
        if not content:
            return JSONResponse(
                {"error": "message_content is required"}, status_code=400
            )
        if message_type not in _MESSAGE_TYPES:
            return JSONResponse(
                {"error": f"message_type must be one of {_MESSAGE_TYPES}"},
                status_code=400,
            )
        if priority not in _MESSAGE_PRIORITIES:
            return JSONResponse(
                {"error": f"priority must be one of {_MESSAGE_PRIORITIES}"},
                status_code=400,
            )

        timestamp = datetime.datetime.now().isoformat()
        sender_id = caller_identity(auth)

        conn = get_db_connection()
        cursor = conn.cursor()

        # PR 9 (Message flip): single import covers both the broadcast
        # bulk_send below and the single-recipient send further down.
        from ...repositories import message_repo

        # Broadcast: recipient_id="*" fans out to every active worker
        # (admin excluded), mirroring the broadcast_message MCP tool.
        # One INSERT per recipient so the messages show up in the
        # listing keyed by their real recipient_id.
        if recipient_id == "*":
            from ...core import globals as _g
            recipients: list[str] = []
            for _tok, agent_data in _g.active_agents.items():
                rid = agent_data.get("agent_id")
                if rid and rid != "admin" and rid != sender_id:
                    recipients.append(rid)

            # PR 9 (Message flip): the broadcast bulk INSERT now goes
            # through `message_repo.bulk_send` instead of the legacy
            # `bulk_insert_messages` shim. Same executemany pattern
            # under the hood (PR #98), plus an EventBus
            # `message.created` publish per distinct recipient — the
            # action-log INSERT keeps its raw cursor so the audit
            # entry still commits atomically with whatever else this
            # route writes.
            sent_ids: list[str] = []
            broadcast_rows: list[dict] = []
            for rid in recipients:
                msg_id = f"msg_{_secrets.token_hex(8)}"
                sent_ids.append(msg_id)
                broadcast_rows.append({
                    "message_id": msg_id,
                    "sender_id": sender_id,
                    "recipient_id": rid,
                    "message_content": content,
                    "message_type": message_type,
                    "priority": priority,
                    "timestamp": timestamp,
                    "delivered": False,
                    "read": False,
                })
            message_repo.bulk_send(broadcast_rows)
            log_agent_action_to_db(
                cursor, sender_id, "broadcast_message_via_dashboard",
                details={"recipients": recipients,
                         "sent_count": len(sent_ids)},
            )
            conn.commit()
            return JSONResponse({
                "success": True,
                "broadcast": True,
                "sent_count": len(sent_ids),
                "message_ids": sent_ids,
                "message": f"Broadcast sent to {len(sent_ids)} agents",
            })

        message_id = f"msg_{_secrets.token_hex(8)}"

        # v5.0.22 effective-subject computation. Three branches —
        # mirrors send_agent_message_tool_impl exactly:
        #   1. Reply (parent set) → subject NULL regardless of body.
        #   2. Explicit subject → verbatim.
        #   3. Root w/o subject → Ollama suggest_subject if
        #      AGENT_MCP_SUBJECT_MODEL is set; otherwise truncated body.
        effective_subject: str | None
        if parent_message_id:
            effective_subject = None
        elif explicit_subject:
            effective_subject = explicit_subject
        else:
            suggested: str | None = None
            if os.environ.get("AGENT_MCP_SUBJECT_MODEL", "").strip():
                from ...features.message_suggestions import suggest_subject
                suggested = await suggest_subject(content)
            if suggested:
                effective_subject = suggested
            else:
                effective_subject = (
                    content[:50] + "..." if len(content) > 50 else content
                )

        # PR 6: single-recipient message INSERT goes through message_repo
        # with the caller's cursor so it's atomic with the audit log.
        message_repo.send(
            message_id=message_id,
            sender_id=sender_id,
            recipient_id=recipient_id,
            message_content=content,
            message_type=message_type,
            priority=priority,
            timestamp=timestamp,
            subject=effective_subject,
            parent_message_id=parent_message_id,
            connection=cursor,
        )
        log_agent_action_to_db(
            cursor, sender_id, "sent_message_via_dashboard",
            details={"message_id": message_id, "recipient": recipient_id},
        )
        conn.commit()

        # BL-R8-1: message_repo.send(connection=cursor) DELIBERATELY
        # suppresses its own ``message.created`` publish while the caller
        # cursor is open (a subscriber must never observe an uncommitted
        # row). Every other send path re-fires the wake post-commit — the
        # MCP send_agent_message tool calls g.notify_agent_inbox(), the
        # broadcast branch above publishes via bulk_send. Mirror that here
        # so a recipient blocked in wait_for_events is woken for a
        # dashboard-composed direct message. Defensive: the row is already
        # committed, so a failed wake must not fail the request.
        try:
            from ...core import globals as _g
            _g.notify_agent_inbox(recipient_id)
        except Exception as notify_exc:  # pragma: no cover - defensive
            logger.warning(
                "notify_agent_inbox(%s) raised after dashboard message "
                "send: %s", recipient_id, notify_exc,
            )

        return JSONResponse({
            "success": True,
            "message_id": message_id,
            "message": f"Message sent to {recipient_id}",
        })
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Error sending message: {e}", exc_info=True)
        # BL-R5-2 / SD-R6-1: generic message — see list-messages note.
        return JSONResponse(
            {"error": "Failed to send message"}, status_code=500
        )
    finally:
        if conn:
            conn.close()


@router.api_route("/{message_id}", methods=["PATCH", "DELETE", "OPTIONS"])
async def patch_message_api_route(
    request: Request,
    auth: dict = Depends(require_operator_session),
) -> JSONResponse:
    """PATCH/DELETE /api/messages/{message_id}.

    PATCH flips read/delivered. DELETE removes the row (used by the
    dashboard's row-level + bulk delete actions).

    PR D (prancy-napping-pie): auth via ``require_operator_session``.
    """
    if request.method == 'OPTIONS':
        return await handle_options(request)
    if request.method not in ('PATCH', 'DELETE'):
        return JSONResponse({"error": "Method not allowed"}, status_code=405)

    path_parts = request.url.path.split('/')
    if len(path_parts) < 4 or not path_parts[-1]:
        return JSONResponse({"error": "message_id is required in URL"}, status_code=400)
    message_id = path_parts[-1]

    conn = None
    try:
        data = await get_sanitized_json_body(request)

        # PR 6: existence check via message_repo (cache-bypassing read).
        from ...repositories import message_repo
        if message_repo.get_by_id(message_id) is None:
            return JSONResponse({"error": "Message not found"}, status_code=404)

        conn = get_db_connection()
        cursor = conn.cursor()

        if request.method == 'DELETE':
            # PR 6: message DELETE goes through message_repo with the
            # caller's cursor so it stays atomic with the audit log.
            message_repo.delete(message_id, connection=cursor)
            log_agent_action_to_db(
                cursor, caller_identity(auth),
                "deleted_message_via_dashboard",
                details={"message_id": message_id},
            )
            conn.commit()
            return JSONResponse({"success": True, "deleted": message_id})

        # PATCH
        updates: list[tuple[str, object]] = []
        if 'read' in data:
            updates.append(("read", 1 if data['read'] else 0))
        if 'delivered' in data:
            updates.append(("delivered", 1 if data['delivered'] else 0))
        if not updates:
            return JSONResponse(
                {"error": "no updatable field provided (read, delivered)"},
                status_code=400,
            )

        # PR 6: PATCH flips run through message_repo.mark_delivered for
        # the `delivered` field; `read=true` becomes the single-message
        # variant of mark_read (the bulk recipient version is for the
        # MCP tool path). Both go through the caller's cursor so the
        # audit-log INSERT below lands in the same transaction.
        for col, val in updates:
            if col == "delivered":
                message_repo.mark_delivered(
                    message_id, bool(val), connection=cursor,
                )
            elif col == "read":
                # PR 9 (Message flip): single-message mark-read goes
                # through `message_repo.mark_read` with the caller's
                # cursor so the UPDATE and the audit-log INSERT below
                # land in one transaction.
                message_repo.mark_read(
                    message_id, bool(val), connection=cursor,
                )
        log_agent_action_to_db(
            cursor, caller_identity(auth),
            "updated_message", details={"message_id": message_id,
                                        "fields": [c for c, _ in updates]},
        )
        conn.commit()
        return JSONResponse({"success": True})
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Error patching message: {e}", exc_info=True)
        # BL-R5-2 / SD-R6-1: generic message — see list-messages note.
        return JSONResponse(
            {"error": "Failed to patch message"}, status_code=500
        )
    finally:
        if conn:
            conn.close()
