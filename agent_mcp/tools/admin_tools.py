# Agent-MCP/mcp_template/mcp_server_src/tools/admin_tools.py
import json
import datetime
import os
import sqlite3
from typing import Dict, Any, Optional

from .registry import register_tool
from ..core.config import logger, AGENT_COLORS  # AGENT_COLORS for register_agent
from ..core import globals as g
from ..core.auth import generate_token  # For register_agent, terminate_agent
# Wave 6 PR 5 — migrated to Principal + ToolResult. The
# ``@requires_role("operator")`` decorator is replaced by an inline
# capability check at the top of each tool (the decorator's wrapper
# signature locks the inner function to
# ``(arguments) -> list[TextContent]`` and can't forward the
# Principal kwarg the dispatcher passes to migrated tools). Wave 9
# PR 3 narrows the check from the role-tier ``has_role("operator")``
# bridge to the per-action capability the tool actually performs
# (``agents.register``, ``agents.terminate``, ``agents.view``,
# ``system.view``) — each tool declares its cap at the call site so
# the gate names the action. Tool visibility in ``tools/list`` is
# still gated by the ``visibility="operator"`` kwarg on each
# ``register_tool(...)`` call below — that's the source of truth
# read by ``tools/access._derive_access_level`` once the decorator
# is gone.
from ..core.principal import Principal
from ..core.operator_tier import (
    is_confirmed_operator_tier as _shared_is_confirmed_operator_tier,
)
from ..core.tool_result import (
    Conflict,
    Failed,
    Invalid,
    NotFound,
    Ok,
    PermissionDenied,
    ToolResult,
)
from ..utils.audit_utils import log_audit
from ..db.unit_of_work import unit_of_work
from ..db.actions.agent_actions_db import log_agent_action_to_db  # For DB logging


_OPERATOR_REQUIRED_REASON = (
    "Operator session or system token required for admin tools."
)


def _require_capability(
    principal: Optional[Principal],
    cap: str,
) -> Optional[PermissionDenied]:
    """Return PermissionDenied iff the caller's principal lacks ``cap``.

    Wave 9 PR 3 — every tool in this module is operator-tier, but the
    Wave 9 capability vocabulary lets each tool name the specific
    action it performs. The helper centralises the failure wording so
    the inline check at the top of each tool reads as one line. Pass
    the action-specific cap (``agents.register``, ``agents.terminate``,
    ``agents.view``, ``system.view``) — the sysadmin wildcard
    short-circuits ``has_capability`` so a sysadmin admits every cap.
    """
    if principal is None or not principal.has_capability(cap):
        return PermissionDenied(reason=_OPERATOR_REQUIRED_REASON)
    return None


def _is_confirmed_operator_tier(principal: Optional[Principal]) -> bool:
    """Return True iff ``principal`` is CONFIRMED operator-tier.

    Thin MCP-side adapter over the shared predicate
    ``core/operator_tier.is_confirmed_operator_tier`` — the single source
    of truth that the REST ``app/routers/composition`` surface also calls,
    so the two cannot drift (they did: a per-agent manager bearer was
    confirmed on REST but masked here, because this copy keyed only on
    ``sysadmin``/``project_role`` and an agent bearer carries neither).

    Confirmed operator tier is a verifiable per-agent manager/admin bearer
    (``kind == "agent_bearer"``), a sysadmin, OR an operator-role project
    member (``project_role == "operator"``). A viewer — even one whose
    group memberships happen to grant an operator-only capability — is NOT
    confirmed: it can pass the coarse capability gate but must still have
    agent tokens withheld, since a bearer it harvests can be replayed to
    re-authenticate as that agent and escalate to write. This is the
    defense-in-depth layer behind the capability gate.
    """
    if principal is None:
        return False
    return _shared_is_confirmed_operator_tier(
        kind=principal.kind,
        sysadmin=principal.sysadmin,
        project_role=principal.project_role,
        agent_role=principal.agent_role,
    )



# --- register_agent tool ---
#
# Mints an agent identity (DB row + bearer token) WITHOUT spawning a
# claude process: agent-mcp does not own the user's claude session.
# The operator hands the returned ready-to-paste ``.mcp.json`` snippet
# to the user, who starts their own ``claude`` process and points it
# at the snippet.
#
# Architectural directive: ``feedback_agent_mcp_coordinator_not_spawner``
# in user memory. Future fixes to runtime code must follow this shape.

_DEFAULT_REGISTER_AGENT_URL_BASE = (
    # Last-resort host used when neither the operator's request body
    # nor ``$AGENT_MCP_EXTERNAL_URL`` told us where this deployment is
    # reachable from. Marked obviously fake so an operator who pastes
    # the snippet realises they need to substitute the real host
    # before it works.
    "https://REPLACE_WITH_YOUR_AGENT_MCP_HOST"
)


def _resolve_snippet_host(arguments: Dict[str, Any]) -> str:
    """Pick the public base URL the ``.mcp.json`` snippet should embed.

    Resolution order (most-specific first):

    1. ``arguments["host"]`` — the dashboard knows its own
       ``window.location.origin`` and ships it explicitly. This is
       the production happy path.
    2. ``$AGENT_MCP_EXTERNAL_URL`` — set on the router service by the
       nix module. The per-project backend doesn't currently read it,
       but if a deployment chooses to thread it through (single-tenant
       mode, future env-plumbing), the snippet builder picks it up.
    3. The placeholder constant — surfaces clearly in copy-paste form
       that the host needs filling in.

    Returns a string without a trailing slash so URL concatenation in
    :func:`_build_mcp_config_snippet` is unambiguous.
    """
    raw = arguments.get("host")
    if isinstance(raw, str) and raw.strip():
        return raw.strip().rstrip("/")
    env_host = os.environ.get("AGENT_MCP_EXTERNAL_URL", "").strip()
    if env_host:
        return env_host.rstrip("/")
    return _DEFAULT_REGISTER_AGENT_URL_BASE


def _resolve_snippet_project(
    arguments: Dict[str, Any],
    principal: Optional[Principal],
) -> Optional[str]:
    """Pick the project name to use in the snippet's URL + key.

    Resolution order:

    1. ``arguments["project_name"]`` — explicit override from the
       dashboard route adapter. The frontend reads this from
       ``projectContext.projectName`` (derived from
       ``window.location.pathname``).
    2. ``principal.project_name`` — set by the router's
       :class:`AuthHeaderMiddleware` when a request arrives via the
       router proxy with a recognised project segment.
    3. None — caller is responsible for treating the snippet as
       project-less (the URL will use a placeholder).

    Returns the project name verbatim (no sanitisation here — the
    upstream router already validated against the project-name
    slug regex before admitting the request).
    """
    raw = arguments.get("project_name")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    if principal is not None and principal.project_name:
        return principal.project_name
    return None


def _build_mcp_config_snippet(
    *,
    project: Optional[str],
    token: str,
    host: str,
) -> str:
    """Return the JSON ``.mcp.json`` snippet operators paste into
    their user's claude config.

    Shape (matches the router's ``_mcp_json_for`` helper at
    ``agent_mcp/router/app.py``, with the addition of a per-project
    server key so multiple Agent-MCP deployments can coexist in one
    ``.mcp.json``)::

        {
          "mcpServers": {
            "agent-mcp-<project>": {
              "type": "http",
              "url": "<host>/agent-mcp/mcp/<project>",
              "headers": {"Authorization": "Bearer <token>"}
            }
          }
        }

    Standalone (no router / single-tenant) deployments where the
    backend is reached directly without a project segment fall back
    to ``agent-mcp`` as the server key and an URL without the
    project component.

    The result is pretty-printed JSON (indent=2) so the modal can
    drop it straight into a ``<pre>`` block.
    """
    if project:
        server_key = f"agent-mcp-{project}"
        url = f"{host}/agent-mcp/mcp/{project}"
    else:
        server_key = "agent-mcp"
        url = f"{host}/mcp"
    snippet = {
        "mcpServers": {
            server_key: {
                "type": "http",
                "url": url,
                "headers": {"Authorization": f"Bearer {token}"},
            }
        }
    }
    return json.dumps(snippet, indent=2)


class _UnitOfWorkAbort(Exception):
    """Internal signal: force a ``unit_of_work()`` rollback while
    carrying the caller-facing :class:`ToolResult` to return.

    Raised inside a uow scope when a write must be undone but the caller
    should still see a specific typed result (e.g. a repo-side
    ``ValueError`` on a duplicate agent name → :class:`Invalid`) rather
    than a generic ``Failed``. The uow rolls back + fires zero effects;
    the outer handler unwraps ``.result``."""

    def __init__(self, result: "ToolResult") -> None:
        super().__init__(getattr(result, "message", "unit-of-work aborted"))
        self.result = result


async def register_agent_tool_impl(
    arguments: Dict[str, Any],
    *,
    principal: Optional[Principal] = None,
) -> ToolResult:
    """Register an agent identity — operator-only. No spawning.

    Inserts a fresh ``agents`` row + mints a bearer token, then
    returns the token alongside a ready-to-paste ``.mcp.json``
    snippet the operator hands to the user. The user is responsible
    for starting their own claude session and pointing it at the
    snippet — agent-mcp never owns the claude process.

    Arguments:
        name: agent_id for the new row. Required. Same slug regex
            ``create_agent`` uses (enforced by ``agent_repo.create``).
            ``agent_id`` is accepted as a back-compat alias so the
            dashboard's existing modal can flip with a one-field
            rename rather than a coordinated frontend+backend change.
        role: ``worker`` or ``manager``. Defaults to ``worker``.
        project_name: project the snippet should point at. Optional;
            falls back to ``principal.project_name`` and finally to a
            placeholder.
        host: public base URL the user's claude reaches the
            deployment at (e.g. ``https://host.tailnet.ts.net``).
            Optional; falls back to ``$AGENT_MCP_EXTERNAL_URL`` and
            then to a placeholder constant.
    """
    denied = _require_capability(principal, "agents.register")
    if denied is not None:
        return denied

    # Accept both the new ``name`` shape (per the Wave 7 plan) and
    # the legacy ``agent_id`` field so the dashboard's existing
    # request body can flow through unchanged during the PR-0 /
    # PR-1 coordination window.
    name = arguments.get("name") or arguments.get("agent_id")
    if not isinstance(name, str) or not name.strip():
        return Invalid(
            field="name",
            message="`name` (agent_id) is required and must be a non-empty string.",
        )
    agent_id = name.strip()

    role = arguments.get("role") or arguments.get("agent_role") or "worker"
    if role not in ("worker", "manager"):
        return Invalid(
            field="role",
            message="`role` must be 'worker' or 'manager'.",
        )

    # Defence-in-depth tombstone-bracket guard. The repo would also
    # catch this via its slug regex; returning a clean Invalid here
    # gives the operator a precise reason instead of a generic
    # regex-mismatch.
    if "[" in agent_id or "]" in agent_id:
        return Invalid(
            field="name",
            message=(
                f"invalid name {agent_id!r}: `[` and `]` are reserved "
                "characters (used by the purge-cascade tombstone format "
                "`[deleted-<id>]`)."
            ),
        )

    # Reserved-name guard (Wave-B). The repo's create() would also
    # reject this (single owner of the invariant) — returning a clean
    # Invalid here gives the operator a precise reason instead of a
    # generic DB-error. Kept in lockstep with
    # ``agent_repo._is_reserved_agent_id``.
    from ..repositories.agent_repository import _is_reserved_agent_id

    if _is_reserved_agent_id(agent_id):
        return Invalid(
            field="name",
            message=(
                f"reserved name {agent_id!r}: names beginning with "
                "'admin' are reserved for privileged / built-in "
                "identities and cannot be assigned to an agent."
            ),
        )

    # Refuse to clobber an existing agent. Mirrors create_agent's
    # in-memory + DB checks so both surfaces give the operator the
    # same wording when they try to re-register a name in use.
    if agent_id in g.agent_working_dirs:
        return Conflict(
            reason=f"Agent '{agent_id}' already exists (in active memory).",
        )

    try:
        # D3: the unit-of-work owns the transaction. The agent INSERT
        # (repo write on ``u.cursor``) and its ``registered_agent``
        # DB-audit row commit atomically; the post-commit cache upsert
        # and the in-memory ``register_agent`` audit are *registered* on
        # ``u`` and flushed only after a clean commit (emit-iff-commit).
        with unit_of_work() as u:
            cursor = u.cursor

            cursor.execute(
                "SELECT agent_id FROM agents WHERE agent_id = ?", (agent_id,)
            )
            if cursor.fetchone():
                return Conflict(
                    reason=f"Agent '{agent_id}' already exists (in database).",
                )

            new_agent_token = generate_token()
            created_at_iso = datetime.datetime.now().isoformat()

            # Working directory mirrors create_agent's "all agents share
            # the project dir" semantics. Wave 7's coordinator model
            # doesn't actually run an agent process here — the
            # working_directory column is informational metadata for
            # dashboards / audit logs.
            project_dir_env = os.environ.get("MCP_PROJECT_DIR")
            if not project_dir_env:
                logger.error(
                    "MCP_PROJECT_DIR not set; register_agent cannot resolve "
                    "the working directory for agent %r.",
                    agent_id,
                )
                return Failed(
                    message="Server configuration error: MCP_PROJECT_DIR not set.",
                )
            agent_working_dir_abs = os.path.abspath(project_dir_env)

            agent_color = AGENT_COLORS[g.agent_color_index % len(AGENT_COLORS)]
            g.agent_color_index += 1

            from ..repositories import agent_repo

            # A duplicate-name / validation failure raises ValueError; the
            # sentinel rolls the uow back (zero effects) and surfaces the
            # caller-facing Invalid unchanged.
            try:
                agent_repo.create(
                    token=new_agent_token,
                    agent_id=agent_id,
                    capabilities=[],
                    status="created",
                    current_task=None,
                    working_directory=agent_working_dir_abs,
                    color=agent_color,
                    agent_role=role,
                    connection=cursor,
                )
            except ValueError as ve:
                raise _UnitOfWorkAbort(
                    Invalid(field="name", message=str(ve))
                )

            actor_label = principal.actor_label() if principal else "operator"

            log_agent_action_to_db(
                cursor,
                actor_label,
                "registered_agent",
                details={
                    "agent_id": agent_id,
                    "role": role,
                },
            )

            # Post-commit cache reconciliation through the repo (mirrors
            # create_agent's pattern — keeps cache + DB in lockstep). The
            # in-memory audit sink keeps its present-tense
            # ``register_agent`` action (pinned by the view_audit_log
            # tests) distinct from the DB sink's ``registered_agent``
            # above — both are registered post-commit so a rolled-back
            # registration leaves neither the cache row nor either audit.
            agent_cache_row = {
                "token": new_agent_token,
                "agent_id": agent_id,
                "capabilities": [],
                "created_at": created_at_iso,
                "status": "created",
                "current_task": None,
                "color": agent_color,
                "working_directory": agent_working_dir_abs,
                "terminated_at": None,
                "updated_at": created_at_iso,
                "agent_role": role,
            }
            u.on_commit(lambda: agent_repo.upsert_cache(agent_cache_row))
            u.on_commit(
                lambda: log_audit(
                    actor_label,
                    "register_agent",
                    {
                        "agent_id": agent_id,
                        "role": role,
                    },
                )
            )

            project_for_snippet = _resolve_snippet_project(arguments, principal)
            host_for_snippet = _resolve_snippet_host(arguments)
            snippet = _build_mcp_config_snippet(
                project=project_for_snippet,
                token=new_agent_token,
                host=host_for_snippet,
            )

            logger.info(
                "Agent %r registered via register_agent (role=%s). No claude "
                "spawned — operator hands the snippet to the user.",
                agent_id, role,
            )

            # Returning here runs the uow __exit__: commit the INSERT +
            # audit row, then flush the cache upsert + in-memory audit.
            return Ok(
                data={
                    "agent_id": agent_id,
                    "token": new_agent_token,
                    "agent_role": role,
                    "mcp_snippet": snippet,
                    "project_name": project_for_snippet,
                },
                message=(
                    f"Agent '{agent_id}' registered. Paste the snippet into "
                    "the user's claude .mcp.json — agent-mcp no longer "
                    "spawns the claude session itself."
                ),
            )

    except _UnitOfWorkAbort as ab:
        return ab.result
    except sqlite3.Error as e_sql:
        # The unit-of-work already rolled back + closed the connection.
        logger.error(
            "Database error registering agent %s: %s",
            agent_id, e_sql, exc_info=True,
        )
        return Failed(message=f"Database error registering agent: {e_sql}")
    except Exception as e:
        logger.error(
            "Unexpected error registering agent %s: %s",
            agent_id, e, exc_info=True,
        )
        return Failed(message=f"Unexpected error registering agent: {e}")


# --- view_status tool ---
# Original logic from main.py: lines 1242-1268 (view_status_tool function)
async def view_status_tool_impl(
    arguments: Dict[str, Any],
    *,
    principal: Optional[Principal] = None,
) -> ToolResult:
    """Report active agents + server status — operator-only."""
    # SECURITY (viewer-read-gating, 2026-07-08): gated on
    # ``system.config.write`` — the operator-only system capability —
    # NOT ``system.view``. ``system.view`` is in the VIEWER project-role
    # bundle (core/capabilities.py::PROJECT_ROLE_BUNDLES), so gating on
    # it let a read-only viewer who called this tool directly over the
    # MCP wire (it's hidden from their tools/list, but visibility is not
    # enforcement) read every agent's status + absolute working
    # directory. ``system.config.write`` is held by operators + sysadmin
    # but not viewers, so it names the operator tier that may see
    # system-level oversight data. Sysadmin admits via the wildcard.
    denied = _require_capability(principal, "system.config.write")
    if denied is not None:
        return denied

    log_audit(
        principal.actor_label() if principal else "operator",
        "view_status",
        {},
    )  # main.py:1249

    # Build agent status from g.active_agents and g.agent_working_dirs (main.py:1251-1259)
    # arch-r5 #7: this iterates the SAME state.active_agents cache that
    # owns "which agents are active" everywhere else (main_app.
    # _bearer_is_active, AgentRepository.active_agent_ids) — a full-row
    # scan, not just the id-set, because the payload below needs
    # per-agent status/task/capabilities/color. Reading the cache
    # directly here (rather than via active_agent_ids(), which only
    # returns ids) still agrees with those callers by construction:
    # one cache, no second query.
    agent_status_dict = {}
    for agent_tkn, agent_data in g.active_agents.items():
        agent_id = agent_data.get("agent_id")
        if agent_id:  # Should always be present if agent_data is valid
            agent_status_dict[agent_id] = {
                "status": agent_data.get("status", "unknown"),
                "current_task": agent_data.get("current_task"),
                "capabilities": agent_data.get("capabilities", []),
                "working_directory": g.agent_working_dirs.get(agent_id, "N/A"),
                "color": agent_data.get(
                    "color", "N/A"
                ),  # Added color from active_agents
            }

    # Server uptime was N/A in original (main.py:1264)
    # We need a server start time global to calculate this, or pass it from app lifecycle.
    # For now, keeping it N/A for 1-to-1.
    server_start_time_iso = (
        g.server_start_time if hasattr(g, "server_start_time") else None
    )
    uptime_str = "N/A"
    if server_start_time_iso:
        uptime_delta = datetime.datetime.now() - datetime.datetime.fromisoformat(
            server_start_time_iso
        )
        uptime_str = str(uptime_delta)

    # Wave 7 PR 3 (coordinator transition): ``tmux_info`` is gone.
    # agent-mcp doesn't own user-side claude processes any more — the
    # liveness signal is "is the bearer currently connected via MCP"
    # (derivable from the session registry by ``view_status`` callers
    # that need it). The legacy dashboard already migrated to that
    # presence-driven view in PR 2; nothing in tree reads the
    # ``tmux_info`` block today.

    status_payload = {  # main.py:1260-1266
        "active_connections": len(
            g.connections
        ),  # g.connections might be managed by SSE transport layer
        "active_agents_count": len(g.active_agents),
        "agents_details": agent_status_dict,
        "server_uptime": uptime_str,
        "file_map_size": len(g.file_map),
        "file_map_preview": {
            k: v for i, (k, v) in enumerate(g.file_map.items()) if i < 5
        },  # Preview first 5
        # Consider adding task counts, DB status, RAG index status etc.
    }

    try:
        status_json = json.dumps(status_payload, indent=2)
    except TypeError as e:
        logger.error(f"Error serializing server status to JSON: {e}")
        return Failed(message=f"Error creating status JSON: {e}")

    return Ok(
        data=status_payload,
        message=f"MCP Server Status:\n{status_json}",
    )


def _reconcile_reassigned_tasks(
    reassigned_tasks: list,
) -> None:
    """Post-commit reconciliation for tasks bulk-unassigned by the
    terminate (here) and purge (``app/routers/agents.py``) cascades.

    ``reassigned_tasks`` is a list of ``(task_id,
    required_capabilities_raw)`` tuples captured BEFORE the bulk
    ``UPDATE tasks SET assigned_to=NULL`` ran.

    Two round-10 findings are addressed per task:

    * **BL-R10-1** — refresh ``g.tasks[task_id]`` from the
      DB-authoritative row so ``view_tasks`` (which iterates the cache)
      shows the task as unassigned instead of pinned to the now-dead
      agent. We UPSERT the fresh row rather than evict it: the task
      still exists (just unassigned), and eviction would make it vanish
      from ``view_tasks`` instead of showing it available.
      ``get_task_by_id`` (the DB free function) is used deliberately —
      ``task_repo.get_by_id`` is cache-first and would hand back the
      stale entry we are trying to replace.
    * **BL-R10-2** — wake every capability-matched worker via
      ``notify_unassigned_task_appeared`` so a live ``wait_for_events``
      waiter picks the task up immediately. (Disconnected workers catch
      up via ``_collect_unassigned_task_events_for``, which keys on
      ``updated_at``.)

    Best-effort per task: a cache/notify failure must never poison the
    reassignment, which is already committed.
    """
    if not reassigned_tasks:
        return
    from ..repositories.task_repository import get_task_by_id
    from ..repositories import task_repo

    for task_id, req_caps_raw in reassigned_tasks:
        try:
            fresh = get_task_by_id(task_id)
            if fresh is not None:
                task_repo.upsert_cache(fresh)
        except Exception as e:  # pragma: no cover - defensive
            logger.warning(
                "cache reconcile of reassigned task %s failed: %s",
                task_id, e,
            )
        try:
            if isinstance(req_caps_raw, str):
                caps_list = json.loads(req_caps_raw or "[]")
            elif req_caps_raw is None:
                caps_list = []
            else:
                caps_list = list(req_caps_raw)
        except Exception:
            caps_list = []
        try:
            g.notify_unassigned_task_appeared(task_id, caps_list)
        except Exception as e:  # pragma: no cover - defensive
            logger.warning(
                "notify_unassigned_task_appeared(%s) failed after "
                "reassignment: %s", task_id, e,
            )


# --- terminate_agent tool ---
# Original logic from main.py: lines 1270-1316 (terminate_agent_tool function)
async def terminate_agent_tool_impl(
    arguments: Dict[str, Any],
    *,
    principal: Optional[Principal] = None,
) -> ToolResult:
    """Soft-terminate an agent (flips status) — operator-only."""
    denied = _require_capability(principal, "agents.terminate")
    if denied is not None:
        return denied

    agent_id_to_terminate = arguments.get("agent_id")

    if not agent_id_to_terminate or not isinstance(agent_id_to_terminate, str):
        return Invalid(
            field="agent_id",
            message="`agent_id` to terminate is required.",
        )

    # Find agent token from in-memory map (main.py:1279-1283)
    found_agent_token: Optional[str] = None
    for tkn, data in g.active_agents.items():
        if data.get("agent_id") == agent_id_to_terminate:
            found_agent_token = tkn
            break

    try:
        # D3: the unit-of-work owns the transaction. The status flip
        # (repo write on ``u.cursor``), the active-task unassign UPDATE,
        # and the ``terminated_agent`` DB-audit row commit atomically;
        # cache eviction, stream teardown, task reconciliation, file
        # release and the in-memory ``terminate_agent`` audit are
        # *registered* on ``u`` and flushed only after a clean commit
        # (emit-iff-commit) — a rollback tears down nothing.
        with unit_of_work() as u:
            cursor = u.cursor

            if not found_agent_token:
                # Check DB if not found in memory (main.py:1285-1290).
                # Exclude tombstone rows (`[deleted-<id>]` purge FK
                # artefacts, BL-R31-3b): a tombstone is not a live agent, so
                # it is not a terminate target — treat it as not-found.
                from ..repositories.agent_repository import LIVE_AGENT_SQL

                cursor.execute(
                    "SELECT token FROM agents WHERE agent_id = ? "
                    f"AND {LIVE_AGENT_SQL}",
                    (agent_id_to_terminate,),
                )
                row = cursor.fetchone()
                if row:
                    # Agent exists in DB but not active memory. Proceed to terminate in DB.
                    logger.warning(
                        f"Agent {agent_id_to_terminate} found in DB (token: {row['token']}) but not in active memory. Proceeding with DB termination."
                    )
                    # We don't have its token to remove from g.active_agents if it's not there.
                else:
                    return NotFound(
                        resource="agent",
                        identifier=agent_id_to_terminate,
                    )

            # PR 6: terminate UPDATE goes through agent_repo with the
            # caller's cursor so it stays atomic with the agent_actions
            # audit-log INSERT below. The repo defers cache eviction +
            # `agent.terminated` publish to the post-commit step.
            from ..repositories import agent_repo
            ok = agent_repo.terminate(
                agent_id_to_terminate, connection=cursor,
            )

            if (
                not ok and not found_agent_token
            ):  # If DB check didn't find it initially and update affected 0 rows
                return NotFound(
                    resource="agent",
                    identifier=agent_id_to_terminate,
                )

            # Wave-B: reconcile tasks so no ACTIVE task is stranded on a
            # terminated agent that will never run it. Terminal tasks
            # (completed/cancelled/failed) keep their attribution —
            # terminate is a soft-delete (the row still exists), and
            # reverting a completed task to unassigned would destroy
            # completion history. Runs on the caller's cursor so it stays
            # atomic with the status flip + audit INSERT. Mirrors the
            # purge-cascade convention in app/routers/agents.py, minus the
            # terminal-status carve-out (purge is a hard delete).
            from ..tools.task_tools import _TERMINAL_TASK_STATUSES

            terminal_placeholders = ",".join("?" * len(_TERMINAL_TASK_STATUSES))
            # BL-R10-1/2: capture the rows we're about to reassign BEFORE the
            # bulk UPDATE so we can reconcile them post-commit. We grab
            # required_capabilities here too — the unassign UPDATE doesn't
            # touch that column and the cache projection drops it, so this is
            # the cheapest place to read it for the capability-matched wake.
            cursor.execute(
                "SELECT task_id, required_capabilities FROM tasks "
                f"WHERE assigned_to = ? AND status NOT IN ({terminal_placeholders})",
                (
                    agent_id_to_terminate,
                    *sorted(_TERMINAL_TASK_STATUSES),
                ),
            )
            reassigned_tasks = [
                (r["task_id"], r["required_capabilities"])
                for r in cursor.fetchall()
            ]
            cursor.execute(
                "UPDATE tasks SET assigned_to = NULL, status = 'unassigned', "
                "updated_at = ? "
                f"WHERE assigned_to = ? AND status NOT IN ({terminal_placeholders})",
                (
                    datetime.datetime.now().isoformat(),
                    agent_id_to_terminate,
                    *sorted(_TERMINAL_TASK_STATUSES),
                ),
            )
            tasks_unassigned = cursor.rowcount
            if tasks_unassigned:
                logger.info(
                    "Unassigned %d active task(s) from terminated agent %s.",
                    tasks_unassigned, agent_id_to_terminate,
                )

            log_agent_action_to_db(
                cursor,
                "admin",
                "terminated_agent",
                details={"agent_id": agent_id_to_terminate},
            )

            actor_label = principal.actor_label() if principal else "operator"

            # Post-commit reconciliation, registered as one ordered hook
            # so the legacy inline post-commit sequence (cache eviction →
            # stream teardown → task reconcile → file release → in-memory
            # audit) fires in exactly its historical order, and only on a
            # clean commit.
            def _post_terminate_effects() -> None:
                # Post-commit cache reconciliation through the repo.
                # Mirrors the manual evictions the legacy code did inline;
                # the repo's `evict_from_cache` handles both the
                # token-keyed and agent_id-keyed maps in lockstep.
                agent_repo.evict_from_cache(
                    agent_id_to_terminate, token=found_agent_token,
                )

                # AC-R29-1: an already-open GET /mcp SSE push stream
                # authenticates its bearer once at open then pumps
                # indefinitely; without an active nudge it would survive
                # revocation until its next heartbeat self-validation
                # tick. Signal every open stream for this agent to
                # re-validate NOW (cache eviction above already made the
                # bearer read as revoked), so teardown is immediate. The
                # pump's own per-heartbeat self-validation is the backstop
                # if a stream can't be signalled here (queue full / not yet
                # reconnected).
                try:
                    from ..core import session_registry

                    closed_streams = session_registry.close_streams_for_agent(
                        agent_id_to_terminate
                    )
                    if closed_streams:
                        logger.info(
                            "Signalled %d open MCP stream(s) to close for "
                            "terminated agent %s.",
                            len(closed_streams), agent_id_to_terminate,
                        )
                except Exception:  # pragma: no cover - defensive
                    logger.warning(
                        "Failed to signal open MCP streams for terminated agent "
                        "%s (pump self-validation will still tear them down).",
                        agent_id_to_terminate, exc_info=True,
                    )

                # BL-R10-1/2: reconcile the tasks we just unassigned —
                # refresh their g.tasks cache entries (so view_tasks stops
                # pinning them to the dead agent) and wake
                # capability-matched workers.
                _reconcile_reassigned_tasks(reassigned_tasks)

                # Release any files held by this agent from g.file_map
                files_released_count = 0
                for filepath, info in list(g.file_map.items()):  # Iterate over a copy
                    if info.get("agent_id") == agent_id_to_terminate:
                        del g.file_map[filepath]
                        files_released_count += 1
                if files_released_count > 0:
                    logger.info(
                        f"Released {files_released_count} files held by terminated agent {agent_id_to_terminate}."
                    )

                # agent-mcp never owns the user's claude process, so
                # terminate is just "revoke the token + flip status". The
                # user's local claude session keeps running until they
                # close it themselves.

                log_audit(
                    actor_label,
                    "terminate_agent",
                    {"agent_id": agent_id_to_terminate},
                )  # main.py:1313
                logger.info(
                    f"Agent '{agent_id_to_terminate}' terminated successfully."
                )

            u.on_commit(_post_terminate_effects)

            # Returning here runs the uow __exit__: commit the status flip
            # + unassign + audit row, then flush _post_terminate_effects.
            return Ok(
                data={
                    "agent_id": agent_id_to_terminate,
                    "status": "terminated",
                },
                message=(
                    f"Agent '{agent_id_to_terminate}' terminated. The token "
                    "is revoked, but your local claude session is still "
                    "running — close it manually if you want it to stop."
                ),
            )

    except sqlite3.Error as e_sql:
        # The unit-of-work already rolled back + closed the connection.
        logger.error(
            f"Database error terminating agent {agent_id_to_terminate}: {e_sql}",
            exc_info=True,
        )
        return Failed(message=f"Database error terminating agent: {e_sql}")
    except Exception as e:
        logger.error(
            f"Unexpected error terminating agent {agent_id_to_terminate}: {e}",
            exc_info=True,
        )
        return Failed(message=f"Unexpected error terminating agent: {e}")


# --- agent lifecycle: restore / edit / purge tools (E2 arch-deepening) ---
#
# `terminate_agent` (above) is a soft-delete: it flips status='terminated'
# but leaves the row + tokens + messages + tasks intact. An operator then
# either Restore (reverse soft-delete) or Purge (hard delete + cascade
# tombstone rewrite). Before E2 these three mutations existed ONLY as
# shadow business-logic tiers inside ``app/routers/agents.py`` — the REST
# route WAS the implementation. E2 extracts them as real tools on the
# unit-of-work; the routes become thin adapters (mirroring E1's
# ``create_task``). Cascade table (purge):
#
#   agents          → DELETE row (last in tx)
#   agent_messages  → tombstone sender_id/recipient_id → [deleted-<id>]
#   tasks           → tombstone created_by; SET NULL assigned_to + status=unassigned
#   agent_actions   → tombstone agent_id
#   mcp_sessions / claude_code_sessions → DELETE (FK to agents.agent_id)
#   tasks.notes JSON → UNTOUCHED — preserved as audit trail
#
# Tombstone format ``[deleted-<id>]`` depends on ``[``/``]`` being absent
# from real agent_ids; see register_agent_tool_impl validation.
#
# Audit sink (D3 finding): all three REST handlers wrote ONLY the DB sink
# (``log_agent_action_to_db`` — ``restored_agent`` / ``edited_agent`` /
# ``purged_agent``) and NEVER the in-memory ``log_audit``. We keep that
# exactly — the DB row is written inside the scope on ``u.cursor`` (NOT
# via ``u.audit``, which would add the in-memory sink they never wrote).


#: Whitelisted editable agent fields for :func:`edit_agent_tool_impl` and
#: the ``POST /api/agents/<id>/edit`` route adapter. ONE source of truth so
#: the route's wire-level type guards and the tool's apply loop can't drift.
#: Anything outside this tuple is silently ignored (defence in depth —
#: status / agent_id / token must never flow through the edit surface).
EDITABLE_AGENT_FIELDS = (
    "capabilities", "color", "working_directory", "aoe_session_id",
    "auto_event_loop", "agent_role",
)


def _purge_tombstone(agent_id: str) -> str:
    """Tombstone literal used to rewrite references to a purged agent."""
    return f"[deleted-{agent_id}]"


def _gather_purge_preview(cursor, agent_id: str) -> Dict[str, Any]:
    """Compute the blast-radius counts + samples for a future purge.

    Message counts + sample go through ``message_repo`` so the repo owns
    the agent_messages query surface. The task / agent_actions counts stay
    on the cursor — they live in tables the message repo doesn't own, and
    the surrounding purge cascade is a multi-table transaction the cursor
    still drives. Shared by :func:`purge_agent_tool_impl` (counts only) and
    the ``GET /api/agents/<id>/purge-preview`` route (counts + samples).
    """
    from ..repositories import message_repo

    messages_sent = message_repo.count_query({"from": agent_id})
    messages_received = message_repo.count_query({"to": agent_id})
    cursor.execute(
        "SELECT COUNT(*) AS n FROM tasks WHERE created_by = ?",
        (agent_id,),
    )
    tasks_created = cursor.fetchone()["n"]
    cursor.execute(
        "SELECT COUNT(*) AS n FROM tasks WHERE assigned_to = ?",
        (agent_id,),
    )
    tasks_assigned = cursor.fetchone()["n"]
    cursor.execute(
        "SELECT COUNT(*) AS n FROM agent_actions WHERE agent_id = ?",
        (agent_id,),
    )
    agent_actions = cursor.fetchone()["n"]

    # Samples (most-recent first; small enough to inline in a modal).
    def _trim(s: "str | None", n: int = 80) -> str:
        if not s:
            return ""
        return s if len(s) <= n else s[:n] + "..."

    sample_messages_sent = [
        {"content": _trim(m["message_content"]),
         "timestamp": m["timestamp"]}
        for m in message_repo.query(
            {"from": agent_id, "limit": 3, "offset": 0}
        )
    ]
    cursor.execute(
        "SELECT title FROM tasks WHERE created_by = ? "
        "ORDER BY created_at DESC LIMIT 3",
        (agent_id,),
    )
    sample_tasks_created = [r["title"] for r in cursor.fetchall()]
    cursor.execute(
        "SELECT title FROM tasks WHERE assigned_to = ? "
        "ORDER BY created_at DESC LIMIT 3",
        (agent_id,),
    )
    sample_tasks_assigned = [r["title"] for r in cursor.fetchall()]

    return {
        "counts": {
            "messages_sent": messages_sent,
            "messages_received": messages_received,
            "tasks_created": tasks_created,
            "tasks_assigned": tasks_assigned,
            "agent_actions": agent_actions,
        },
        "samples": {
            "messages_sent": sample_messages_sent,
            "tasks_created": sample_tasks_created,
            "tasks_assigned": sample_tasks_assigned,
        },
    }


# E2: @requires_capability("agents.terminate"). Restore/edit/purge are all
# operator-tier agent-lifecycle mutations; the locked capability vocabulary
# (core/capabilities.py, 27 entries) carries no ``agents.restore/edit/purge``
# verb, so they gate on ``agents.terminate`` — the agents.* write cap the
# operator bundle carries and the REST routes' ``require_operator_session``
# resolves to. Auth-equivalent, no privilege change (sysadmin wildcards;
# viewer/worker lacks it → PermissionDenied → 403).
async def restore_agent_tool_impl(
    arguments: Dict[str, Any],
    *,
    principal: Optional[Principal] = None,
) -> ToolResult:
    """Reverse a soft-delete: flip status='terminated' → 'created'.

    E2: the CANONICAL restore implementation, shared by the
    ``restore_agent`` MCP tool and ``POST /api/agents/<id>/restore``.
    Side effects of the original terminate (cleared current_task,
    released files) are NOT undone — the operator reassigns work
    explicitly. On the unit-of-work: the two field clears (status,
    terminated_at) + the ``restored_agent`` DB audit row commit in ONE
    transaction on ``u.cursor``; the ``g.active_agents`` /
    ``g.agent_working_dirs`` cache rebuild registers post-commit
    (emit-iff-commit — a rollback re-adds nothing).
    """
    denied = _require_capability(principal, "agents.terminate")
    if denied is not None:
        return denied

    agent_id = arguments.get("agent_id")
    if not agent_id or not isinstance(agent_id, str):
        return Invalid(field="agent_id", message="`agent_id` is required.")

    actor_label = principal.actor_label() if principal else "operator"

    try:
        with unit_of_work() as u:
            cursor = u.cursor
            cursor.execute(
                "SELECT token, status FROM agents WHERE agent_id = ?",
                (agent_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return NotFound(resource="agent", identifier=agent_id)
            if row["status"] != "terminated":
                return Conflict(
                    reason=(
                        f"Agent '{agent_id}' is not terminated "
                        f"(status={row['status']!r}); nothing to restore"
                    ),
                )

            agent_token = row["token"]
            # PR 6 + PR 8: restore goes through agent_repo.update_field with
            # the caller's cursor — atomic with the audit INSERT below.
            # update_field takes one field at a time, so two calls.
            from ..repositories import agent_repo

            agent_repo.update_field(
                agent_id, "status", "created", connection=cursor,
            )
            agent_repo.update_field(
                agent_id, "terminated_at", None, connection=cursor,
            )
            log_agent_action_to_db(
                cursor, actor_label, "restored_agent",
                details={"agent_id": agent_id},
            )

            # Read the (uncommitted, same-connection) restored row so the
            # post-commit cache rebuild uses the fresh values.
            cursor.execute(
                "SELECT agent_id, capabilities, created_at, status, color, "
                "working_directory, terminated_at, updated_at, current_task, "
                "agent_role "
                "FROM agents WHERE agent_id = ?",
                (agent_id,),
            )
            full = cursor.fetchone()

            def _restore_cache() -> None:
                # Re-add to the in-memory active map so the dashboard sees
                # the restored agent again.
                if full is not None:
                    try:
                        caps = json.loads(full["capabilities"] or "[]")
                    except (TypeError, json.JSONDecodeError):
                        caps = []
                    # SECURITY (terminate-revocation, related): rebuild the
                    # FULL cache row including agent_role. Omitting it made a
                    # restored manager transiently resolve to worker
                    # capabilities (a privilege downgrade) until reload.
                    g.active_agents[agent_token] = {
                        "token": agent_token,
                        "agent_id": full["agent_id"],
                        "capabilities": caps,
                        "created_at": full["created_at"],
                        "status": full["status"],
                        "color": full["color"],
                        "working_directory": full["working_directory"],
                        "terminated_at": full["terminated_at"],
                        "updated_at": full["updated_at"],
                        "current_task": full["current_task"],
                        "agent_role": full["agent_role"],
                    }
                    # BL-R13-2: working_directory has a SECOND in-memory view
                    # (g.agent_working_dirs, keyed by agent_id) that
                    # get_working_directory() reads first. Mirror the
                    # edit-path reconcile for the restored agent.
                    g.agent_working_dirs[agent_id] = full["working_directory"]

            u.on_commit(_restore_cache)

            return Ok(
                data={"agent_id": agent_id, "status": "created"},
                message=f"Agent '{agent_id}' restored",
            )
    except sqlite3.Error as e_sql:
        logger.error(
            "Database error restoring agent %s: %s", agent_id, e_sql,
            exc_info=True,
        )
        return Failed(message=f"Database error restoring agent: {e_sql}")
    except Exception as e:
        logger.error(
            "Unexpected error restoring agent %s: %s", agent_id, e,
            exc_info=True,
        )
        return Failed(message=f"Unexpected error restoring agent: {e}")


async def edit_agent_tool_impl(
    arguments: Dict[str, Any],
    *,
    principal: Optional[Principal] = None,
) -> ToolResult:
    """Update mutable agent fields — operator-tier.

    E2: the CANONICAL edit implementation, shared by the ``edit_agent``
    MCP tool and ``POST /api/agents/<id>/edit``. Accepts any combination
    of :data:`EDITABLE_AGENT_FIELDS`; non-whitelisted keys are ignored
    (status / agent_id / token have their own flows). On the
    unit-of-work: each field update (repo write on ``u.cursor``) + the
    ``edited_agent`` DB audit row commit atomically; the ``g.active_agents``
    / ``g.agent_working_dirs`` cache refresh and the ``auto_event_loop``
    wake register post-commit (emit-iff-commit).

    Wire-level type guards + the ``agent_role`` 422 + ``aoe_session_id``
    format normalisation live in the REST adapter (they carry non-standard
    HTTP statuses / body wording the dashboard pins); by dispatch time the
    values are already validated. The tool's inputSchema validates the same
    fields on the MCP path.
    """
    denied = _require_capability(principal, "agents.terminate")
    if denied is not None:
        return denied

    agent_id = arguments.get("agent_id")
    if not agent_id or not isinstance(agent_id, str):
        return Invalid(field="agent_id", message="`agent_id` is required.")

    updates = {
        k: arguments[k] for k in EDITABLE_AGENT_FIELDS if k in arguments
    }
    if not updates:
        return Invalid(
            message=(
                "No editable fields supplied. Accepts any of: "
                + ", ".join(EDITABLE_AGENT_FIELDS)
            ),
        )
    if "agent_role" in updates and updates["agent_role"] not in (
        "worker", "manager",
    ):
        return Invalid(
            field="agent_role",
            message=(
                f"Invalid agent_role {updates['agent_role']!r}: "
                "must be 'worker' or 'manager'."
            ),
        )

    actor_label = principal.actor_label() if principal else "operator"

    try:
        with unit_of_work() as u:
            cursor = u.cursor
            cursor.execute(
                "SELECT token, status FROM agents WHERE agent_id = ?",
                (agent_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return NotFound(resource="agent", identifier=agent_id)

            # PR 6: field updates route through agent_repo with the caller's
            # cursor so each update + the audit INSERT land in one txn. A
            # None return means the field write failed — abort (roll back
            # the partial) and surface Failed.
            from ..repositories import agent_repo

            applied: Dict[str, Any] = {}
            for field, value in updates.items():
                # aoe_session_id clear sentinel: the REST adapter normalises
                # the clear case to ``""`` (``None`` would be stripped by the
                # dispatch layer); a direct MCP caller may also pass ``""``.
                # Store NULL in the column either way.
                if field == "aoe_session_id" and value == "":
                    value = None
                result = agent_repo.update_field(
                    agent_id, field, value, connection=cursor,
                )
                if result is None:
                    raise _UnitOfWorkAbort(
                        Failed(message=f"Failed to update field {field!r}")
                    )
                applied[field] = value

            agent_token = row["token"]

            log_agent_action_to_db(
                cursor, actor_label, "edited_agent",
                details={"agent_id": agent_id, "fields": list(applied.keys())},
            )

            def _edit_cache() -> None:
                # Refresh the in-memory active entry so the dashboard sees
                # the new color/capabilities without a restart.
                if agent_token in g.active_agents:
                    for field, value in applied.items():
                        g.active_agents[agent_token][field] = value
                # BL-R11-1: working_directory's second view (g.agent_working_dirs).
                if "working_directory" in applied:
                    g.agent_working_dirs[agent_id] = applied["working_directory"]
                # PR-2 event-coord: wake in-flight wait_for_events so the
                # agent re-evaluates the flag state.
                if "auto_event_loop" in applied:
                    try:
                        g.wake_for_flag_recheck(agent_id)
                    except Exception as e:  # pragma: no cover - defensive
                        logger.warning(
                            "wake_for_flag_recheck(%s) failed after toggle: %s",
                            agent_id, e,
                        )

            u.on_commit(_edit_cache)

            return Ok(
                data={"agent_id": agent_id, "updated": applied},
                message=(
                    f"Agent '{agent_id}' updated: " + ", ".join(applied.keys())
                ),
            )
    except _UnitOfWorkAbort as ab:
        return ab.result
    except sqlite3.Error as e_sql:
        logger.error(
            "Database error editing agent %s: %s", agent_id, e_sql,
            exc_info=True,
        )
        return Failed(message=f"Database error editing agent: {e_sql}")
    except Exception as e:
        logger.error(
            "Unexpected error editing agent %s: %s", agent_id, e,
            exc_info=True,
        )
        return Failed(message=f"Unexpected error editing agent: {e}")


async def purge_agent_tool_impl(
    arguments: Dict[str, Any],
    *,
    principal: Optional[Principal] = None,
) -> ToolResult:
    """Hard-delete an agent + cascade-tombstone every reference.

    E2: the CANONICAL purge implementation, shared by the ``purge_agent``
    MCP tool and ``DELETE /api/agents/<id>?cascade=true``. The whole
    6-table cascade (tombstone insert → agent_messages / tasks /
    agent_actions rewrites → session-table deletes → ``purged_agent`` DB
    audit → agents-row DELETE, LAST) runs as ONE atomic transaction on
    ``u.cursor``. Before E2 this was an explicit ``BEGIN``/``COMMIT`` block
    in the router; the unit-of-work now owns the transaction. In-memory
    reference drops + the reassigned-task reconcile register post-commit
    (emit-iff-commit — a rollback tombstones nothing).

    The ``?cascade=true`` confirmation gate is a wire-level safety kept in
    the REST adapter (refuse a bare DELETE); a direct MCP call to this
    operator-tier tool is already a deliberate purge.
    """
    denied = _require_capability(principal, "agents.terminate")
    if denied is not None:
        return denied

    agent_id = arguments.get("agent_id")
    if not agent_id or not isinstance(agent_id, str):
        return Invalid(field="agent_id", message="`agent_id` is required.")

    actor_label = principal.actor_label() if principal else "operator"

    try:
        with unit_of_work() as u:
            cursor = u.cursor
            cursor.execute(
                "SELECT token FROM agents WHERE agent_id = ?", (agent_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return NotFound(resource="agent", identifier=agent_id)
            agent_token = row["token"]

            # Snapshot counts before tombstoning so the response reflects
            # what we actually rewrote.
            counts = _gather_purge_preview(cursor, agent_id)["counts"]
            tombstone = _purge_tombstone(agent_id)

            from ..repositories import agent_repo
            from ..repositories import message_repo

            # PR-G1: agent_messages.{sender_id,recipient_id} FK to
            # agents.agent_id, so the tombstone `[deleted-<id>]` must exist
            # as an agents row before any rewrite. INSERT OR IGNORE so a
            # re-purge is a no-op; token namespaced under `__tombstone_`.
            agent_repo.insert_tombstone(
                token=f"__tombstone_{agent_id}",
                tombstone_agent_id=tombstone,
                connection=cursor,
            )
            message_repo.rename_participant(
                agent_id, tombstone, connection=cursor,
            )
            cursor.execute(
                "UPDATE tasks SET created_by = ? WHERE created_by = ?",
                (tombstone, agent_id),
            )
            # Reassignment: ACTIVE tasks assigned to this agent become
            # unassigned (operator reassigns). BL-R17-2: carve out TERMINAL
            # tasks — reverting a finished task resurrects done work. Purge
            # is a HARD delete (agents row DELETEd below) and
            # tasks.assigned_to is an FK, so terminal tasks get a SEPARATE
            # UPDATE that NULLs the dangling ref while KEEPING terminal
            # status (no resurrection, no notify).
            # BL-R10-1/2: capture affected rows (+ required_capabilities)
            # BEFORE the UPDATE for the post-commit cache reconcile + wake;
            # bump updated_at so the catch-up feed surfaces the transition.
            from ..tools.task_tools import _TERMINAL_TASK_STATUSES

            terminal_placeholders = ",".join(
                "?" * len(_TERMINAL_TASK_STATUSES)
            )
            terminal_params = sorted(_TERMINAL_TASK_STATUSES)
            cursor.execute(
                "SELECT task_id, required_capabilities FROM tasks "
                "WHERE assigned_to = ? "
                f"AND status NOT IN ({terminal_placeholders})",
                (agent_id, *terminal_params),
            )
            reassigned_tasks = [
                (r["task_id"], r["required_capabilities"])
                for r in cursor.fetchall()
            ]
            now_iso = datetime.datetime.now().isoformat()
            cursor.execute(
                "UPDATE tasks SET assigned_to = NULL, status = 'unassigned', "
                "updated_at = ? WHERE assigned_to = ? "
                f"AND status NOT IN ({terminal_placeholders})",
                (now_iso, agent_id, *terminal_params),
            )
            cursor.execute(
                "UPDATE tasks SET assigned_to = NULL, updated_at = ? "
                "WHERE assigned_to = ? "
                f"AND status IN ({terminal_placeholders})",
                (now_iso, agent_id, *terminal_params),
            )
            cursor.execute(
                "UPDATE agent_actions SET agent_id = ? WHERE agent_id = ?",
                (tombstone, agent_id),
            )
            # AC-R29-1 symmetry (SEC-B/F3): snapshot this agent's open
            # `mcp_sessions` ids BEFORE the cascade DELETE below removes
            # them — `session_registry.close_streams_for_agent` looks
            # rows up fresh from the DB, so calling it post-commit (after
            # the delete below has committed) would find nothing and
            # silently signal no one. `_purge_post_commit` uses this
            # captured list with `close_streams` instead.
            from ..core import session_registry

            purge_session_ids = [
                h.session_id
                for h in session_registry.sessions_for_agent(agent_id)
            ]

            # BL-R4-2: mcp_sessions / claude_code_sessions FK agents.agent_id;
            # a session row still referencing this agent at DELETE time makes
            # the final DELETE FROM agents raise FOREIGN KEY constraint
            # failed. A purged agent's sessions are dead — DELETE them here,
            # same txn, BEFORE the agents-row delete. Guarded on table
            # presence for older schemas that predate them.
            for _session_table in ("mcp_sessions", "claude_code_sessions"):
                cursor.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type = 'table' AND name = ?",
                    (_session_table,),
                )
                if cursor.fetchone() is not None:
                    cursor.execute(
                        f"DELETE FROM {_session_table} WHERE agent_id = ?",
                        (agent_id,),
                    )
            # Audit the purge BEFORE the agent row disappears so the action
            # log has a non-tombstoned 'purged_agent' entry.
            log_agent_action_to_db(
                cursor, actor_label, "purged_agent",
                details={
                    "agent_id": agent_id,
                    "tombstone": tombstone,
                    "counts": counts,
                },
            )
            # DELETE the agents row LAST (PR 8: through agent_repo.delete
            # with the caller's cursor).
            agent_repo.delete(agent_id, connection=cursor)

            def _purge_post_commit() -> None:
                # Drop in-memory references.
                if agent_token in g.active_agents:
                    del g.active_agents[agent_token]
                if agent_id in g.agent_working_dirs:
                    del g.agent_working_dirs[agent_id]
                for filepath, info in list(g.file_map.items()):
                    if info.get("agent_id") == agent_id:
                        del g.file_map[filepath]

                # AC-R29-1 symmetry (SEC-B/F3): purge, like terminate, drops
                # this agent's bearer from `active_agents` above — an
                # already-open GET /mcp SSE stream would otherwise survive
                # until its next heartbeat self-validation tick (up to
                # `_HEARTBEAT_INTERVAL_SECONDS`). Signal every open stream
                # for this agent to re-validate NOW so teardown is
                # immediate, mirroring `_post_terminate_effects` above. Uses
                # `close_streams` with the ids captured BEFORE the cascade
                # DELETE (not `close_streams_for_agent`, which re-queries
                # `mcp_sessions` and would find nothing — purge already
                # deleted those rows in the same committed transaction).
                # The pump's own per-heartbeat self-validation is the
                # backstop if a stream can't be signalled here.
                try:
                    from ..core import session_registry

                    closed_streams = session_registry.close_streams(
                        purge_session_ids
                    )
                    if closed_streams:
                        logger.info(
                            "Signalled %d open MCP stream(s) to close for "
                            "purged agent %s.",
                            len(closed_streams), agent_id,
                        )
                except Exception:  # pragma: no cover - defensive
                    logger.warning(
                        "Failed to signal open MCP streams for purged agent "
                        "%s (pump self-validation will still tear them down).",
                        agent_id, exc_info=True,
                    )

                # BL-R10-1/2: reconcile reassigned tasks' cache + wake
                # capability-matched workers (shared with the terminate path).
                _reconcile_reassigned_tasks(reassigned_tasks)

            u.on_commit(_purge_post_commit)

            return Ok(
                data={
                    "agent_id": agent_id,
                    "tombstone": tombstone,
                    "counts": counts,
                },
                message=f"Agent '{agent_id}' purged",
            )
    except sqlite3.Error as e_sql:
        logger.error(
            "Database error purging agent %s: %s", agent_id, e_sql,
            exc_info=True,
        )
        return Failed(message=f"Database error purging agent: {e_sql}")
    except Exception as e:
        logger.error(
            "Unexpected error purging agent %s: %s", agent_id, e,
            exc_info=True,
        )
        return Failed(message=f"Unexpected error purging agent: {e}")


# --- view_audit_log tool ---
# Original logic from main.py: lines 1387-1408 (view_audit_log_tool function)
async def view_audit_log_tool_impl(
    arguments: Dict[str, Any],
    *,
    principal: Optional[Principal] = None,
) -> ToolResult:
    """Read recent audit-log entries — operator-only."""
    # SECURITY (viewer-read-gating, 2026-07-08): gated on
    # ``system.config.write`` (operator-only system cap), NOT
    # ``system.view`` — the audit log discloses operator user_ids and
    # every agent action, and ``system.view`` is in the VIEWER bundle.
    # See view_status_tool_impl for the full rationale.
    denied = _require_capability(principal, "system.config.write")
    if denied is not None:
        return denied

    filter_agent_id = arguments.get("agent_id")  # Optional filter
    filter_action = arguments.get("action")  # Optional filter
    limit = arguments.get("limit", 50)  # Default limit 50

    # Validate limit
    try:
        limit = int(limit)
        if not (1 <= limit <= 200):  # Max 200 for safety
            limit = 50
    except ValueError:
        limit = 50

    # Filter the in-memory audit log (g.audit_log) (main.py:1394-1400)
    # For a more complete audit log, one might query the agent_actions table from DB.
    # The original tool only viewed the in-memory `audit_log`.

    # The original `audit_log` was a global list.
    # The `log_audit` function in `utils/audit_utils.py` appends to `g.audit_log`.
    # So, we read from `g.audit_log`.

    # Create a working copy for filtering
    current_audit_log_snapshot = list(g.audit_log)  # Filter from a snapshot

    filtered_log_entries = current_audit_log_snapshot
    if filter_agent_id:
        filtered_log_entries = [
            entry
            for entry in filtered_log_entries
            if entry.get("agent_id") == filter_agent_id
        ]
    if filter_action:
        filtered_log_entries = [
            entry
            for entry in filtered_log_entries
            if entry.get("action") == filter_action
        ]

    # Get the most recent entries up to the limit (main.py:1403)
    # Slicing from the end gives recent entries.
    limited_log_entries = filtered_log_entries[-limit:]

    # Log this action itself (main.py:1405)
    log_audit(
        principal.actor_label() if principal else "operator",
        "view_audit_log",
        {
            "filter_agent_id": filter_agent_id,
            "filter_action": filter_action,
            "limit": limit,
        },
    )

    try:
        log_json = json.dumps(limited_log_entries, indent=2)
    except TypeError as e:
        logger.error(f"Error serializing audit log to JSON: {e}")
        return Failed(message=f"Error creating audit log JSON: {e}")

    return Ok(
        data={
            "entries": limited_log_entries,
            "count": len(limited_log_entries),
            "filter_agent_id": filter_agent_id,
            "filter_action": filter_action,
            "limit": limit,
        },
        message=(
            f"Audit Log ({len(limited_log_entries)} entries displayed, "
            f"filtered by agent: {filter_agent_id or 'Any'}, action: "
            f"{filter_action or 'Any'}):\n{log_json}"
        ),
    )


# --- get_agent_tokens tool ---
async def get_agent_tokens_tool_impl(
    arguments: Dict[str, Any],
    *,
    principal: Optional[Principal] = None,
) -> ToolResult:
    """
    Retrieve agent tokens with advanced filtering capabilities.
    Supports filtering by status, agent_id pattern, creation date range,
    and more. Operator-only.
    """
    # SECURITY (FINDING 2): agent bearer tokens are operator-tier
    # secrets. The viewer bundle holds ``agents.view`` (see
    # core/capabilities.py::PROJECT_ROLE_BUNDLES), so gating on it leaked
    # every agent's plaintext bearer to read-only viewers, who could
    # replay a harvested token to escalate to write. Gate on an
    # operator-only cap instead: ``agents.register`` is the operation
    # that MINTS + returns an agent bearer, so the privilege to view
    # existing agent tokens belongs to the same tier. Viewers (and agent
    # bearers, which lack this cap) are denied here; the masking check
    # below is the second, defense-in-depth layer.
    denied = _require_capability(principal, "agents.register")
    if denied is not None:
        return denied

    # Extract and validate filter parameters
    filter_status = arguments.get(
        "filter_status"
    )  # e.g., "active", "terminated", "created"
    filter_agent_id_pattern = arguments.get(
        "filter_agent_id_pattern"
    )  # SQL LIKE pattern
    filter_created_after = arguments.get("filter_created_after")  # ISO format date
    filter_created_before = arguments.get("filter_created_before")  # ISO format date
    include_terminated = arguments.get("include_terminated", False)  # Boolean
    # SECURITY (FINDING 2): default to MASKED. The prior default of True
    # meant a caller who simply omitted the flag received plaintext
    # bearers. Callers must now explicitly opt in AND be confirmed
    # operator tier (checked via ``expose_tokens`` below) to see them.
    include_sensitive_data = arguments.get("include_sensitive_data", False)  # Boolean
    limit = arguments.get("limit", 50)  # Default limit
    offset = arguments.get("offset", 0)  # Pagination offset
    sort_by = arguments.get("sort_by", "created_at")  # Sort field
    sort_order = arguments.get("sort_order", "DESC")  # ASC or DESC

    # Validate parameters
    try:
        limit = int(limit)
        if not (1 <= limit <= 500):  # Max 500 for safety
            limit = 50
    except (ValueError, TypeError):
        limit = 50

    try:
        offset = int(offset)
        if offset < 0:
            offset = 0
    except (ValueError, TypeError):
        offset = 0

    # Validate sort parameters
    allowed_sort_fields = ["created_at", "updated_at", "agent_id", "status"]
    if sort_by not in allowed_sort_fields:
        sort_by = "created_at"

    if sort_order.upper() not in ["ASC", "DESC"]:
        sort_order = "DESC"

    try:
        # PR 6: filter + count go through AgentRepository.query — the
        # repo owns the WHERE-building loop and the pagination total.
        from ..repositories import agent_repo

        rows, total_count = agent_repo.query({
            "status": filter_status,
            "agent_id_pattern": filter_agent_id_pattern,
            "include_terminated": include_terminated,
            "created_after": filter_created_after,
            "created_before": filter_created_before,
            "sort_by": sort_by,
            "sort_order": sort_order,
            "limit": limit,
            "offset": offset,
        })

        # SECURITY (FINDING 2): plaintext tokens are surfaced ONLY when
        # the caller both explicitly opted in AND is confirmed operator
        # tier. Any non-confirmed-operator-tier caller (e.g. a viewer
        # whose group grant let them pass the coarse cap gate) is masked
        # regardless of the flag — mirrors ``is_confirmed_operator_tier``
        # in app/routers/composition.py so REST and MCP agree.
        expose_tokens = include_sensitive_data and _is_confirmed_operator_tier(
            principal
        )
        agents_data = []
        for row in rows:
            agent_data = dict(row)
            if not expose_tokens:
                # SECURITY (viewer-read-gating finding 3, 2026-07-08):
                # FULL-mask the bearer for the non-confirmed-operator
                # path. The prior ``token[:4] + "..." + token[-4:]`` form
                # disclosed 8 characters of a secret bearer to any
                # non-operator caller — enough to narrow a brute-force or
                # confirm a guessed token. Confirmed operators still get
                # the real token via the ``expose_tokens`` branch (the
                # SEC2 contract, unchanged).
                if "token" in agent_data:
                    agent_data["token"] = "***"
            agents_data.append(agent_data)

        # Log this access against the REAL caller (FINDING 2 audit bug:
        # the actor was hard-coded "admin", so a viewer's dump was
        # recorded as an admin action). ``expose_tokens`` records whether
        # plaintext was actually surfaced, not merely requested.
        log_audit(
            principal.actor_label() if principal else "operator",
            "get_agent_tokens",
            {
                "filter_status": filter_status,
                "filter_agent_id_pattern": filter_agent_id_pattern,
                "agents_returned": len(agents_data),
                "total_matching": total_count,
                "include_sensitive_data": include_sensitive_data,
                "tokens_exposed": expose_tokens,
            },
        )

        # Build response
        response_data = {
            "agents": agents_data,
            "pagination": {
                "offset": offset,
                "limit": limit,
                "total_count": total_count,
                "returned_count": len(agents_data),
                "has_more": offset + len(agents_data) < total_count,
            },
            "filters_applied": {
                "filter_status": filter_status,
                "filter_agent_id_pattern": filter_agent_id_pattern,
                "filter_created_after": filter_created_after,
                "filter_created_before": filter_created_before,
                "include_terminated": include_terminated,
                # Report the EFFECTIVE exposure, not merely the requested
                # flag, so a client can tell whether tokens were masked.
                "include_sensitive_data": expose_tokens,
            },
            "sort": {"sort_by": sort_by, "sort_order": sort_order},
        }

        try:
            response_json = json.dumps(response_data, indent=2)
        except TypeError as e:
            logger.error(f"Error serializing agent tokens response to JSON: {e}")
            return Failed(message=f"Error creating response JSON: {e}")

        return Ok(
            data=response_data,
            message=(
                f"Agent Tokens ({len(agents_data)} of {total_count} "
                f"total):\n{response_json}"
            ),
        )

    except sqlite3.Error as e_sql:
        logger.error(f"Database error retrieving agent tokens: {e_sql}", exc_info=True)
        return Failed(message=f"Database error retrieving agent tokens: {e_sql}")
    except Exception as e:
        logger.error(f"Unexpected error retrieving agent tokens: {e}", exc_info=True)
        return Failed(message=f"Unexpected error retrieving agent tokens: {e}")



# --- Register all admin tools ---
def register_admin_tools():
    # ``register_agent`` is the only agent-creation surface. agent-mcp
    # does not start a claude process: the operator pastes the
    # returned ``mcp_snippet`` into the user's ``.mcp.json`` and the
    # user owns their own claude session.
    register_tool(
        name="register_agent",
        description=(
            "Register a new agent identity (DB row + bearer token) WITHOUT "
            "spawning a claude process. Returns the token alongside a "
            "ready-to-paste .mcp.json snippet. Operator-only."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "agent_id for the new row.",
                },
                "agent_id": {
                    "type": "string",
                    "description": (
                        "Back-compat alias for `name`. Either field works; "
                        "if both are present, `name` wins."
                    ),
                },
                "role": {
                    "type": "string",
                    "description": "Agent role: 'worker' or 'manager'.",
                    "enum": ["worker", "manager"],
                    "default": "worker",
                },
                "agent_role": {
                    "type": "string",
                    "description": "Back-compat alias for `role`.",
                    "enum": ["worker", "manager"],
                },
                "project_name": {
                    "type": "string",
                    "description": (
                        "Project the .mcp.json snippet should point at. "
                        "Optional; falls back to principal.project_name."
                    ),
                },
                "host": {
                    "type": "string",
                    "description": (
                        "Public base URL the user's claude reaches the "
                        "deployment at (e.g. https://host.tailnet.ts.net). "
                        "Optional; falls back to $AGENT_MCP_EXTERNAL_URL."
                    ),
                },
            },
            # `name` OR `agent_id` is required — the impl rejects with
            # ``Invalid(field="name", ...)`` when both are absent. Not
            # expressing that as a JSON-schema ``anyOf`` because the
            # back-compat alias is a transient (PR-0 / PR-1) shape.
            "required": [],
            "additionalProperties": False,
        },
        implementation=register_agent_tool_impl,
        visibility="operator",
    )

    register_tool(
        name="view_status",
        description="View the status of all agents, connections, and the MCP server.",
        input_schema={  # From main.py:1663-1674
            "type": "object",
            "properties": {
            },
            "required": [],
            "additionalProperties": False,
        },
        implementation=view_status_tool_impl,
        visibility="operator",
    )

    register_tool(
        name="terminate_agent",
        description="Terminate an active agent with the given ID.",
        input_schema={  # From main.py:1676-1689
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "Unique identifier for the agent to terminate",
                },
            },
            "required": ["agent_id"],
            "additionalProperties": False,
        },
        implementation=terminate_agent_tool_impl,
        visibility="operator",
    )

    # E2 (arch-deepening): the real tools behind the agent-lifecycle REST
    # routes (``/api/agents/<id>/restore``, ``/edit``, ``DELETE /<id>``).
    # Each gates on ``agents.terminate`` — the same operator-tier cap the
    # routes' ``require_operator_session`` resolves to (auth-equivalent).
    # ``visibility="operator"`` keeps them out of a worker's tools/list.
    register_tool(
        name="restore_agent",
        description=(
            "Restore a terminated agent (reverse a soft-delete): flip "
            "status='terminated' back to 'created' and clear terminated_at. "
            "Operator-only."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "Unique identifier for the terminated agent to restore.",
                },
            },
            "required": ["agent_id"],
            "additionalProperties": False,
        },
        implementation=restore_agent_tool_impl,
        visibility="operator",
    )

    register_tool(
        name="edit_agent",
        description=(
            "Update mutable fields of an existing agent (capabilities, "
            "color, working_directory, aoe_session_id, auto_event_loop, "
            "agent_role). Operator-only."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "Unique identifier for the agent to edit.",
                },
                "capabilities": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Replacement capability labels for the agent.",
                },
                "color": {
                    "type": "string",
                    "description": "Display color for the agent in the dashboard.",
                },
                "working_directory": {
                    "type": "string",
                    "description": "Agent working directory (informational metadata).",
                },
                "aoe_session_id": {
                    "type": "string",
                    "description": "AoE session binding (16 lowercase hex chars, or empty to clear).",
                },
                "auto_event_loop": {
                    "type": "boolean",
                    "description": "Per-agent wake-loop toggle.",
                },
                "agent_role": {
                    "type": "string",
                    "description": "Agent role: 'worker' or 'manager'.",
                    "enum": ["worker", "manager"],
                },
            },
            "required": ["agent_id"],
            "additionalProperties": False,
        },
        implementation=edit_agent_tool_impl,
        visibility="operator",
    )

    register_tool(
        name="purge_agent",
        description=(
            "Hard-delete an agent and cascade-tombstone every reference "
            "(messages, tasks, actions, sessions). Destructive + "
            "irreversible. Operator-only."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "Unique identifier for the agent to purge.",
                },
            },
            "required": ["agent_id"],
            "additionalProperties": False,
        },
        implementation=purge_agent_tool_impl,
        visibility="operator",
    )

    register_tool(
        name="view_audit_log",
        description="View the in-memory audit log, optionally filtered by agent ID or action, with a limit.",
        input_schema={  # From main.py:1788-1810
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "Filter audit log by agent ID (optional)",
                },
                "action": {
                    "type": "string",
                    "description": "Filter audit log by action (e.g., 'create_agent') (optional)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of entries to return (default 50, max 200)",
                    "default": 50,
                    "minimum": 1,
                    "maximum": 200,
                },
            },
            "required": [],
            "additionalProperties": False,
        },
        implementation=view_audit_log_tool_impl,
        visibility="operator",
    )

    register_tool(
        name="get_agent_tokens",
        description="Retrieve agent tokens with advanced filtering capabilities. Supports filtering by status, agent_id pattern, creation date range, and more.",
        input_schema={
            "type": "object",
            "properties": {
                "filter_status": {
                    "type": "string",
                    "description": "Filter by agent status (e.g., 'active', 'terminated', 'created')",
                },
                "filter_agent_id_pattern": {
                    "type": "string",
                    "description": "Filter by agent ID using SQL LIKE pattern (e.g., 'test_%', '%prod%')",
                },
                "filter_created_after": {
                    "type": "string",
                    "description": "Filter agents created after this date (ISO format: YYYY-MM-DDTHH:MM:SS)",
                },
                "filter_created_before": {
                    "type": "string",
                    "description": "Filter agents created before this date (ISO format: YYYY-MM-DDTHH:MM:SS)",
                },
                "include_terminated": {
                    "type": "boolean",
                    "description": "Include terminated agents in results (default: false)",
                    "default": False,
                },
                "include_sensitive_data": {
                    "type": "boolean",
                    "description": "Include full tokens in response (default: true). If false, tokens will be masked for security.",
                    "default": True,
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of agents to return (default: 50, max: 500)",
                    "default": 50,
                    "minimum": 1,
                    "maximum": 500,
                },
                "offset": {
                    "type": "integer",
                    "description": "Number of agents to skip for pagination (default: 0)",
                    "default": 0,
                    "minimum": 0,
                },
                "sort_by": {
                    "type": "string",
                    "description": "Field to sort by (default: 'created_at')",
                    "enum": ["created_at", "updated_at", "agent_id", "status"],
                    "default": "created_at",
                },
                "sort_order": {
                    "type": "string",
                    "description": "Sort order (default: 'DESC')",
                    "enum": ["ASC", "DESC"],
                    "default": "DESC",
                },
            },
            "required": [],
            "additionalProperties": False,
        },
        implementation=get_agent_tokens_tool_impl,
        visibility="operator",
    )

    # There is no ``relaunch_agent`` tool: agent-mcp doesn't own the
    # user's claude process, so relaunching is the user's business
    # (close the session, paste the snippet again, ``claude``).
    # Operators who want a fresh bearer for an existing row use
    # ``register_agent`` to mint a new identity.


# Call registration when this module is imported
register_admin_tools()
