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
from typing import Any, Dict

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from starlette.requests import Request

from .._dispatch_helpers import (
    _build_route_principal,
    _dispatch_through_tool,
    handle_options,
)
from ._wire_validation import require_str as _require_str
from ..deps import caller_identity, require_operator_session
from ...core.authorize import AuthRejected
from ...core.config import logger
from ...core import globals as g
from ...core.operator_tier import (
    is_confirmed_operator_tier as _shared_is_confirmed_operator_tier,
)
from ...core.tool_result import (
    Conflict,
    Failed,
    Invalid,
    NotFound,
    Ok,
    PermissionDenied,
    ToolResult,
    tool_result_to_http,
)
from ...db.connection import get_db_connection
from ...db.engine import SessionLocal
from ...db.models import ProjectContext
from ...features.dashboard.api import (
    fetch_graph_data_logic,
    fetch_task_tree_data_logic,
)
from ...tools.registry import ToolInputValidationError, dispatch_tool_call
from ...utils.json_utils import get_sanitized_json_body
from .agents import _mcp_presence_for


def _context_row_to_dict(row: Any) -> Dict[str, Any]:
    """Serialise a ``ProjectContext`` ORM row for the dashboard reads.

    ADR-0017 (Wave 12 PR B): project_context is shared project knowledge,
    returned AS-IS — there is no content-based secret redaction. Shared by
    ``/api/all-data`` and ``/api/context-data`` so the two reads never
    drift on serialisation shape.
    """
    return {
        "context_key": row.context_key,
        "value": row.value,
        "updated_at": row.updated_at,
        "updated_by": row.updated_by,
        "created_at": row.created_at,
        "created_by": row.created_by,
        "description": row.description,
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
# up front so a bad value is a clean 400.
#
# arch-r4 #10: ``_require_str`` now lives once in ``._wire_validation``
# (imported above) — the round-9 "kept local, do NOT consolidate" scope
# boundary is settled. This is an import-only change; the
# ``update_task_details_api_route`` handler body (arch-r4 #1's
# just-completed dispatch-through-tool refactor) is untouched.


# ADR-0017 (Wave 12 PR B): ``_context_value_should_redact`` (the
# content-based redaction predicate for project_context dashboard reads)
# is deleted. project_context is shared project knowledge, returned AS-IS.
# ``is_confirmed_operator_tier`` below stays — it still gates the agent
# BEARER-token exposure on /api/all-data + /api/tokens (an authorization
# control on real credentials, not content-guessing).


def is_confirmed_operator_tier(auth: Dict[str, Any]) -> bool:
    """Return True iff ``auth`` came via a CONFIRMED operator-tier path.

    ``require_operator_session`` admits three kinds:

      * ``"operator_bearer"`` — a per-agent bearer that resolved to a
        manager/admin agent row (worker tokens are rejected). Operator
        tier is CONFIRMED.
      * ``"session"`` — cookie operator identity. Since PR #280 the
        per-project backend DOES resolve the caller's ``project_role``
        and ``sysadmin`` flag against router.db (in
        ``app/deps._authorize_session_for_project``); Wave 12 PR A stops
        discarding them and carries them in the auth dict. So a genuine
        cookie OPERATOR (or sysadmin) is now CONFIRMED and reads their
        own project's secrets, while a cookie VIEWER (``project_role ==
        "viewer"``) stays NOT confirmed → still redacted.
      * ``"forwarding"`` — signed-header operator identity. The REST auth
        dict carries no role for this path (the forwarding role rides the
        task-local ``_forwarding_route_role`` carrier consumed by the
        dispatch seam, not this dict), so it passes only ``kind`` and a
        forwarding caller is conservatively NOT confirmed here.

    Endpoints that return agent bearer tokens use this to withhold them
    from the unverifiable-tier paths, closing the viewer→agent token
    disclosure / privilege-escalation surface. A confirmed cookie
    operator, an operator-tier bearer (agent CLI / admin scripts), or a
    sysadmin may read them.

    The policy itself lives in
    ``core/operator_tier.is_confirmed_operator_tier`` so this REST surface
    and the MCP ``tools/admin_tools`` surface cannot drift (they did — see
    that module). This thin adapter maps the auth dict onto the shared
    predicate's keyword fields. Absent keys (forwarding, or a harness path
    that couldn't resolve a role) default to least-privilege in the
    predicate, so a session with ``project_role=None`` stays unconfirmed.
    """
    return _shared_is_confirmed_operator_tier(
        kind=auth.get("kind"),
        sysadmin=auth.get("sysadmin", False),
        project_role=auth.get("project_role"),
    )


# --- Composition reads (cross-resource) ---


@router.get("/status")
async def simple_status_api_route(
    auth: dict = Depends(require_operator_session),
) -> JSONResponse:
    # SECURITY (AZ-R28-1): gated to match the composition router's other
    # reads (node-details / all-data / context-data). AuthHeaderMiddleware
    # gates only /mcp, not /api/*, so without this dep the backend's own
    # (UDS) surface served system status unauthenticated — the direct-UDS
    # defense-in-depth tier PRs #280 / #281 closed on the sibling reads.
    try:
        # PERF/DOS (pentest R4-F1): count via SQL aggregates, never a
        # full-table materialise-then-``len()``. The previous handler
        # read the WHOLE tasks table (``task_repo.list_all()``) AND the
        # WHOLE non-terminal agents set (``get_all_active_agents_from_db``)
        # into Python on every dashboard poll purely to compute three
        # counts. A ``LIMIT`` can't fix a count (it under-counts), so the
        # reads are replaced with ``GROUP BY status`` aggregates on both
        # tables. ``count_by_status`` (all tasks) and
        # ``count_active_by_status`` (non-terminal agents) preserve the
        # exact previous semantics: ``total_*`` is the sum of the grouped
        # counts, and each named count is ``.get(status, 0)``.
        from ...repositories import agent_repo, task_repo

        task_counts = task_repo.count_by_status()
        agent_counts = agent_repo.count_active_by_status()

        return JSONResponse({
            "server_running": True,
            "total_agents": sum(agent_counts.values()),
            "active_agents": agent_counts.get('active', 0),
            "total_tasks": sum(task_counts.values()),
            "pending_tasks": task_counts.get('pending', 0),
            "completed_tasks": task_counts.get('completed', 0),
            "last_updated": datetime.datetime.now().isoformat()
        })
    except Exception as e:
        logger.error(f"Error in simple_status_api_route: {e}", exc_info=True)
        return JSONResponse({"error": "Failed to get simple status."}, status_code=500)


@router.get("/graph-data")
async def graph_data_api_route(
    request: Request,
    auth: dict = Depends(require_operator_session),
) -> JSONResponse:
    # SECURITY (AZ-R28-1): gated to match the sibling composition reads —
    # see simple_status_api_route.
    #
    # ADR-0017 (Wave 12 PR B): project_context node descriptions render
    # AS-IS — no content-based redaction, so no confirmed-operator-tier
    # signal is threaded into the graph builder any more.
    #
    # PERF/DOS (pentest R2-F2): bound the per-section reads with the SAME
    # clamp ``/api/all-data`` uses (``_clamp_section_limit``) so this
    # sibling can't full-table-scan tasks / project_context /
    # agent_actions on every dashboard refresh. ``?limit=`` overrides
    # within ``[1, _ALL_DATA_MAX_LIMIT]``.
    try:
        data = await fetch_graph_data_logic(
            g.file_map.copy(),
            limit=_clamp_section_limit(request),
        )
        return JSONResponse(data)
    except Exception as e:
        logger.error(f"Error serving graph data: {e}", exc_info=True)
        return JSONResponse({'nodes': [], 'edges': [], 'error': 'Failed to serve graph data.'}, status_code=500)


@router.get("/task-tree-data")
async def task_tree_data_api_route(
    request: Request,
    auth: dict = Depends(require_operator_session),
) -> JSONResponse:
    # SECURITY (AZ-R28-1): gated to match the sibling composition reads —
    # see simple_status_api_route.
    #
    # PERF/DOS (pentest R2-F2): bound the task read with the SAME clamp
    # ``/api/all-data`` uses (``_clamp_section_limit``); ``?limit=``
    # overrides within ``[1, _ALL_DATA_MAX_LIMIT]``.
    try:
        data = await fetch_task_tree_data_logic(
            limit=_clamp_section_limit(request)
        )
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
    "created_at",
    "updated_at",
    "terminated_at",
    "current_task",
    "working_directory",
    "color",
    "auto_event_loop",
    "last_event_seen_at",
)


@router.get("/node-details")
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
                # ADR-0017 (Wave 12 PR B): project_context is shared
                # project knowledge — returned AS-IS, no content-based
                # secret redaction.
                details['data'] = dict(row)
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
# The bounded-read clamp (`_ALL_DATA_DEFAULT_LIMIT` / `_ALL_DATA_MAX_LIMIT`
# / `_clamp_section_limit`) now lives in `._read_limits` — its single
# source of truth — so the standalone `/api/tasks` + `/api/agents` list
# reads (pentest R3-F3) can share it without an `agents`→`composition`
# import cycle. Re-imported here (and re-exported) so callers and tests
# that import these names from `composition` keep working unchanged.
from ._read_limits import (  # noqa: E402
    _ALL_DATA_DEFAULT_LIMIT as _ALL_DATA_DEFAULT_LIMIT,  # noqa: F401 (re-export)
    _ALL_DATA_MAX_LIMIT as _ALL_DATA_MAX_LIMIT,  # noqa: F401 (re-export)
    _clamp_section_limit,
)


@router.get("/all-data")
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
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Bound the per-section response so a project with thousands
        # of agents/tasks/file_metadata rows no longer ships an
        # unbounded blob on every dashboard refresh (db review item 2).
        # Default to `_ALL_DATA_DEFAULT_LIMIT`; allow `?limit=` to
        # override within `[1, _ALL_DATA_MAX_LIMIT]` (shared clamp).
        section_limit = _clamp_section_limit(request)

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
        # Global loop toggle, read ONCE for the whole list — a paused
        # fleet reports every agent offline (authoritative Disconnect).
        from ...tools import access as _access
        all_data_global_loop_on = _access._get_config_bool(
            "config_auto_event_loop_global",
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
            # SQLite stores the toggle as 0/1; coerce to a real bool for
            # the typed frontend contract + the presence check below
            # (default ON — column DEFAULT TRUE). The dashboard reads this
            # for the Disconnect/Reconnect affordance + the PAUSED badge.
            agent_dict['auto_event_loop'] = bool(
                1 if agent_dict.get('auto_event_loop') is None
                else agent_dict['auto_event_loop']
            )
            # Wave 7 PR 2 — coordinator transition. Surface presence
            # (online + last_mcp_connection) for the dashboard agents
            # list so the badge can switch from spawn-lifecycle status
            # to live MCP-connection status. Same source as the
            # GET /api/agents endpoint. Authoritative Disconnect: a paused
            # agent (per-agent OFF or global OFF) reads offline.
            agent_dict.update(_mcp_presence_for(
                agent_dict['agent_id'],
                agent_dict.get('last_activity_at'),
                auto_event_loop=agent_dict['auto_event_loop'],
                global_loop_on=all_data_global_loop_on,
            ))
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
            # ADR-0017 (Wave 12 PR B): project_context is shared project
            # knowledge — serialised AS-IS, no content-based secret
            # redaction. (Agent bearer tokens above are still gated by
            # ``expose_tokens`` — that's an authorization control on real
            # credentials, unaffected by this wave.)
            context_data = [_context_row_to_dict(r) for r in ctx_rows]

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


@router.get("/context-data")
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

    SECURITY: gated behind ``require_operator_session`` (any project
    member — viewer or operator). ADR-0017 (Wave 12 PR B): project_context
    is shared project knowledge, returned AS-IS — there is no
    content-based secret redaction. Real secrets belong in the
    operator-only, non-RAG project_settings store (``/api/settings-data``,
    which keeps its own settings-store redaction).

    PERF/DOS (pentest R2-F2): bound the project_context read with the
    SAME clamp ``/api/all-data`` uses (``_clamp_section_limit``) so this
    sibling can't materialise every context row on each dashboard
    refresh. ``?limit=`` overrides within ``[1, _ALL_DATA_MAX_LIMIT]``;
    the ORDER BY + LIMIT runs in SQL so the newest rows survive the
    clamp (not a full-materialise-then-slice).
    """
    section_limit = _clamp_section_limit(request)

    try:
        with SessionLocal() as session:
            rows = (
                session.query(ProjectContext)
                .order_by(ProjectContext.updated_at.desc())
                .limit(section_limit)
                .all()
            )
            # ADR-0017 (Wave 12 PR B): project_context is shared project
            # knowledge — serialised AS-IS, no content-based secret
            # redaction.
            context_data = [_context_row_to_dict(r) for r in rows]

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
@router.post("/terminate-agent")
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


def _update_task_error_detail(result: ToolResult) -> str:
    """Human-readable error string for the legacy ``{"error": ...}``
    envelope this route returns, given a non-``Ok`` ``update_task``
    result. Kept in the legacy envelope (not the shared
    :func:`tool_result_to_http` body) because the dashboard pins this
    shape. STATUS comes from the shared adapter; only the body wording
    lives here.
    """
    if isinstance(result, NotFound):
        return "Task not found"
    if isinstance(result, (Conflict, PermissionDenied)):
        return result.reason
    if isinstance(result, Invalid):
        return result.message
    return "Failed to update task."


@router.api_route("/update-task-dashboard", methods=["POST", "OPTIONS"])
async def update_task_details_api_route(
    request: Request,
    auth: dict = Depends(require_operator_session),
) -> JSONResponse:
    """Dashboard task edit endpoint — thin adapter over the ``update_task``
    MCP tool.

    arch-r4 #1 (arch-deepening round 4): the ~440-line hand-reimplemented
    invariant surface (terminal-sink transition guard, assignability,
    capability-routing parity, ``current_task`` reconcile,
    unassigned-fanout parity, ``task.updated`` publish + assignee wake —
    the BL-R7-1 / BL-R12-1 / BL-R13-1 / BL-R16-1 / BL-R17-1 / BL-R18-1 /
    BL-R30-1 / AZ-R26-1 drift-bug ledger) now lives ONCE in
    :func:`agent_mcp.tools.task_tools.update_task_tool_impl` on the
    unit-of-work, wrapping the SAME ``_update_single_task`` helper
    ``update_task_status`` uses. This handler keeps only the HTTP-wire
    concerns — body sanitization, the SEC-round-9 type-confusion guards,
    and the "at least one editable field" no-op rejection — then
    dispatches and maps the ``ToolResult`` to the legacy response shape.

    Rules (unchanged from the pre-refactor route):
      - task_id required.
      - status is OPTIONAL (status-only updates still supported).
      - At least one editable field must be supplied.
      - assigned_to: <agent_id> assigns; null/empty/'unassigned' clears.

    PR D (prancy-napping-pie): auth via require_operator_session.
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

    try:
        data = await get_sanitized_json_body(request)
    except ValueError as e_val:
        return JSONResponse({"error": str(e_val)}, status_code=400)

    task_id = data.get('task_id')
    if not task_id:
        return JSONResponse({"error": "task_id is a required field."}, status_code=400)

    # The set of recognised editable fields. At least one must be
    # supplied; otherwise the request is a no-op and rejected. Kept as a
    # wire-level check (not delegated to the tool) so the response body
    # stays byte-identical to the pre-refactor route.
    EDITABLE_KEYS = {"status", "title", "description", "priority", "notes", "assigned_to"}
    if not any(k in data for k in EDITABLE_KEYS):
        return JSONResponse(
            {"error": "at least one editable field is required (status, title, description, priority, notes, assigned_to)."},
            status_code=400,
        )

    # SEC round-9: reject structured JSON in the string fields BEFORE
    # dispatch. Without this a dict/list value reaches the tool's own
    # string handling and either raises (500) or is silently coerced —
    # neither is the clean 400 a caller-error deserves. Wire-level input
    # hygiene, kept local per the round-9 scope (the MCP path validates
    # the same via the tool's inputSchema).
    for _field in ("task_id", "status", "title", "description",
                   "priority", "assigned_to"):
        _err = _require_str(data.get(_field), _field)
        if _err is not None:
            return _err

    # dispatch_tool_call's schema-cleaning step (``_clean_arguments_for_
    # schema``) treats ANY top-level ``null`` argument as absent — it
    # strips the key entirely before the tool ever sees it (the Q6e
    # ``token: null`` tolerance rule applies uniformly, not just to
    # ``token``). ``assigned_to: null`` is how this endpoint's caller
    # spells "clear the assignment", which is semantically DIFFERENT
    # from "omit — leave unchanged"; a bare ``None`` here would collapse
    # both into "not supplied" and silently break clearing. Normalize a
    # supplied-but-clearing value to the ``"unassigned"`` sentinel string
    # (which the tool already treats as "clear", alongside "" and a
    # supplied empty string) so the clear intent survives the null-strip.
    assigned_to_arg: Any = None
    if "assigned_to" in data:
        raw_assigned_to = data.get("assigned_to")
        if raw_assigned_to is None or (
            isinstance(raw_assigned_to, str)
            and raw_assigned_to.strip() in ("", "unassigned")
        ):
            assigned_to_arg = "unassigned"
        else:
            assigned_to_arg = raw_assigned_to

    arguments = {
        "task_id": task_id,
        "status": data.get("status"),
        "title": data.get("title"),
        "description": data.get("description"),
        "priority": data.get("priority"),
        "assigned_to": assigned_to_arg,
        "notes": data.get("notes"),
    }

    principal = _build_route_principal(
        bearer_token=None,
        operator_session=True,
        operator_user_id=caller_identity(auth),
    )

    try:
        result = await dispatch_tool_call(
            "update_task", arguments, principal=principal,
        )
    except AuthRejected as e:
        return JSONResponse({"error": e.reason}, status_code=403)
    except ToolInputValidationError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:  # pragma: no cover - defensive
        # SD-R7-1: a raw ``str(e)`` can leak internals; log server-side,
        # return the STATIC generic 500 body this route has always used.
        logger.error(f"Error dispatching update_task: {e}", exc_info=True)
        return JSONResponse(
            {"error": "Failed to update task."}, status_code=500
        )

    if isinstance(result, Ok):
        return JSONResponse(
            {
                "success": True,
                "message": result.message or "Task updated successfully via dashboard.",
            },
            status_code=200,
        )

    status, _ = tool_result_to_http(result)
    if isinstance(result, Failed):
        return JSONResponse({"error": "Failed to update task."}, status_code=status)
    return JSONResponse(
        {"error": _update_task_error_detail(result)}, status_code=status
    )


# --- Test/Demo Data Endpoint ---
@router.post("/create-sample-memories")
async def create_sample_memories_route(
    auth: dict = Depends(require_operator_session),
) -> JSONResponse:
    """Create sample memory entries for testing.

    Wave 8 PR 1 placement: lives on the composition router (not the
    memories router) because the URL is ``/api/create-sample-memories``
    and doesn't match the memories router's ``/api/memories`` prefix.
    URL stability is the constraint; a future PR could migrate to
    ``POST /api/memories/sample`` alongside dashboard updates.

    SECURITY: this route previously had NO auth dep — an unauthenticated
    caller could WRITE project_context rows. ``require_operator_session``
    authenticates the caller.

    R9-F2 (pentest, class-sweep): the route used to write the
    ``project_context`` table ORM-DIRECT, which bypassed the tool-layer
    ``_deny_viewer_tier_write`` gate — the backend dep admits a signed
    VIEWER forwarding header (only the ROUTER proxy 403s viewer
    mutations), so a read-only viewer reaching the backend directly could
    still seed these rows (a RAG-poisoning primitive, same class as the
    memories PUT/DELETE bypass). The write now dispatches through the
    gated ``bulk_update_project_context`` tool, so every project_context
    write surface goes through ONE enforcement path: viewers are denied,
    and the sample keys are attributed to the operator (not a spoofable
    hard-coded ``updated_by``).
    """
    # Sample memory entries — hard-coded, non-``config_*`` keys. Values
    # are the RAW JSON-serialisable objects; the tool ``json.dumps`` them.
    sample_memories = [
        {
            "context_key": "api.config.base_url",
            "context_value": "https://api.example.com",
            "description": "Main API base URL for external services",
        },
        {
            "context_key": "app.settings.theme",
            "context_value": {"theme": "dark", "accent": "blue"},
            "description": "Application theme preferences",
        },
        {
            "context_key": "database.connection.timeout",
            "context_value": 30,
            "description": "Database connection timeout in seconds",
        },
        {
            "context_key": "cache.redis.config",
            "context_value": {"host": "localhost", "port": 6379, "ttl": 3600},
            "description": "Redis cache configuration",
        },
    ]

    principal = _build_route_principal(
        bearer_token=None,
        operator_session=True,
        operator_user_id=caller_identity(auth),
    )

    try:
        result = await dispatch_tool_call(
            "bulk_update_project_context",
            {"updates": sample_memories},
            principal=principal,
        )
    except ToolInputValidationError as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=400)
    except Exception as e:  # pragma: no cover - defensive
        logger.error(f"Error creating sample memories: {e}", exc_info=True)
        return JSONResponse(
            {"success": False, "error": "Failed to create sample memories."},
            status_code=500,
        )

    if isinstance(result, Ok):
        return JSONResponse({
            "success": True,
            "message": f"Created {len(sample_memories)} sample memory entries",
            "created_count": len(sample_memories),
        })

    # Error variants (viewer-tier denial → 403, etc.) via the shared adapter.
    status, body = tool_result_to_http(result)
    return JSONResponse(body, status_code=status)
