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
    close_streams_for_agent(agent_id) -> list[str]
    close_streams(session_ids) -> list[str]

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
    """

    session_id: str
    agent_id: str
    opened_at: str
    last_seen_at: str
    bearer_token_hash: str


# In-memory map: session_id → queue used by emitters to push payloads
# at the live SSE writer. NOT persisted on purpose — see module
# docstring's "Runtime layer" note.
_runtime_queues: dict[str, asyncio.Queue[Any]] = {}


# ---------------------------------------------------------------------------
# Profile-review greet tracking (agent self-service profiles, plan §7 PR3).
#
# The event-loop ``profile_review`` section greets an agent ONCE per
# connection (delivers a manager its charter, prompts a blank worker to
# author one), then only re-surfaces when the profile goes overdue.
#
# In this deployment the /mcp transport runs STATELESS
# (``StreamableHTTPSessionManager(stateless=True)`` in ``app.main_app``),
# so the SDK issues NO ``Mcp-Session-Id`` and there is no per-request MCP
# session object to hang the flag on (the plan's §4 "session object caches
# principal" describes a shape this code never had). The real
# per-connection lifecycle object IS the GET /mcp SSE stream registered
# here. We therefore scope the greet to that connection, keyed by
# ``agent_id``: opening a GET /mcp stream (``register_session``) RESETS the
# greet so the agent's next event-loop call re-greets ("dies with the
# connection → every reconnect re-greets", decision 7). A POST-only
# long-poll agent that never opens a GET stream is simply greeted once on
# its first loop call (it never appears here to be reset) — which is the
# same once-per-working-session intent. In-memory only; a backend restart
# clears it (agents reconnect and re-greet).
_profile_greeted_agents: set[str] = set()


def reset_profile_greet(agent_id: str) -> None:
    """Forget that ``agent_id`` was greeted, so its next event-loop call
    re-delivers the ``profile_review`` greet. Called on GET /mcp connect
    (``register_session``) so a reconnect re-greets."""
    _profile_greeted_agents.discard(agent_id)


def mark_profile_greeted(agent_id: str) -> None:
    """Record that ``agent_id`` has received its first-connect greet."""
    _profile_greeted_agents.add(agent_id)


def is_profile_greeted(agent_id: str) -> bool:
    """True iff ``agent_id`` has already been greeted this connection."""
    return agent_id in _profile_greeted_agents


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
    # A (re)connect resets the profile-review greet so the agent's next
    # event-loop call re-delivers it (decision 7). See the module-level
    # ``_profile_greeted_agents`` note.
    reset_profile_greet(agent_id)
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
        # Capture the agent_id before the delete so we can clear its greet
        # state once its last stream is gone (the greet "dies with the
        # connection").
        row = (
            session.query(_McpSession)
            .filter(_McpSession.session_id == session_id)
            .one_or_none()
        )
        closing_agent_id = row.agent_id if row is not None else None
        session.execute(
            _sa_delete(_McpSession).where(_McpSession.session_id == session_id)
        )
        session.commit()
    # Drop the runtime queue too — keeps the in-memory layer in
    # lockstep with the DB row.
    _runtime_queues.pop(session_id, None)
    # If that was the agent's last GET /mcp stream, forget the greet so a
    # fresh connection (or a POST-only reconnect) re-greets.
    if closing_agent_id and not sessions_for_agent(closing_agent_id):
        reset_profile_greet(closing_agent_id)


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
        # Drop the runtime queue, mirroring unregister_session /
        # detach_runtime_queue.
        _runtime_queues.pop(sid, None)
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
    """Drop the runtime queue for `session_id`. Idempotent."""
    _runtime_queues.pop(session_id, None)


def get_runtime_queue(session_id: str) -> Optional[asyncio.Queue[Any]]:
    return _runtime_queues.get(session_id)


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


# ---- revocation teardown (AC-R29-1) -----------------------------------------

# Sentinel enqueued onto a session's runtime queue to wake its GET /mcp
# SSE pump immediately so it re-validates the streaming bearer's
# liveness. Used by terminate/revoke for prompt teardown instead of
# waiting for the pump's next heartbeat self-validation tick. The pump
# identity-compares against this object and never serialises it onto the
# wire.
CLOSE_STREAM = object()


def close_streams(session_ids: Iterable[str]) -> List[str]:
    """Wake every open GET /mcp stream in ``session_ids`` so its pump
    re-validates the bearer now. Returns the session_ids signalled.

    Enqueues :data:`CLOSE_STREAM` onto each attached runtime queue — see
    :func:`close_streams_for_agent` for the pump-side contract. This is
    the explicit-ids sibling: callers whose transaction DELETEs the
    ``mcp_sessions`` row(s) in the same commit that revokes the agent
    (purge's cascade, required by the ``agents.agent_id`` FK) can't rely
    on a post-commit `close_streams_for_agent` — its own DB lookup would
    find nothing once the row is gone. Capture the session_ids via
    :func:`sessions_for_agent` BEFORE the delete, then signal them
    directly here after commit.
    """
    signalled: List[str] = []
    for session_id in session_ids:
        q = _runtime_queues.get(session_id)
        if q is None:
            continue
        try:
            q.put_nowait(CLOSE_STREAM)
        except asyncio.QueueFull:
            continue
        signalled.append(session_id)
    return signalled


def close_streams_for_agent(agent_id: str) -> List[str]:
    """Wake every open GET /mcp stream for ``agent_id`` so its pump
    re-validates the bearer now. Returns the session_ids signalled.

    The pump recognises the :data:`CLOSE_STREAM` sentinel, re-checks
    liveness (cache-only), and — for a terminated / revoked agent —
    tears the stream down immediately rather than after the next
    ≤heartbeat-interval self-validation tick.

    Best-effort: a session whose queue is full isn't signalled here, but
    it is still torn down on its next heartbeat's self-validation check
    (the pump never trusts its open-time auth indefinitely). Rows without
    an attached runtime queue (registered by a prior backend process,
    client not yet reconnected) are skipped — there's no live pump to
    signal, and a reconnect re-derives auth at the gate.

    Looks up ``agent_id``'s sessions fresh from the DB — a caller whose
    own transaction already deleted those rows (e.g. purge's cascade)
    must use :func:`close_streams` with ids captured before the delete
    instead, since this lookup would find nothing post-commit.
    """
    return close_streams(
        handle.session_id for handle in sessions_for_agent(agent_id)
    )
