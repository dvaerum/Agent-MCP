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
# ``principal.has_role("operator")`` check at the top of each tool
# (the decorator's wrapper signature locks the inner function to
# ``(arguments) -> list[TextContent]`` and can't forward the
# Principal kwarg the dispatcher passes to migrated tools). Tool
# visibility in ``tools/list`` is still gated by the
# ``visibility="operator"`` kwarg on each ``register_tool(...)``
# call below — that's the source of truth read by
# ``tools/access._derive_access_level`` once the decorator is gone.
from ..core.principal import Principal
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
from ..db.connection import get_db_connection
from ..db.actions.agent_actions_db import log_agent_action_to_db  # For DB logging


_OPERATOR_REQUIRED_REASON = (
    "Operator session or system token required for admin tools."
)


def _require_operator(principal: Optional[Principal]) -> Optional[PermissionDenied]:
    """Return PermissionDenied iff the caller's principal isn't operator-tier.

    Wave 6 PR 5 — every tool in this module is operator-only (matches the
    pre-migration ``@requires_role("operator")`` decorator). Centralised
    here so the inline check at the top of each tool reads as one line
    and the failure wording stays uniform across the module.
    """
    if principal is None or not principal.has_role("operator"):
        return PermissionDenied(reason=_OPERATOR_REQUIRED_REASON)
    return None



# --- register_agent tool (Wave 7 PR 0 — coordinator transition) ---
#
# The register-only sibling of :func:`create_agent_tool_impl`. Mints an
# agent identity (DB row + bearer token) WITHOUT spawning a claude
# process. The plan calls this the "coordinator" shape: agent-mcp stops
# owning user-side claude processes; the user owns them; agent-mcp
# mints the token and hands the operator a ready-to-paste ``.mcp.json``
# snippet they drop into the user's claude config.
#
# Co-existence: PR 0 ships ``register_agent`` ALONGSIDE the legacy
# ``create_agent`` (which still spawns via tmux). PR 1 migrates test
# fixtures to register-only; PR 3 deletes the spawn block + the
# ``agent_mcp/runtime/agent_runtime.py`` module entirely.
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

    Wave 7 PR 0 (coordinator transition). The legacy
    :func:`create_agent_tool_impl` (which spawns via tmux) stays in
    this PR to keep old tests / old workflows working; PR 3 deletes
    the spawn block + the runtime module entirely.

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
    denied = _require_operator(principal)
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

    # Mirror create_agent_tool_impl's defence-in-depth tombstone-
    # bracket guard. The repo would also catch this via its slug
    # regex; returning a clean Invalid here gives the operator a
    # precise reason instead of a generic regex-mismatch.
    if "[" in agent_id or "]" in agent_id:
        return Invalid(
            field="name",
            message=(
                f"invalid name {agent_id!r}: `[` and `]` are reserved "
                "characters (used by the purge-cascade tombstone format "
                "`[deleted-<id>]`)."
            ),
        )

    # Refuse to clobber an existing agent. Mirrors create_agent's
    # in-memory + DB checks so both surfaces give the operator the
    # same wording when they try to re-register a name in use.
    if agent_id in g.agent_working_dirs:
        return Conflict(
            reason=f"Agent '{agent_id}' already exists (in active memory).",
        )

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

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
            try:
                conn.rollback()
            except Exception:
                pass
            return Invalid(field="name", message=str(ve))

        log_agent_action_to_db(
            cursor,
            principal.actor_label() if principal else "operator",
            "registered_agent",
            details={
                "agent_id": agent_id,
                "role": role,
            },
        )
        conn.commit()

        # Post-commit cache reconciliation through the repo (mirrors
        # create_agent's pattern — keeps cache + DB in lockstep).
        agent_repo.upsert_cache({
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
        })

        log_audit(
            principal.actor_label() if principal else "operator",
            "register_agent",
            {
                "agent_id": agent_id,
                "role": role,
            },
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

    except sqlite3.Error as e_sql:
        if conn:
            conn.rollback()
        logger.error(
            "Database error registering agent %s: %s",
            agent_id, e_sql, exc_info=True,
        )
        return Failed(message=f"Database error registering agent: {e_sql}")
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(
            "Unexpected error registering agent %s: %s",
            agent_id, e, exc_info=True,
        )
        return Failed(message=f"Unexpected error registering agent: {e}")
    finally:
        if conn:
            conn.close()


# --- view_status tool ---
# Original logic from main.py: lines 1242-1268 (view_status_tool function)
async def view_status_tool_impl(
    arguments: Dict[str, Any],
    *,
    principal: Optional[Principal] = None,
) -> ToolResult:
    """Report active agents + server status — operator-only."""
    denied = _require_operator(principal)
    if denied is not None:
        return denied

    log_audit("admin", "view_status", {})  # main.py:1249

    # Build agent status from g.active_agents and g.agent_working_dirs (main.py:1251-1259)
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


# --- terminate_agent tool ---
# Original logic from main.py: lines 1270-1316 (terminate_agent_tool function)
async def terminate_agent_tool_impl(
    arguments: Dict[str, Any],
    *,
    principal: Optional[Principal] = None,
) -> ToolResult:
    """Soft-terminate an agent (flips status) — operator-only."""
    denied = _require_operator(principal)
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

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        if not found_agent_token:
            # Check DB if not found in memory (main.py:1285-1290)
            cursor.execute(
                "SELECT token FROM agents WHERE agent_id = ? AND status != ?",
                (agent_id_to_terminate, "terminated"),
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

        log_agent_action_to_db(
            cursor,
            "admin",
            "terminated_agent",
            details={"agent_id": agent_id_to_terminate},
        )
        conn.commit()

        # Post-commit cache reconciliation through the repo. Mirrors
        # the manual evictions the legacy code did inline; the repo's
        # `evict_from_cache` handles both the token-keyed and
        # agent_id-keyed maps in lockstep.
        agent_repo.evict_from_cache(
            agent_id_to_terminate, token=found_agent_token,
        )

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

        # Wave 7 PR 3 (coordinator transition): the spawn machinery is
        # gone — agent-mcp never owned the user's claude process, so
        # terminate is just "revoke the token + flip status". The
        # user's local claude session keeps running until they close
        # it themselves. The tmux-session tracking globals + the
        # ``tmux_killed`` response field PR 0 left as back-compat are
        # both retired here.

        log_audit(
            "admin", "terminate_agent", {"agent_id": agent_id_to_terminate}
        )  # main.py:1313
        logger.info(f"Agent '{agent_id_to_terminate}' terminated successfully.")
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
        if conn:
            conn.rollback()
        logger.error(
            f"Database error terminating agent {agent_id_to_terminate}: {e_sql}",
            exc_info=True,
        )
        return Failed(message=f"Database error terminating agent: {e_sql}")
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(
            f"Unexpected error terminating agent {agent_id_to_terminate}: {e}",
            exc_info=True,
        )
        return Failed(message=f"Unexpected error terminating agent: {e}")
    finally:
        if conn:
            conn.close()


# --- view_audit_log tool ---
# Original logic from main.py: lines 1387-1408 (view_audit_log_tool function)
async def view_audit_log_tool_impl(
    arguments: Dict[str, Any],
    *,
    principal: Optional[Principal] = None,
) -> ToolResult:
    """Read recent audit-log entries — operator-only."""
    denied = _require_operator(principal)
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
        "admin",
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
    denied = _require_operator(principal)
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
    include_sensitive_data = arguments.get("include_sensitive_data", True)  # Boolean
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

        # Mask sensitive data the same way the legacy inline path did.
        agents_data = []
        for row in rows:
            agent_data = dict(row)
            if not include_sensitive_data:
                if "token" in agent_data:
                    token_value = agent_data["token"]
                    if token_value and len(token_value) > 8:
                        agent_data["token"] = (
                            token_value[:4] + "..." + token_value[-4:]
                        )
                    else:
                        agent_data["token"] = "***"
            agents_data.append(agent_data)

        # Log this access
        log_audit(
            "admin",
            "get_agent_tokens",
            {
                "filter_status": filter_status,
                "filter_agent_id_pattern": filter_agent_id_pattern,
                "agents_returned": len(agents_data),
                "total_matching": total_count,
                "include_sensitive_data": include_sensitive_data,
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
                "include_sensitive_data": include_sensitive_data,
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
    # Wave 7 PR 3 (coordinator transition) — ``register_agent`` is the
    # only agent-creation surface. The legacy ``create_agent`` tool
    # (which spawned a claude via tmux + ``--dangerously-skip-permissions``
    # and orphan-stormed processes through the SIGHUP-ignore bug) was
    # deleted along with the runtime module that implemented it. agent-mcp
    # never starts a claude process now — the operator pastes the
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
                "token": {"type": "string", "description": "Admin authentication token. Optional if Authorization: Bearer header is supplied (recommended)."}
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
                "token": {
                    "type": "string",
                    "description": "Admin authentication token. Optional if Authorization: Bearer header is supplied (recommended).",
                },
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

    register_tool(
        name="view_audit_log",
        description="View the in-memory audit log, optionally filtered by agent ID or action, with a limit.",
        input_schema={  # From main.py:1788-1810
            "type": "object",
            "properties": {
                "token": {
                    "type": "string",
                    "description": "Admin authentication token. Optional if Authorization: Bearer header is supplied (recommended).",
                },
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
                "token": {
                    "type": "string",
                    "description": "Admin authentication token. Optional if Authorization: Bearer header is supplied (recommended).",
                },
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

    # Wave 7 PR 3 (coordinator transition): the ``relaunch_agent``
    # tool (which sent ``/clear`` to the agent's tmux session and
    # pushed a new prompt) was deleted with the rest of the spawn
    # machinery. Under the coordinator model agent-mcp doesn't own
    # the user's claude process, so "relaunch" is the user's
    # business (close the session, paste the snippet again,
    # ``claude``). Operators who want a fresh bearer for an existing
    # row use ``register_agent`` to mint a new identity.


# Call registration when this module is imported
register_admin_tools()
