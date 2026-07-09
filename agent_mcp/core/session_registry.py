"""SQLite-backed registry of open Streamable HTTP /mcp streams.

Two layers, paired:

* **Persistent layer** — `mcp_sessions` table (Alembic 0004). Rows are
  inserted on GET /mcp open, deleted on disconnect, touched on
  heartbeat. The row is the source of truth for "this stream exists";
  it survives backend restarts so emitters can reason about
  subscribers without depending on volatile in-memory state.

* **Runtime layer** — `_runtime_queues`, an in-memory dict mapping
  `session_id → asyncio.Queue`. This is where fan-out actually
  delivers: the GET /mcp handler attaches its queue on connect, the
  reader task pulls items from the queue and writes them to the live
  SSE stream, the handler detaches on disconnect. Lost across
  restarts on purpose — clients reconnect and the queue is rebuilt
  from scratch.

This separation matters because:

  * If a row exists with no runtime queue (the row was registered by a
    previous backend process and the client hasn't reconnected yet),
    `fanout_to_agent` silently skips it. The data is the source of
    truth — when the client reconnects and re-attaches its queue, it
    catches up by re-reading the underlying tables (`agent_messages`,
    `project_context`, …).

  * If a runtime queue exists with no row (defensive bug-guard), the
    queue is never written to: `fanout_to_*` iterates rows first, then
    looks up queues.

API summary:

    register_session(agent_id, bearer_token) -> session_id
    unregister_session(session_id)
    touch_session(session_id)
    sessions_for_agent(agent_id) -> list[SessionHandle]
    all_sessions() -> list[SessionHandle]
    expire_stale(threshold_seconds=300) -> list[str]   # expired session_ids
    attach_runtime_queue(session_id, asyncio.Queue) -> None
    detach_runtime_queue(session_id) -> None
    get_runtime_queue(session_id) -> asyncio.Queue | None
    fanout_to_agent(agent_id, payload) -> list[str]
    fanout_to_all(payload) -> list[str]

`bearer_token` is never stored raw; we keep `sha256(bearer)` so the
registry stays opaque about credential bytes.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import hashlib
import uuid
from dataclasses import dataclass
from typing import Any, Iterable, List, Optional

from sqlalchemy import delete as _sa_delete, select as _sa_select

from .config import logger
from .principal import Principal
from ..db.engine import get_session
from ..db.models import McpSession as _McpSession


@dataclass(frozen=True)
class SessionHandle:
    """Read-only snapshot of one `mcp_sessions` row.

    The transport layer can fan out without re-querying for fields
    other than `session_id`; the rest are here for diagnostic
    surfaces (operator inspection, audit log) so a caller doesn't
    have to round-trip the DB again to know whose subscription it
    was about to push to.

    Wave 6 PR 0 added ``principal`` — the cached
    :class:`agent_mcp.core.principal.Principal` for this MCP session.
    Built once by ``AuthHeaderMiddleware`` when the GET /mcp stream
    opens and cached via :func:`attach_principal`; reads from
    :func:`get_principal` so the dispatcher can thread the same
    Principal through every tool call that runs against this
    bearer-bound stream without re-deriving identity on each request.
    None when no Principal has been attached yet (e.g. handles
    returned by :func:`sessions_for_agent` / :func:`all_sessions`
    which read from the DB only — the Principal is in-memory by
    design and dies with the process). Handles minted via
    :func:`register_session` start with None too; the attach happens
    explicitly so callers without a Principal in hand don't
    accidentally insert a half-built one.
    """

    session_id: str
    agent_id: str
    opened_at: str
    last_seen_at: str
    bearer_token_hash: str
    principal: Optional[Principal] = None


# In-memory map: session_id → queue used by emitters to push payloads
# at the live SSE writer. NOT persisted on purpose — see module
# docstring's "Runtime layer" note.
_runtime_queues: dict[str, asyncio.Queue[Any]] = {}

# Wave 6 PR 0: in-memory map session_id → Principal. The Principal
# is built once by ``AuthHeaderMiddleware`` at MCP stream open and
# cached here so the per-request dispatcher can thread the same
# Principal through every tool call without re-deriving identity.
# Cleared in :func:`detach_runtime_queue` and :func:`unregister_session`
# so a session id can't outlive its principal.
_runtime_principals: dict[str, Principal] = {}


def _now_utc_iso() -> str:
    """Current UTC time as an ISO-8601 string with explicit timezone.

    `datetime.now(timezone.utc).isoformat()` produces strings like
    `2026-06-02T13:45:09.812345+00:00` that round-trip through
    `datetime.fromisoformat()` without ambiguity — critical for the
    `expire_stale` comparison which subtracts threshold seconds and
    compares as strings.
    """
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _hash_bearer(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def register_session(
    *,
    agent_id: str,
    bearer_token: str,
    alias_used: Optional[str] = None,
) -> str:
    """Insert a row for a new GET /mcp stream, return the minted session_id.

    `session_id` is a fresh UUID4 — independent of any client-provided
    identifier so a misbehaving client can't collide with someone
    else's stream. `bearer_token` is hashed before persisting; the raw
    value never lands on disk.

    `alias_used` (Phase 1c): when the upstream router proxied this
    request from an alias URL it forwards
    `X-Agent-MCP-Alias: <alias_name>,<expires_at>` and the transport
    layer threads `alias_name` through here. The row's `alias_used`
    column then lets operators answer "which alias is still receiving
    traffic" without re-joining against the router-side registry.
    NULL (the default) means the stream was opened on the canonical
    project URL — the common case.
    """
    session_id = uuid.uuid4().hex
    now = _now_utc_iso()
    # PR-G5 cutover: write via the ORM. The DDL-level FK (PR-G1)
    # still rejects rows whose agent_id has no parent in `agents`.
    with get_session() as session:
        session.add(
            _McpSession(
                session_id=session_id,
                agent_id=agent_id,
                opened_at=now,
                last_seen_at=now,
                bearer_token_hash=_hash_bearer(bearer_token),
                alias_used=alias_used,
            )
        )
        session.commit()
    return session_id


def unregister_session(session_id: str) -> None:
    """Remove the row for `session_id`. No-op if the row is gone.

    The transport-level disconnect hook calls this on every close;
    races between heartbeat and disconnect, or duplicate cleanup
    paths, would otherwise crash the request. Silent no-op is the
    safer contract here — the row is GONE either way, and the
    in-memory queue gets dropped by `detach_runtime_queue` regardless.
    """
    with get_session() as session:
        session.execute(
            _sa_delete(_McpSession).where(_McpSession.session_id == session_id)
        )
        session.commit()
    # Drop the runtime queue + cached Principal too — keeps the
    # in-memory layers in lockstep with the DB row.
    _runtime_queues.pop(session_id, None)
    _runtime_principals.pop(session_id, None)


def touch_session(session_id: str) -> None:
    """Bump `last_seen_at` to now. No-op if the row is gone.

    Called by the GET /mcp reader on each heartbeat (and could be
    called on every notification delivery, though that's overkill);
    `expire_stale` uses the column to evict zombie rows whose
    associated stream went away without a clean disconnect.
    """
    with get_session() as session:
        row = (
            session.query(_McpSession)
            .filter(_McpSession.session_id == session_id)
            .one_or_none()
        )
        if row is not None:
            row.last_seen_at = _now_utc_iso()
            session.commit()


def _orm_rows_to_handles(rows: Iterable[Any]) -> List[SessionHandle]:
    return [
        SessionHandle(
            session_id=r.session_id,
            agent_id=r.agent_id,
            opened_at=r.opened_at,
            last_seen_at=r.last_seen_at,
            bearer_token_hash=r.bearer_token_hash,
        )
        for r in rows
    ]


def sessions_for_agent(agent_id: str) -> List[SessionHandle]:
    """Every registered session for `agent_id` (zero or more)."""
    with get_session() as session:
        rows = (
            session.query(_McpSession)
            .filter(_McpSession.agent_id == agent_id)
            .all()
        )
        return _orm_rows_to_handles(rows)


def all_sessions() -> List[SessionHandle]:
    """Every registered session across every agent.

    Used by emitters whose semantics affect every subscriber (e.g.
    `notifications/tools/list_changed` after a worker-policy toggle).
    """
    with get_session() as session:
        rows = session.query(_McpSession).all()
        return _orm_rows_to_handles(rows)


def expire_stale(threshold_seconds: int = 300) -> List[str]:
    """Delete rows whose `last_seen_at` is older than threshold; return ids.

    Designed to be called periodically from a background task (every
    60s is plenty for a 300s threshold). The returned list lets the
    caller log which sessions got reaped for operator visibility.

    The threshold compares ISO-UTC strings lexicographically, which
    works because `_now_utc_iso()` always emits the same fixed-width
    format. We compute the cutoff in Python (rather than in SQL) so
    the SQL itself stays portable — SQLite's `datetime()` arithmetic
    has surprising edge cases around tz-aware vs naive strings.
    """
    cutoff = (
        _dt.datetime.now(_dt.timezone.utc)
        - _dt.timedelta(seconds=threshold_seconds)
    ).isoformat()
    with get_session() as session:
        rows = session.execute(
            _sa_select(_McpSession.session_id).where(
                _McpSession.last_seen_at < cutoff
            )
        ).all()
        expired = [r[0] for r in rows]
        if expired:
            session.execute(
                _sa_delete(_McpSession).where(
                    _McpSession.session_id.in_(expired)
                )
            )
            session.commit()
    for sid in expired:
        # Drop the runtime queue + cached Principal, mirroring
        # unregister_session / detach_runtime_queue — otherwise a
        # stale-reaped session's Principal lingers forever (AC-R5-2).
        _runtime_queues.pop(sid, None)
        _runtime_principals.pop(sid, None)
    return expired


# ---- runtime queue layer ----------------------------------------------------


def attach_runtime_queue(session_id: str, queue: asyncio.Queue[Any]) -> None:
    """Bind `queue` to `session_id` for fan-out delivery.

    The transport's GET /mcp handler calls this right after
    `register_session`; the queue is what its reader-loop pulls from
    to write payloads at the live SSE stream.
    """
    _runtime_queues[session_id] = queue


def detach_runtime_queue(session_id: str) -> None:
    """Drop the runtime queue + cached principal for `session_id`. Idempotent."""
    _runtime_queues.pop(session_id, None)
    _runtime_principals.pop(session_id, None)


def get_runtime_queue(session_id: str) -> Optional[asyncio.Queue[Any]]:
    return _runtime_queues.get(session_id)


def attach_principal(session_id: str, principal: Principal) -> None:
    """Cache ``principal`` against ``session_id``.

    Called from the MCP stream's auth seam right after the bearer
    is verified, so the per-request tool dispatcher can pull the
    same Principal back out without re-deriving identity on each
    request. Cleared in :func:`detach_runtime_queue` /
    :func:`unregister_session` so a session id never outlives its
    principal.
    """
    _runtime_principals[session_id] = principal


def get_principal(session_id: str) -> Optional[Principal]:
    """Return the cached :class:`Principal` for ``session_id`` if any."""
    return _runtime_principals.get(session_id)


def _enqueue_to(handles: Iterable[SessionHandle], payload: Any) -> List[str]:
    delivered: List[str] = []
    for h in handles:
        q = _runtime_queues.get(h.session_id)
        if q is None:
            continue  # row exists but no live queue — client reconnect rebuilds it
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            # Refuse-to-block on a slow consumer; drop and log.
            # If this ever becomes common, switch to a bounded queue
            # with a documented overflow policy (drop-oldest vs reject).
            logger.warning(
                "session_registry: queue full for session %s — dropping payload",
                h.session_id,
            )
            continue
        delivered.append(h.session_id)
    return delivered


def fanout_to_agent(agent_id: str, payload: Any) -> List[str]:
    """Push `payload` onto every runtime queue attached for `agent_id`.

    Returns the list of session_ids that actually received the payload
    (rows without attached queues are silently skipped — see module
    docstring's "Runtime layer" note for why).
    """
    return _enqueue_to(sessions_for_agent(agent_id), payload)


def fanout_to_all(payload: Any) -> List[str]:
    """Push `payload` onto every runtime queue attached anywhere.

    Used by emitters whose semantics aren't agent-scoped — e.g.
    `notifications/tools/list_changed` after a worker-policy toggle
    that affects every worker simultaneously.
    """
    return _enqueue_to(all_sessions(), payload)
