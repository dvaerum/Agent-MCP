"""Composition router — cross-resource reads + legacy verb-y URLs.

Wave 8 PR 1 of prancy-napping-pie hosts two distinct shapes here,
both unified by living on the bare ``/api`` prefix:

Cross-resource reads (genuine composition):
  * ``GET /api/status`` — system status
  * ``GET /api/graph-data`` — agents/tasks/files relationship graph
  * ``GET /api/task-tree-data`` — task tree
  * ``GET /api/node-details`` — node detail panel reads
  * ``GET /api/all-data`` — single-call dashboard hydration blob
  * ``GET /api/context-data`` — memory-only payload (URL doesn't
    match ``/api/memories`` so it stays here, not on the memories
    router)

Legacy verb-y URLs that don't match the semantic resource's prefix:
  * ``POST /api/terminate-agent`` — would semantically belong on
    the agents router, but its URL doesn't match ``/api/agents``.
  * ``POST /api/update-task-dashboard`` — would semantically belong
    on the tasks router, but its URL doesn't match ``/api/tasks``.
  * ``POST /api/create-sample-memories`` — would semantically
    belong on the memories router, but its URL doesn't match
    ``/api/memories``.

URL stability wins; a future PR can migrate these URLs to canonical
per-resource shapes alongside dashboard updates.

Auth: handler-level ``Depends(require_operator_session)`` is kept
verbatim on the handlers that currently have it. The router-level
gate is deferred to a follow-up PR.
"""

from __future__ import annotations

import datetime
import json
from typing import Any, Dict

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from starlette.requests import Request

from .._dispatch_helpers import _dispatch_through_tool, handle_options
from ..deps import caller_identity, require_operator_session
from ...core.config import logger
from ...core import globals as g
from ...db.actions.agent_actions_db import log_agent_action_to_db
from ...db.connection import get_db_connection
from ...db.engine import SessionLocal
from ...db.models import ProjectContext
from ...features.dashboard.api import (
    fetch_graph_data_logic,
    fetch_task_tree_data_logic,
)
from ...tools.project_context_tools import is_secret_key
from ...utils.json_utils import get_sanitized_json_body
from .agents import _mcp_presence_for


#: Placeholder shown in place of a secret project_context value when the
#: caller is not confirmed operator tier. Keeps the key visible (so the
#: dashboard can show that a secret exists) while withholding the value.
_REDACTED_VALUE = "[redacted]"


def _redact_context_row(row: Any, *, redact: bool) -> Dict[str, Any]:
    """Serialise a ``ProjectContext`` ORM row for the dashboard reads,
    blanking BOTH ``value`` AND ``description`` when ``redact`` is True.

    Round-5: the redaction filter (``_context_value_should_redact``)
    inspects the value *and* the description, so a credential pasted into
    either field trips it — and both must therefore be withheld. Masking
    the value alone leaked a secret sitting in the description. The KEY
    stays visible so the dashboard can still show that a secret exists.
    Shared by ``/api/all-data`` and ``/api/context-data`` so the two
    reads never drift on redaction shape.
    """
    return {
        "context_key": row.context_key,
        "value": _REDACTED_VALUE if redact else row.value,
        "updated_at": row.updated_at,
        "updated_by": row.updated_by,
        "created_at": row.created_at,
        "created_by": row.created_by,
        "description": _REDACTED_VALUE if redact else row.description,
    }


router = APIRouter(
    prefix="/api",
    tags=["composition"],
)


# SEC round-9 (type-confusion 400-not-500): the update-task-dashboard
# handler writes the DB directly, bypassing the schema-validating MCP
# tool dispatch. A dict/list in a string field used to reach
# ``task_repo.update_fields`` → a SQLite bind that raises inside the repo,
# is swallowed (returns False), and the handler STILL commits + returns a
# misleading ``200 {"success": true}`` — a silent no-op. Guard each field
# up front so a bad value is a clean 400. Local to this router per scope.
def _require_str(value, field):
    """Return a 400 JSONResponse if ``value`` is present but not a str."""
    if value is not None and not isinstance(value, str):
        return JSONResponse(
            {"error": f"{field} must be a string"}, status_code=400
        )
    return None


def _context_value_should_redact(
    context_key: Any, value: Any, description: Any
) -> bool:
    """True iff a ``project_context`` row must be withheld from a
    non-confirmed-operator caller.

    TWO-part filter, identical to the tool boundary
    (``project_context_tools``) and the RAG surfaces
    (``rag/indexing.py`` / ``rag/query.py``): redact when the KEY name is
    secret OR the VALUE / DESCRIPTION carries an embedded credential
    (round-4 backstop — a secret pasted into a benign-named key). Without
    the value backstop, the dashboard REST reads leaked such a secret to
    every viewer-tier / cookie / forwarding operator (which
    ``is_confirmed_operator_tier`` cannot verify).

    The predicate is imported lazily — exactly as the tool does — to
    avoid the tools <-> rag import cycle. Do NOT reimplement it here.
    """
    if is_secret_key(context_key):
        return True
    from ...features.rag.indexing import _value_has_embedded_secret

    return _value_has_embedded_secret(value, description)


def is_confirmed_operator_tier(auth: Dict[str, Any]) -> bool:
    """Return True iff ``auth`` came via a CONFIRMED operator-tier path.

    ``require_operator_session`` admits three kinds:

      * ``"operator_bearer"`` — a per-agent bearer that resolved to a
        manager/admin agent row (worker tokens are rejected). Operator
        tier is CONFIRMED.
      * ``"session"`` / ``"forwarding"`` — cookie or signed-header
        operator identity. The router admits viewer-tier operators on
        GET requests, and the per-project backend has no router.db
        project-role handle (by design — role gating is the router
        middleware's job; see ``app/deps.py`` + ``main_app`` Principal
        construction). So for these paths the tier is UNVERIFIABLE from
        the backend — it could be a read-only viewer.

    Endpoints that return agent bearer tokens use this to withhold them
    from the unverifiable-tier paths, closing the viewer→agent token
    disclosure / privilege-escalation surface. Operators who need agent
    tokens use the operator-tier bearer path (agent CLI / admin scripts)
    or a dedicated operator-gated endpoint.
    """
    return auth.get("kind") == "operator_bearer"


# --- Composition reads (cross-resource) ---


@router.api_route("/status", methods=["GET", "OPTIONS"])
async def simple_status_api_route(request: Request) -> JSONResponse:
    # Handle OPTIONS for CORS preflight
    if request.method == 'OPTIONS':
        return await handle_options(request)

    try:
        # Get system status
        from ...db.actions.agent_db import get_all_active_agents_from_db
        from ...repositories import task_repo

        agents = get_all_active_agents_from_db()
        # PR #146: route the listing through the class-based
        # TaskRepository so future per-instance hooks (audit, metrics)
        # apply uniformly to dashboard reads too.
        tasks = task_repo.list_all()

        # Count task statuses
        pending_tasks = len([t for t in tasks if t.get('status') == 'pending'])
        completed_tasks = len([t for t in tasks if t.get('status') == 'completed'])

        return JSONResponse({
            "server_running": True,
            "total_agents": len(agents),
            "active_agents": len([a for a in agents if a.get('status') == 'active']),
            "total_tasks": len(tasks),
            "pending_tasks": pending_tasks,
            "completed_tasks": completed_tasks,
            "last_updated": datetime.datetime.now().isoformat()
        })
    except Exception as e:
        logger.error(f"Error in simple_status_api_route: {e}", exc_info=True)
        return JSONResponse({"error": "Failed to get simple status."}, status_code=500)


@router.api_route("/graph-data", methods=["GET", "OPTIONS"])
async def graph_data_api_route(request: Request) -> JSONResponse:
    if request.method == 'OPTIONS':
        return await handle_options(request)
    try:
        data = await fetch_graph_data_logic(g.file_map.copy())
        return JSONResponse(data)
    except Exception as e:
        logger.error(f"Error serving graph data: {e}", exc_info=True)
        return JSONResponse({'nodes': [], 'edges': [], 'error': 'Failed to serve graph data.'}, status_code=500)


@router.api_route("/task-tree-data", methods=["GET", "OPTIONS"])
async def task_tree_data_api_route(request: Request) -> JSONResponse:
    if request.method == 'OPTIONS':
        return await handle_options(request)
    try:
        data = await fetch_task_tree_data_logic()
        return JSONResponse(data)
    except Exception as e:
        logger.error(f"Error serving task tree data: {e}", exc_info=True)
        return JSONResponse({'nodes': [], 'edges': [], 'error': 'Failed to serve task tree data.'}, status_code=500)


#: Safe, non-secret columns to project from ``agents`` for the
#: node-details panel. Excludes ``token`` (the bearer secret — leaking
#: it lets a viewer replay as the agent) and ``aoe_session_id`` (the
#: AoE side-channel session credential). The previous ``SELECT *`` +
#: ``dict(row)`` returned both verbatim. Keep this in sync with the
#: agents model (``agent_mcp/db/models/agent.py``) when columns change.
_AGENT_NODE_SAFE_COLUMNS = (
    "agent_id",
    "status",
    "agent_role",
    "capabilities",
    "created_at",
    "updated_at",
    "terminated_at",
    "current_task",
    "working_directory",
    "color",
    "auto_event_loop",
    "last_event_seen_at",
)


@router.api_route("/node-details", methods=["GET", "OPTIONS"])
async def node_details_api_route(
    request: Request,
    auth: dict = Depends(require_operator_session),
) -> JSONResponse:
    # SECURITY: this endpoint previously had NO auth dependency and, for
    # an ``agent_<id>`` node, returned ``SELECT * FROM agents`` verbatim
    # — including the secret bearer ``token`` column. The router admits
    # viewer-tier operators on GET, so any viewer could harvest an
    # agent's bearer and replay it to escalate to write. The gate below
    # + the safe-column projection in the ``agent`` branch close it.
    if request.method == 'OPTIONS':
        return await handle_options(request)
    node_id = request.query_params.get('node_id')
    if not node_id:
        return JSONResponse({'error': 'Missing node_id parameter'}, status_code=400)
    details: Dict[str, Any] = {'id': node_id, 'type': 'unknown', 'data': {}, 'actions': [], 'related': {}}
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        parts = node_id.split('_', 1)
        node_type_from_id = parts[0] if len(parts) > 1 else node_id
        actual_id_from_node = parts[1] if len(parts) > 1 else (node_id if node_type_from_id != 'admin' else 'admin')
        details['type'] = node_type_from_id
        if node_type_from_id == 'agent':
            # Explicit safe-column projection — NEVER ``SELECT *`` here:
            # the agents table holds the secret bearer ``token`` (and the
            # ``aoe_session_id`` side-channel credential) which must not
            # reach any dashboard client. See ``_AGENT_NODE_SAFE_COLUMNS``.
            _cols = ", ".join(_AGENT_NODE_SAFE_COLUMNS)
            cursor.execute(
                f"SELECT {_cols} FROM agents WHERE agent_id = ?",
                (actual_id_from_node,),
            )
            row = cursor.fetchone()
            if row:
                details['data'] = dict(row)
            cursor.execute("SELECT timestamp, action_type, task_id, details FROM agent_actions WHERE agent_id = ? ORDER BY timestamp DESC LIMIT 10", (actual_id_from_node,))
            details['actions'] = [dict(r) for r in cursor.fetchall()]
            cursor.execute("SELECT task_id, title, status, priority FROM tasks WHERE assigned_to = ? ORDER BY created_at DESC LIMIT 10", (actual_id_from_node,))
            details['related']['assigned_tasks'] = [dict(r) for r in cursor.fetchall()]
        elif node_type_from_id == 'task':
            cursor.execute("SELECT * FROM tasks WHERE task_id = ?", (actual_id_from_node,))
            row = cursor.fetchone()
            if row:
                details['data'] = dict(row)
            cursor.execute("SELECT timestamp, agent_id, action_type, details FROM agent_actions WHERE task_id = ? ORDER BY timestamp DESC LIMIT 10", (actual_id_from_node,))
            details['actions'] = [dict(r) for r in cursor.fetchall()]
        elif node_type_from_id == 'context':
            cursor.execute("SELECT * FROM project_context WHERE context_key = ?", (actual_id_from_node,))
            row = cursor.fetchone()
            if row:
                data = dict(row)
                # SECURITY (round-2/4/5): the router admits viewer-tier
                # operators on GET and the backend can't verify a
                # cookie/forwarding caller's tier, so redact secrets unless
                # the caller is CONFIRMED operator tier. Round-4: redact on
                # the TWO-part filter (secret KEY name OR an embedded
                # credential in the VALUE/DESCRIPTION), matching the tool
                # boundary — not is_secret_key alone, which let a secret
                # pasted into a benign key leak verbatim here. Round-5:
                # blank the DESCRIPTION too, not just the value — the filter
                # scans both fields, so a credential pasted into the
                # description tripped the predicate yet leaked verbatim.
                if not is_confirmed_operator_tier(auth) and (
                    _context_value_should_redact(
                        data.get('context_key'),
                        data.get('value'),
                        data.get('description'),
                    )
                ):
                    data['value'] = _REDACTED_VALUE
                    data['description'] = _REDACTED_VALUE
                details['data'] = data
            cursor.execute("SELECT timestamp, agent_id, action_type FROM agent_actions WHERE (action_type = 'updated_context' OR action_type = 'update_project_context') AND details LIKE ? ORDER BY timestamp DESC LIMIT 5", (f'%"{actual_id_from_node}"%',))
            details['actions'] = [dict(r) for r in cursor.fetchall()]
        elif node_type_from_id == 'file':
            details['data'] = {'filepath': actual_id_from_node, 'info': g.file_map.get(actual_id_from_node, {})}
            cursor.execute("SELECT timestamp, agent_id, action_type, details FROM agent_actions WHERE (action_type LIKE '%_file' OR action_type LIKE 'claim_file_%' OR action_type = 'release_file') AND details LIKE ? ORDER BY timestamp DESC LIMIT 5", (f'%"{actual_id_from_node}"%',))
            details['actions'] = [dict(r) for r in cursor.fetchall()]
        elif node_type_from_id == 'admin':
            details['data'] = {'name': 'Admin User / System'}
            cursor.execute("SELECT timestamp, action_type, task_id, details FROM agent_actions WHERE agent_id = 'admin' ORDER BY timestamp DESC LIMIT 10")
            details['actions'] = [dict(r) for r in cursor.fetchall()]
        if not details.get('data') and node_type_from_id not in ['admin']:
            return JSONResponse({'error': 'Node data not found or type unrecognized'}, status_code=404)
    except Exception as e:
        logger.error(f"Error fetching details for node {node_id}: {e}", exc_info=True)
        return JSONResponse({'error': 'Failed to fetch node details.'}, status_code=500)
    finally:
        if conn:
            conn.close()
    return JSONResponse(details)


# --- Comprehensive Data Endpoint ---
# Default per-section LIMIT for /api/all-data; bounded by the 2026-06-02
# database review (item 2) so a project with thousands of rows no longer
# materialises an unbounded payload on every dashboard refresh. Callers
# that want more can pass `?limit=N`, but `_ALL_DATA_MAX_LIMIT` clamps
# the upper bound to keep the JSON shape sane.
_ALL_DATA_DEFAULT_LIMIT = 500
_ALL_DATA_MAX_LIMIT = 5000


@router.api_route("/all-data", methods=["GET", "OPTIONS"])
async def all_data_api_route(
    request: Request,
    auth: dict = Depends(require_operator_session),
) -> JSONResponse:
    """Get all data in one call for caching on frontend.

    Wave 1 (prancy-napping-pie): cookie-migrated. Before this PR the
    endpoint had NO auth gate — anyone with network reach got the full
    dashboard hydration blob (agents, tasks, context, admin_token) for
    free. The dep accepts cookie / bearer / body-token / query-token
    just like every other migrated route in this file; see
    ``app/deps.py`` for the legacy fallback rationale.

    Wave 3 (prancy-napping-pie) dropped the legacy ``admin_token``
    field from the response and the synthesised ``Admin`` pseudo-agent
    row (which sourced ``auth_token`` from ``g.admin_token``). The
    dashboard no longer reads the field (Wave 2). The synthesised row
    will be replaced by the real ``admin`` row from the agents table
    once Wave 4 drops the admin pseudo-agent entirely.
    """
    if request.method == 'OPTIONS':
        return await handle_options(request)

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Bound the per-section response so a project with thousands
        # of agents/tasks/file_metadata rows no longer ships an
        # unbounded blob on every dashboard refresh (db review item 2).
        # Default to `_ALL_DATA_DEFAULT_LIMIT`; allow `?limit=` to
        # override within `[1, _ALL_DATA_MAX_LIMIT]`.
        try:
            requested_limit = int(request.query_params.get("limit", _ALL_DATA_DEFAULT_LIMIT))
        except (TypeError, ValueError):
            requested_limit = _ALL_DATA_DEFAULT_LIMIT
        section_limit = max(1, min(requested_limit, _ALL_DATA_MAX_LIMIT))

        # SECURITY: agent bearer tokens are only attached for CONFIRMED
        # operator-tier callers. The router admits viewer-tier operators
        # on GET, and the backend cannot verify the tier of a
        # cookie/forwarding caller, so a viewer must never receive an
        # agent's bearer (which they could replay to escalate to write).
        # See ``is_confirmed_operator_tier``.
        expose_tokens = is_confirmed_operator_tier(auth)

        # Build a single agent_id -> active-token map up front so the
        # per-agent token lookup below is O(1) instead of O(n²)
        # (db review item 9). Only populated when tokens may be exposed.
        active_token_by_agent: dict[str, str] = {}
        if expose_tokens:
            for token, data in g.active_agents.items():
                if data.get("status") == "terminated":
                    continue
                ag_id = data.get("agent_id")
                if ag_id and ag_id not in active_token_by_agent:
                    active_token_by_agent[ag_id] = token

        cursor.execute(
            "SELECT * FROM agents ORDER BY created_at DESC LIMIT ?",
            (section_limit,),
        )
        agents_data = []
        for row in cursor.fetchall():
            agent_dict = dict(row)
            # SECURITY: the ``SELECT *`` above pulls the secret bearer
            # ``token`` column. Drop it unconditionally — the canonical
            # (operator-gated) token field is ``auth_token`` below.
            # Leaving the raw column in ``dict(row)`` re-opened the
            # viewer→agent bearer disclosure that ``auth_token`` gating
            # otherwise closes.
            agent_dict.pop('token', None)
            # SECURITY: aoe_session_id is the AoE side-channel session
            # credential. /api/all-data is served to the viewer tier
            # (the router admits viewers on GET), and node-details
            # already strips it via _AGENT_NODE_SAFE_COLUMNS — strip it
            # here too so the two agent-exposing surfaces agree and a
            # viewer can't harvest it. (Operators edit AoE via the
            # operator-gated POST /api/agents/<id>/edit path.)
            agent_dict.pop('aoe_session_id', None)
            # Defensive skip for the legacy 'admin' pseudo-agent row.
            # Wave 4 (migration 0014) deletes it; this filter remains
            # so a partially-upgraded DB (or one with the
            # AGENT_MCP_FK_BYPASS_ORPHAN_CLEANUP escape hatch leaving
            # the row behind) doesn't suddenly surface a stale entry.
            if agent_dict.get('agent_id') == 'admin':
                continue
            # Skip purge-cascade tombstone rows (status='tombstone',
            # agent_id like '[deleted-<original>]'). PR-G1 INSERTs
            # these so agent_messages.{sender_id,recipient_id} FK can
            # be UPDATE'd to point at the tombstone before the
            # original row is DELETE'd. The tombstone row is
            # load-bearing for FKs — it must stay in the DB — but it
            # has no business in the user-facing dashboard list.
            # Without this filter, purge never drops the dashboard's
            # visible agent count (the tombstone takes the place of
            # the deleted row), which silently violates the spec
            # contract "purge drops count by 1". See
            # tests/test_purge_drops_visible_count.py.
            if agent_dict.get('status') == 'tombstone':
                continue
            agent_dict['auth_token'] = active_token_by_agent.get(
                agent_dict['agent_id']
            )
            # Event-coord PR-3 (updated by PR-B / v5.0.24): surface
            # whether this agent currently has any in-flight
            # ``wait_for_events`` long-poll call. Pre-fan-out (PR-2)
            # this read ``g.lock_for(agent_id).locked()`` because
            # only one waiter at a time was permitted. After PR-B the
            # lock is gone — multiple concurrent waiters are
            # supported — so the flag now reflects "≥1 waiter parked"
            # via ``g.waiter_count()``. Dashboard + Settings page
            # consume the same boolean shape; semantics widen from
            # "this single call is in flight" to "at least one call
            # is in flight" without changing the contract.
            agent_dict['wait_for_events_in_flight'] = bool(
                g.waiter_count(agent_dict['agent_id']) > 0
            )
            # Wave 7 PR 2 — coordinator transition. Surface presence
            # (online + last_mcp_connection) for the dashboard agents
            # list so the badge can switch from spawn-lifecycle status
            # to live MCP-connection status. Same source as the
            # GET /api/agents endpoint.
            agent_dict.update(_mcp_presence_for(agent_dict['agent_id']))
            agents_data.append(agent_dict)

        # Wave 3 (prancy-napping-pie): the synthesised 'Admin' agent
        # row (which sourced ``auth_token`` from ``g.admin_token``) is
        # gone. The dashboard's hardcoded ``agent_id === 'Admin'``
        # defensive branches simply never match now — that's fine; they
        # were defensive (skip edit/terminate buttons for the admin
        # row), not load-bearing. Wave 4 deleted the underlying admin
        # pseudo-agent row entirely (migration 0014).
        #
        # Out-of-tree consumers that relied on the synthesised
        # 'Admin' row's ``auth_token`` field to harvest the system
        # token must migrate to per-agent bearer tokens — see
        # ``docs/external-mcp-client.md``. retire-system-token Wave 3
        # deleted the system token; there is no equivalent value.

        cursor.execute(
            "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?",
            (section_limit,),
        )
        tasks_data = [dict(row) for row in cursor.fetchall()]

        # Get bounded context entries via the ORM (Phase 7a; ownership cols 7b).
        with SessionLocal() as ctx_session:
            ctx_rows = (
                ctx_session.query(ProjectContext)
                .order_by(ProjectContext.updated_at.desc())
                .limit(section_limit)
                .all()
            )
            # SECURITY (round-2/4/5): redact secrets for callers that are
            # not CONFIRMED operator tier. ``expose_tokens`` is the same
            # confirmed-operator gate used for agent bearers above; the
            # router admits viewer-tier operators on GET and the backend
            # can't verify a cookie/forwarding caller's tier, so those
            # paths get the redacted view. Mirrors ``/api/context-data``.
            # Round-4: redact on the TWO-part filter (secret KEY OR
            # embedded-secret VALUE/DESCRIPTION) so a credential pasted
            # into a benign key can't leak either. Round-5: the verdict is
            # computed once and blanks BOTH value AND description — the
            # filter scans both, so a secret in the description tripped the
            # predicate yet shipped verbatim when only the value was masked
            # (matches the tool boundary, which drops the whole row).
            context_data = [
                _redact_context_row(
                    r,
                    redact=not expose_tokens
                    and _context_value_should_redact(
                        r.context_key, r.value, r.description
                    ),
                )
                for r in ctx_rows
            ]

        # Recent agent actions: capped at min(100, section_limit). Keeps
        # the pre-existing "last 100" behavior when no `?limit=` is
        # supplied; lets `?limit=N<100` shrink it further.
        actions_cap = min(100, section_limit)
        cursor.execute(
            "SELECT * FROM agent_actions ORDER BY timestamp DESC LIMIT ?",
            (actions_cap,),
        )
        actions_data = [dict(row) for row in cursor.fetchall()]

        # Get bounded file metadata (db review item 2 — was unbounded).
        cursor.execute(
            "SELECT * FROM file_metadata LIMIT ?",
            (section_limit,),
        )
        file_metadata = [dict(row) for row in cursor.fetchall()]

        response_data = {
            "agents": agents_data,
            "tasks": tasks_data,
            "context": context_data,
            "actions": actions_data,
            "file_metadata": file_metadata,
            "file_map": g.file_map,
            # Wave 3 (prancy-napping-pie): ``admin_token`` removed from
            # response. The dashboard no longer reads it (Wave 2).
            # retire-system-token Wave 3 deleted the underlying god-key;
            # external clients use per-agent bearers (see
            # docs/external-mcp-client.md).
            "timestamp": datetime.datetime.now().isoformat()
        }

        # VULN-001 (security audit 2026-06-29): static
        # ``Access-Control-Allow-Origin: *`` headers were dropped from
        # this credentialed endpoint. CORSMiddleware (configured in
        # :func:`agent_mcp.app.main_app.create_app`) now owns CORS
        # response headers and emits the correct per-origin reply
        # against the shared :data:`ALLOWED_ORIGINS` allowlist.
        return JSONResponse(response_data)

    except Exception as e:
        logger.error(f"Error fetching all data: {e}", exc_info=True)
        return JSONResponse({"error": "Failed to fetch all data."}, status_code=500)
    finally:
        if conn:
            conn.close()


@router.api_route("/context-data", methods=["GET", "OPTIONS"])
async def context_data_api_route(
    request: Request,
    auth: dict = Depends(require_operator_session),
) -> JSONResponse:
    """Get only context data.

    URL placement note: this lives on the composition router
    (prefix ``/api``) rather than the memories router (prefix
    ``/api/memories``) because the URL is ``/api/context-data``,
    not ``/api/memories``. URL stability wins; a future PR can
    migrate to ``/api/memories`` GET alongside dashboard updates.

    SECURITY: this route previously had NO auth dep and returned the
    raw project_context — any project member (including a read-only
    viewer) could read ``config_*_token`` / ``config_*_secret`` values.
    Now gated behind ``require_operator_session`` and secret-keyed
    VALUES are redacted for callers that are not CONFIRMED operator
    tier (mirrors ``/api/all-data``'s agent-bearer gate; the router
    admits viewer-tier operators on GET, and the backend can't verify
    the tier of a cookie/forwarding caller — so those paths get the
    redacted view).
    """
    if request.method == 'OPTIONS':
        return await handle_options(request)

    expose_secrets = is_confirmed_operator_tier(auth)

    try:
        with SessionLocal() as session:
            rows = (
                session.query(ProjectContext)
                .order_by(ProjectContext.updated_at.desc())
                .all()
            )
            # Round-4: redact on the TWO-part filter (secret KEY OR an
            # embedded credential in the VALUE/DESCRIPTION), matching the
            # tool boundary and ``/api/all-data`` — is_secret_key alone let
            # a secret pasted into a benign key leak to viewer-tier here.
            # Round-5: ``_redact_context_row`` blanks BOTH value AND
            # description on a redaction verdict — masking the value alone
            # leaked a secret pasted into the description.
            context_data = [
                _redact_context_row(
                    r,
                    redact=not expose_secrets
                    and _context_value_should_redact(
                        r.context_key, r.value, r.description
                    ),
                )
                for r in rows
            ]

        # VULN-001 (security audit 2026-06-29): see /all-data above —
        # static wildcard CORS headers dropped; CORSMiddleware owns
        # the response shape now.
        return JSONResponse(context_data)

    except Exception as e:
        logger.error(f"Error fetching context data: {e}", exc_info=True)
        return JSONResponse({"error": "Failed to fetch context data."}, status_code=500)


# --- Legacy verb-y URLs placed here because the URL prefix doesn't
# --- match the semantic resource's router.


# Thin adapter (Candidate C, 2026-06-02 architecture review): dispatch
# through the `terminate_agent` MCP tool so validation +
# auth-rejection wording cannot drift between the dashboard surface
# and the MCP surface. Wire-shape parity is pinned by
# tests/test_rest_mcp_tool_parity.py.
@router.api_route("/terminate-agent", methods=["POST", "OPTIONS"])
async def terminate_agent_dashboard_api_route(
    request: Request,
    auth: dict = Depends(require_operator_session),
) -> JSONResponse:
    """POST /api/terminate-agent — operator terminates an agent.

    Thin adapter over the ``terminate_agent`` MCP tool. The tool is
    gated by ``@requires_role("operator")``; this handler just
    translates the HTTP request → tool call → JSON response.

    PR D (prancy-napping-pie): auth via ``require_operator_session``.
    Wave 3 (prancy-napping-pie): the inner tool call no longer
    synthesises ``g.admin_token`` as a bearer to satisfy the gate.
    Instead, ``_dispatch_through_tool`` stamps ``operator_session=True``
    on the dispatch — the decorator admits via the operator-session
    ContextVar (the route's outer dep has already validated the
    cookie / legacy bearer / body-token before we get here).

    Wave 8 PR 1 placement: lives on the composition router (not the
    agents router) because the URL is ``/api/terminate-agent`` and
    doesn't match the agents router's ``/api/agents`` prefix. URL
    stability is the constraint; the semantic awkwardness can be
    cleaned up by a future URL-migration PR alongside dashboard
    updates.
    """
    if request.method == 'OPTIONS':
        return await handle_options(request)
    if request.method != 'POST':
        return JSONResponse({"error": "Method not allowed"}, status_code=405)
    try:
        data = await get_sanitized_json_body(request)
    except ValueError as e_val:
        return JSONResponse({"message": str(e_val)}, status_code=400)

    agent_id = data.get("agent_id")
    return await _dispatch_through_tool(
        "terminate_agent",
        {"agent_id": agent_id} if agent_id else {},
        bearer_token=None,
        operator_session=True,
        operator_user_id=caller_identity(auth),
        success_message=(
            f"Agent '{agent_id}' terminated successfully via dashboard API."
            if agent_id else None
        ),
    )


@router.api_route("/update-task-dashboard", methods=["POST", "OPTIONS"])
async def update_task_details_api_route(
    request: Request,
    auth: dict = Depends(require_operator_session),
) -> JSONResponse:
    """Dashboard task edit endpoint.

    Originally required (task_id, status) and used `status` as the
    only mandatory field. The dashboard's Edit modal needs to mutate
    individual fields independently (title-only, priority-only, …)
    and to manage assignment, so the rules are:
      - task_id still required.
      - status is now OPTIONAL (status-only updates still supported).
      - At least one editable field must be supplied (otherwise the
        UPDATE is a no-op and 400 is clearer than success).
      - assigned_to: new field. <agent_id> assigns; null/empty
        string clears the assignment (NULL in DB).

    PR D (prancy-napping-pie): auth moved to require_operator_session.
    The handler no longer reads or verifies an admin token from the
    body; the dep accepts cookie OR Authorization-bearer OR
    legacy body-token paths.

    Wave 8 PR 1 placement: lives on the composition router (not the
    tasks router) because the URL is ``/api/update-task-dashboard``
    and doesn't match the tasks router's ``/api/tasks`` prefix. URL
    stability is the constraint; future PRs can migrate to ``PATCH
    /api/tasks/{task_id}``.
    """
    if request.method == 'OPTIONS':
        return await handle_options(request)
    if request.method != 'POST':
        return JSONResponse({"error": "Method not allowed"}, status_code=405)
    conn = None
    try:
        import sqlite3
        data = await get_sanitized_json_body(request)
        task_id_to_update = data.get('task_id')
        new_status = data.get('status')
        if not task_id_to_update:
            return JSONResponse({"error": "task_id is a required field."}, status_code=400)
        # The set of recognised editable fields. At least one must be
        # supplied; otherwise the request is a no-op and rejected.
        EDITABLE_KEYS = {"status", "title", "description", "priority", "notes", "assigned_to"}
        supplied_editable = [k for k in EDITABLE_KEYS if k in data]
        if not supplied_editable:
            return JSONResponse(
                {"error": "at least one editable field is required (status, title, description, priority, notes, assigned_to)."},
                status_code=400,
            )

        # SEC round-9: reject structured JSON in the string fields BEFORE
        # they reach task_repo.update_fields. Without this a dict/list
        # value raises inside the repo's SQLite bind, is swallowed
        # (returns False), and the handler still commits + returns a
        # misleading 200 success — a silent no-op. ``task_id`` binds into
        # the WHERE clause; ``notes`` is already isinstance-guarded below
        # (non-str notes are ignored, not an error, preserving behaviour).
        for _field in ("task_id", "status", "title", "description",
                       "priority", "assigned_to"):
            _err = _require_str(data.get(_field), _field)
            if _err is not None:
                return _err

        requesting_admin_id = caller_identity(auth)
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT notes, assigned_to, status FROM tasks WHERE task_id = ?", (task_id_to_update,))
        task_row = cursor.fetchone()
        if not task_row:
            return JSONResponse({"error": "Task not found"}, status_code=404)
        existing_notes_str = task_row["notes"]
        # BL-R7-1: capture the PRIOR assignee before the UPDATE so a
        # reassignment can wake the old assignee (task left their queue)
        # in addition to the new one — see the post-commit publish/notify
        # block below.
        prior_assignee = task_row["assigned_to"]

        # BL-R12-1: the ``status`` transition must NOT be direct-written
        # here — that bypassed all four invariants the canonical MCP path
        # (``update_task_status`` tool → ``_update_single_task``) enforces:
        # (1) the terminal-state transition guard (a ``completed`` task
        # could be resurrected to ``in_progress`` behind a misleading
        # 200), (2) ``clear_current_task_for`` (a completed task left
        # ``agents.current_task`` pinned, leaking a stale pointer into
        # ``/api/all-data``), (3) the parent subtask-completion note, and
        # (4) status-enum validation. Route the status change through the
        # SAME tool the MCP wire uses so all four apply uniformly, rather
        # than duplicating the invariant logic inline. Non-status fields
        # (title/description/priority/assigned_to/notes) keep the direct
        # path below. We pre-check the transition with the canonical
        # ``_is_status_transition_allowed`` so an illegal transition gets a
        # clean 409 (the tool reports a rejected transition as an ``Ok``
        # envelope carrying a "Failed …" message → HTTP 200, so the tool
        # alone can't surface the right status code; the DB write is still
        # correctly refused either way).
        if new_status:
            from ...tools.task_tools import _is_status_transition_allowed

            old_status = task_row["status"]
            if not _is_status_transition_allowed(old_status, new_status):
                return JSONResponse(
                    {
                        "error": (
                            f"Invalid status transition: "
                            f"'{old_status}' -> '{new_status}' is not allowed."
                        )
                    },
                    status_code=409,
                )
            status_resp = await _dispatch_through_tool(
                "update_task_status",
                {"task_id": task_id_to_update, "status": new_status},
                bearer_token=None,
                operator_session=True,
                operator_user_id=requesting_admin_id,
            )
            # Enum-invalid status → Invalid → 400; any other non-2xx →
            # propagate verbatim. On success the tool has already applied
            # the status write + current_task clear + parent note.
            if status_resp.status_code not in (200, 201):
                return status_resp

        # PR 7 (Task flip): build the field dict the same way the
        # legacy code built its SET clause, then hand it to
        # task_repo.update_fields(connection=cursor). The repo's
        # _MUTABLE_FIELDS allowlist + JSON-serialisation rule (for
        # `notes`) live in one place now; the route stops carrying
        # them as inline SQL fragments.
        #
        # BL-R12-1: ``status`` is intentionally absent here — it is
        # applied above via the canonical ``update_task_status`` tool.
        fields_to_update: Dict[str, Any] = {}
        log_details: Dict[str, Any] = {}
        if new_status:
            log_details["status_updated_to"] = new_status
        if 'title' in data and data['title'] is not None:
            fields_to_update["title"] = data['title']
            log_details["title_changed"] = True
        if 'description' in data and data['description'] is not None:
            fields_to_update["description"] = data['description']
            log_details["description_changed"] = True
        if 'priority' in data and data['priority']:
            fields_to_update["priority"] = data['priority']
            log_details["priority_changed"] = True
        if 'assigned_to' in data:
            # null / '' / 'unassigned' clears the assignment; any other
            # value is stored verbatim as the agent_id.
            raw_assigned = data['assigned_to']
            new_assigned: Any
            if raw_assigned is None or (isinstance(raw_assigned, str) and raw_assigned.strip() in ('', 'unassigned')):
                new_assigned = None
            else:
                new_assigned = str(raw_assigned).strip()
            # BL-R13-1: a non-empty reassignment target must satisfy the
            # assignability invariant the canonical MCP path enforces
            # (``_update_single_task`` → ``_agent_assignable`` — the agent
            # exists AND is not terminated). Writing ``assigned_to``
            # directly here bypassed it, so a task could be re-pinned on a
            # nonexistent / terminated agent behind a 200. Clearing the
            # assignment (new_assigned is None) stays allowed.
            if new_assigned is not None:
                from ...tools.task_tools import _agent_assignable
                if not _agent_assignable(cursor, new_assigned):
                    return JSONResponse(
                        {
                            "error": (
                                f"Cannot reassign task '{task_id_to_update}' "
                                f"to '{new_assigned}': agent does not exist "
                                f"or is terminated."
                            )
                        },
                        status_code=400,
                    )
            fields_to_update["assigned_to"] = new_assigned
            log_details["assigned_to_changed"] = new_assigned
        if 'notes' in data and data['notes'] and isinstance(data['notes'], str) and data['notes'].strip():
            try:
                current_notes_list = json.loads(existing_notes_str or "[]")
            except json.JSONDecodeError:
                current_notes_list = []
            new_note_entry = {"timestamp": datetime.datetime.now().isoformat(), "author": requesting_admin_id, "content": data['notes'].strip()}
            current_notes_list.append(new_note_entry)
            # The repo serialises list fields with json.dumps internally;
            # pass the Python list directly.
            fields_to_update["notes"] = current_notes_list
            log_details["notes_added"] = True
        if fields_to_update:
            from ...repositories import task_repo as _task_repo
            _task_repo.update_fields(
                task_id_to_update,
                fields_to_update,
                connection=cursor,
            )
        log_agent_action_to_db(cursor, requesting_admin_id, "updated_task_dashboard", task_id=task_id_to_update, details=log_details)
        conn.commit()
        if task_id_to_update in g.tasks:
            cursor.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id_to_update,))
            updated_task_for_cache = cursor.fetchone()
            if updated_task_for_cache:
                g.tasks[task_id_to_update] = dict(updated_task_for_cache)
                for field_key in ["child_tasks", "depends_on_tasks", "notes"]:
                    if isinstance(g.tasks[task_id_to_update].get(field_key), str):
                        try:
                            g.tasks[task_id_to_update][field_key] = json.loads(g.tasks[task_id_to_update][field_key] or "[]")
                        except json.JSONDecodeError:
                            g.tasks[task_id_to_update][field_key] = []
            else:
                del g.tasks[task_id_to_update]
        # BL-R7-1: task_repo.update_fields(connection=cursor) DELIBERATELY
        # defers the cache-write AND the EventBus publish to the caller
        # (the connection= path returns a thin dict, no _publish — a
        # subscriber must never observe an uncommitted / rolled-back row).
        # Every OTHER mutation path reconciles both after commit: REST
        # create publishes task.created + wakes the assignee (tasks.py,
        # round-5 BL-1); MCP update_task_status wakes each touched task's
        # assignee (task_tools.py). Without this, a dashboard edit that
        # reassigns / re-statuses a task never wakes an agent blocked in
        # wait_for_events and never fans resources/updated to /mcp
        # subscribers. Mirror the create path — publish task.updated and
        # wake the assignee — on the successful-commit path only.
        if fields_to_update:
            # Post-update assignee, cache-first (reconciled above) with a
            # DB fallback for tasks not held in g.tasks.
            current_assignee = None
            if task_id_to_update in g.tasks:
                current_assignee = g.tasks[task_id_to_update].get("assigned_to")
            else:
                cursor.execute(
                    "SELECT assigned_to FROM tasks WHERE task_id = ?",
                    (task_id_to_update,),
                )
                fresh_row = cursor.fetchone()
                if fresh_row:
                    current_assignee = fresh_row["assigned_to"]
            from ...core.repositories import _event_bus_shim
            _event_bus_shim.publish(
                current_assignee or "*",
                "task.updated",
                {
                    "task_id": task_id_to_update,
                    "fields": list(fields_to_update.keys()),
                },
            )
            # Wake wait_for_events waiters. The current assignee learns
            # their task changed; on reassignment the prior assignee also
            # learns the task left their queue. Dedupe to avoid a double
            # wake when nothing moved.
            reassigned = "assigned_to" in fields_to_update
            to_wake: list[str] = []
            if current_assignee:
                to_wake.append(current_assignee)
            if reassigned and prior_assignee and prior_assignee != current_assignee:
                to_wake.append(prior_assignee)
            woken: set = set()
            for aid in to_wake:
                if not aid or aid in woken:
                    continue
                try:
                    g.notify_agent_inbox(aid)
                    woken.add(aid)
                except Exception as notify_exc:  # pragma: no cover - defensive
                    logger.warning(
                        "notify_agent_inbox(%s) raised after dashboard "
                        "task edit: %s",
                        aid, notify_exc,
                    )
        return JSONResponse({"success": True, "message": "Task updated successfully via dashboard."})
    except ValueError as e_val:
        return JSONResponse({"error": str(e_val)}, status_code=400)
    except sqlite3.Error as e_sql:
        if conn:
            conn.rollback()
        logger.error(f"DB error updating task via dashboard: {e_sql}", exc_info=True)
        return JSONResponse({"error": "Failed to update task (DB)."}, status_code=500)
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Error updating task via dashboard: {e}", exc_info=True)
        return JSONResponse({"error": "Failed to update task."}, status_code=500)
    finally:
        if conn:
            conn.close()


# --- Test/Demo Data Endpoint ---
@router.api_route("/create-sample-memories", methods=["POST", "OPTIONS"])
async def create_sample_memories_route(
    request: Request,
    auth: dict = Depends(require_operator_session),
) -> JSONResponse:
    """Create sample memory entries for testing.

    Wave 8 PR 1 placement: lives on the composition router (not the
    memories router) because the URL is ``/api/create-sample-memories``
    and doesn't match the memories router's ``/api/memories`` prefix.
    URL stability is the constraint; a future PR could migrate to
    ``POST /api/memories/sample`` alongside dashboard updates.

    SECURITY: this route previously had NO auth dep — an unauthenticated
    caller (or a read-only viewer) could WRITE project_context rows.
    ``require_operator_session`` authenticates AND enforces the
    mutation gate (POST is a mutation method, so viewer-tier callers are
    rejected — only operator tier may write).
    """
    if request.method == 'OPTIONS':
        return await handle_options(request)

    session = SessionLocal()
    try:
        # Sample memory entries
        sample_memories = [
            {
                'context_key': 'api.config.base_url',
                'value': json.dumps('https://api.example.com'),
                'description': 'Main API base URL for external services',
                'updated_by': 'system'
            },
            {
                'context_key': 'app.settings.theme',
                'value': json.dumps({'theme': 'dark', 'accent': 'blue'}),
                'description': 'Application theme preferences',
                'updated_by': 'admin'
            },
            {
                'context_key': 'database.connection.timeout',
                'value': json.dumps(30),
                'description': 'Database connection timeout in seconds',
                'updated_by': 'system'
            },
            {
                'context_key': 'cache.redis.config',
                'value': json.dumps({
                    'host': 'localhost',
                    'port': 6379,
                    'ttl': 3600
                }),
                'description': 'Redis cache configuration',
                'updated_by': 'admin'
            }
        ]

        current_time = datetime.datetime.now().isoformat()
        created_count = 0

        for memory in sample_memories:
            existing = (
                session.query(ProjectContext)
                .filter(ProjectContext.context_key == memory['context_key'])
                .one_or_none()
            )
            if existing is None:
                session.add(
                    ProjectContext(
                        context_key=memory['context_key'],
                        value=memory['value'],
                        created_at=current_time,
                        updated_at=current_time,
                        updated_by=memory['updated_by'],
                        description=memory['description'],
                    )
                )
            else:
                existing.value = memory['value']
                existing.updated_at = current_time
                existing.updated_by = memory['updated_by']
                existing.description = memory['description']
            created_count += 1

        session.commit()

        return JSONResponse({
            "success": True,
            "message": f"Created {created_count} sample memory entries",
            "created_count": created_count
        })

    except Exception as e:
        session.rollback()
        logger.error(f"Error creating sample memories: {e}", exc_info=True)
        return JSONResponse({
            "success": False,
            "error": "Failed to create sample memories."
        }, status_code=500)
    finally:
        session.close()
