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

from ..deps import caller_identity, require_operator_session
from ...core.config import logger
from ...db.actions.agent_actions_db import log_agent_action_to_db
from ...db.connection import get_db_connection
from ...db.unit_of_work import unit_of_work
from ...repositories.message_repository import ParentMessageNotFound
from ...utils.json_utils import get_sanitized_json_body


router = APIRouter(
    prefix="/api/messages",
    tags=["messages"],
)


class _MessageStoreFailed(RuntimeError):
    """Internal signal: ``message_repo.send`` returned ``None`` inside the
    ``unit_of_work`` scope (SD-R10-1 / PF-R32-1).

    Raised so the exception propagates out of the scope and rolls the
    whole unit back — no orphan audit row for a message that was never
    stored. Caught in ``create_message_api_route`` and mapped to a 500;
    never a false 200 success.
    """

    def __init__(self, message_id: str) -> None:
        self.message_id = message_id
        super().__init__(f"message store returned None for {message_id!r}")


# --- Messages CRUD endpoints (Phase 6 PR #20 / issue P) ---
# agent_messages accumulates indefinitely; reading via get_agent_messages
# marks `read=1` but never deletes. The dashboard's new Messages tab
# needs list+filter+compose+mark-read access. Admin-only.


_MESSAGE_TYPES = ("text", "system", "notification", "task_update",
                  "assistance_request", "stop_command")
_MESSAGE_PRIORITIES = ("low", "normal", "high", "urgent")


@router.post("/query")
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
    try:
        data = await get_sanitized_json_body(request)

        limit = int(data.get('limit', 50))
        offset = int(data.get('offset', 0))

        if limit < 1 or limit > 500:
            return JSONResponse(
                {"error": "limit must be 1..500"}, status_code=400
            )
        # PF-R14-1: a negative offset is a harmless sqlite no-op today,
        # but clamp to a 0 floor for defense-in-depth (unbounded-below
        # pagination has no legitimate use).
        offset = max(0, offset)

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
    except (TypeError, ValueError, OverflowError) as e:
        # PF-R14-1: ``int(data.get('limit'/'offset'))`` raises TypeError
        # (not ValueError) when the caller sends a list/dict value, e.g.
        # ``{"limit": [1, 2]}``. A bare ``except ValueError`` let that
        # fall through to the generic 500; catch both so a non-numeric
        # limit/offset returns a clean 400 like the non-numeric-string
        # case (``int("abc")`` → ValueError) already did.
        # PF-R18-1: ``int(float('inf'))`` raises OverflowError — a THIRD
        # sibling PF-R14-1 missed. A JSON number token like ``1e400``
        # parses to ``float('inf')`` via ``json.loads``, so an
        # overflowing ``{"limit": 1e400}`` slipped past the guard to a
        # 500. Catch it too so it 400s like the other malformed numerics.
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.error(f"Error listing messages: {e}", exc_info=True)
        # BL-R5-2 / SD-R6-1: generic message — ``str(e)`` on a
        # SQLAlchemyError / sqlite3.*Error embeds SQL text + bound
        # params (schema disclosure). Detail stays in the log above.
        return JSONResponse(
            {"error": "Failed to list messages"}, status_code=500
        )


@router.post("/participants")
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
    try:
        _ = await get_sanitized_json_body(request)

        # PR 6: route through MessageRepository.list_participants. The
        # repo owns the filter rules (excludes terminated/tombstone,
        # prepends synthetic admin, mines tombstones from DISTINCT
        # sender/recipient UNION).
        #
        # PERF/DOS (pentest R4-F2): bound the read with the SAME shared
        # clamp every other /api list read uses (_clamp_section_limit,
        # [1, 5000], default 500) so a project with thousands of agents /
        # tombstone markers can't full-table-scan agents + agent_messages
        # on each Messages-tab poll. ``?limit=`` overrides within bounds.
        # The clamped int is passed to the repo — the router owns the
        # request-parsing, the repo owns the SQL.
        from ._read_limits import _clamp_section_limit
        from ...repositories import message_repo
        result = message_repo.list_participants(
            limit=_clamp_section_limit(request)
        )
        return JSONResponse(result)
    except ValueError as e:
        # PF-R12-1: a non-object body (list / string / scalar) now raises
        # ValueError in get_sanitized_json_body. Return a clean 400 rather
        # than letting the generic Exception handler below map it to 500.
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.error(f"Error listing participants: {e}", exc_info=True)
        # BL-R5-2 / SD-R6-1: generic message — see list-messages note.
        return JSONResponse(
            {"error": "Failed to list participants"},
            status_code=500,
        )


@router.post("/suggest-subject")
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


@router.post("")
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
        # SD-R10-1: same silent-drop class as recipient_id/content. A
        # non-string ``subject``/``parent_message_id`` (dict/list) is
        # truthy, so it slips past every check below and reaches the
        # SQLite bind inside message_repo.send() — which swallows the
        # ProgrammingError into a None return. The route then reports a
        # false 200 for a message that was never stored. Reject up front.
        if explicit_subject is not None and not isinstance(
            explicit_subject, str
        ):
            return JSONResponse(
                {"error": "subject must be a string"}, status_code=400
            )
        if parent_message_id is not None and not isinstance(
            parent_message_id, str
        ):
            return JSONResponse(
                {"error": "parent_message_id must be a string"},
                status_code=400,
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

        # PR 9 (Message flip): single import covers both the broadcast
        # bulk_send below and the single-recipient send further down.
        from ...repositories import message_repo

        # Broadcast: recipient_id="*" fans out to every active worker
        # (admin excluded), mirroring the broadcast_message MCP tool.
        # One INSERT per recipient so the messages show up in the
        # listing keyed by their real recipient_id.
        #
        # The broadcast branch keeps its own hand-managed connection:
        # ``bulk_send`` owns its message INSERTs + per-recipient
        # ``message.created`` publishes on a separate session, so only
        # the audit-log row is committed on ``cursor`` here. The
        # single-recipient path below runs on the write-path
        # ``unit_of_work`` instead (D2).
        if recipient_id == "*":
            conn = get_db_connection()
            cursor = conn.cursor()
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

        # D2: the single-recipient send runs on the write-path
        # unit-of-work. The message INSERT + the audit-log row are
        # written on ``u.cursor`` (atomic under one commit); the
        # recipient inbox wake is *registered* on ``u`` and flushes only
        # after a successful commit. On any exception inside the scope
        # (send() → None, ParentMessageNotFound, unknown-recipient
        # LookupError) the uow rolls back and fires NOTHING — no message
        # row, no audit row, no wake (SD-R10-1 / PF-R32-1 re-guard).
        with unit_of_work() as u:
            # PR 6: single-recipient message INSERT goes through
            # message_repo on the uow cursor so it's atomic with the
            # audit log.
            stored = message_repo.send(
                message_id=message_id,
                sender_id=sender_id,
                recipient_id=recipient_id,
                message_content=content,
                message_type=message_type,
                priority=priority,
                timestamp=timestamp,
                subject=effective_subject,
                parent_message_id=parent_message_id,
                connection=u.cursor,
            )
            # SD-R10-1: send() returns None when the INSERT failed (bad
            # bind, FK violation, DB error) — it swallows the exception
            # internally. Must NOT commit a "sent" audit entry nor report
            # success for a message that was never stored. Raise to roll
            # the whole unit back BEFORE the audit-log INSERT so no false
            # trace survives; mapped to a 500 below.
            if stored is None:
                raise _MessageStoreFailed(message_id)
            # Audit row on the uow cursor — DB sink only, committed
            # atomically with the message INSERT (matches the prior
            # ``log_agent_action_to_db`` + ``conn.commit()`` behaviour).
            log_agent_action_to_db(
                u.cursor, sender_id, "sent_message_via_dashboard",
                details={"message_id": message_id, "recipient": recipient_id},
            )

            # BL-R8-1: message_repo.send(connection=...) DELIBERATELY
            # suppresses its own ``message.created`` publish while the
            # cursor's transaction is open (a subscriber must never
            # observe an uncommitted row). Every other send path re-fires
            # the wake post-commit — the MCP send_agent_message tool and
            # the broadcast branch above. Register the wake on the uow so
            # a recipient blocked in wait_for_events is woken for a
            # dashboard-composed direct message, only after commit. The
            # uow's post-commit flush isolates a failed wake so it can't
            # fail the already-committed request.
            def _wake_recipient(rid: str = recipient_id) -> None:
                from ...core import globals as _g
                _g.notify_agent_inbox(rid)

            u.on_commit(_wake_recipient)

        return JSONResponse({
            "success": True,
            "message_id": message_id,
            "message": f"Message sent to {recipient_id}",
        })
    except _MessageStoreFailed as e:
        # SD-R10-1: send() returned None; the uow rolled back and fired
        # no effects, so no audit row / wake survived. Report 500 — never
        # a false 200 for a message that was never stored.
        logger.error(
            "message_repo.send returned None for message %s to %s "
            "(store failed); reporting 500, no audit entry committed",
            e.message_id, recipient_id,
        )
        return JSONResponse(
            {"error": "Failed to send message"}, status_code=500
        )
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except ParentMessageNotFound as e:
        # PF-R32-1: message_repo.send raises ParentMessageNotFound when
        # the reply names a parent_message_id that doesn't exist. Without
        # this branch the swallowed self-FK violation surfaced as a 500;
        # mirror the recipient-not-found shape below and return a clean
        # 404. Roll back so no partial state / audit entry survives.
        if conn:
            conn.rollback()
        logger.warning("Dashboard message rejected (unknown parent): %s", e)
        return JSONResponse(
            {"error": "Parent message not found"}, status_code=404
        )
    except LookupError as e:
        # BL-R13-3: message_repo.send raises LookupError for a recipient
        # that is neither a live agent, a tombstone row, nor the 'admin'
        # label. The canonical MCP send path
        # (send_agent_message_tool_impl) maps this to a clean NotFound /
        # 404; without this branch the same LookupError fell through to
        # the generic handler below and surfaced as an uncaught 500.
        # Roll back so no partial state / audit entry survives.
        if conn:
            conn.rollback()
        logger.warning("Dashboard message rejected (unknown recipient): %s", e)
        return JSONResponse(
            {"error": "Recipient not found"}, status_code=404
        )
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


@router.patch("/{message_id}")
@router.delete("/{message_id}")
async def patch_message_api_route(
    message_id: str,
    request: Request,
    auth: dict = Depends(require_operator_session),
) -> JSONResponse:
    """PATCH/DELETE /api/messages/{message_id}.

    PATCH flips read/delivered. DELETE removes the row (used by the
    dashboard's row-level + bulk delete actions). Both methods share
    this one handler — ``request.method`` still selects the DELETE vs.
    PATCH business logic below (that's a real behavioral branch, not
    wire-shape boilerplate).

    PR D (prancy-napping-pie): auth via ``require_operator_session``.

    arch-r4 #10: ``message_id`` is now a typed path parameter, replacing
    the hand-rolled ``request.url.path.split('/')`` extraction. Stacking
    ``@router.patch`` + ``@router.delete`` on one function registers the
    same handler for both methods (PUT/GET/etc. now 405 at the framework
    level instead of falling into the old ``methods not in (...)`` check).
    """
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
