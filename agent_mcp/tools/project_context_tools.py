# Agent-MCP/mcp_template/mcp_server_src/tools/project_context_tools.py
import json
import datetime
import re
import sqlite3
from typing import List, Dict, Any, Optional, Tuple

# Keys that hold project-level secrets. view_project_context filters
# rows whose key matches when the caller isn't admin, so worker-tier
# tokens can't read the admin credential (or other secrets) through
# the tool surface. UPSTREAM_ISSUES.md issue I.
_SECRET_KEY_RE = re.compile(
    r"config_.*_(token|secret|password|api[_-]?key|priv(?:ate)?[_-]?key)",
    re.IGNORECASE,
)

# Keys reserved for admin-only writes/deletes (Phase 7b). Broader than
# `_SECRET_KEY_RE`: any `config_*` is treated as policy or secret data,
# regardless of suffix. Workers attempting to create or modify a
# config_* entry are rejected at the tool boundary.
_CONFIG_KEY_RE = re.compile(r"^config_", re.IGNORECASE)

# Worker-policy toggle keys (Phase 4). Writing one of these flips the
# tools/list visibility for worker bearers (PR #55 reads the toggle
# live in `tools/access.py::is_visible_to_role`), so any subscribed
# MCP client should re-fetch `tools/list`. We emit
# `notifications/tools/list_changed` on the current request's session
# (best-effort: in stateless StreamableHTTP mode there is no
# enumeration of OTHER sessions to fan out to — clients still see the
# new visibility on their next periodic `tools/list` call).
_WORKER_POLICY_TOGGLE_RE = re.compile(r"^config_allow_worker_", re.IGNORECASE)


def _is_worker_policy_toggle(context_key: str) -> bool:
    """True if `context_key` controls worker tool visibility per
    `agent_mcp/tools/access.py::TOOL_ACCESS`. Source-of-truth here is
    intentionally pattern-based (any `config_allow_worker_*`) so a
    future toggle added to TOOL_ACCESS picks up the notification
    automatically."""
    return bool(_WORKER_POLICY_TOGGLE_RE.match(context_key or ""))


async def _emit_tools_list_changed(context_key: str) -> None:
    """Best-effort emit `notifications/tools/list_changed` on the
    current MCP request's session.

    Safe to call from ANY context — when no `request_ctx` is bound
    (e.g. REST endpoints, unit tests, background tasks) the helper
    silently no-ops. The toggle-write itself is the source of truth;
    `tools/list` reads it live, so clients converge on the next
    refresh even without the push.

    Cross-request fan-out (notifying workers' open GET /mcp streams
    when an admin makes the change) requires the session registry
    not yet built in stateless mode. Tracked separately.
    """
    try:
        from mcp.server.lowlevel.server import request_ctx
    except Exception:  # pragma: no cover - defensive
        return

    try:
        ctx = request_ctx.get()
    except LookupError:
        return
    if ctx is None:
        return

    session = getattr(ctx, "session", None)
    if session is None:
        return

    sender = getattr(session, "send_tool_list_changed", None)
    if sender is None:
        return

    try:
        await sender()
    except Exception as e:  # pragma: no cover - defensive
        from ..core.config import logger
        logger.debug(
            "tools/list_changed emit failed (key=%s): %s",
            context_key, e,
        )

    # Cross-request fan-out: tools/list visibility is bearer-role
    # dependent (PR #55), so every subscribed worker needs to refetch
    # after a worker-policy toggle. Enqueue the notification on every
    # registered session's runtime queue; the GET /mcp drain loop
    # (Phase: transport-wiring) ships these to the wire. Until that
    # wiring lands, the queue accumulates and clients still converge
    # on the next periodic tools/list refresh — same fallback as
    # before this hook.
    try:
        from ..core import session_registry

        payload = {
            "jsonrpc": "2.0",
            "method": "notifications/tools/list_changed",
            "params": {},
        }
        session_registry.fanout_to_all(payload)
    except Exception as e:  # pragma: no cover - defensive
        from ..core.config import logger
        logger.debug(
            "tools/list_changed fanout failed (key=%s): %s",
            context_key, e,
        )


def _config_key_error() -> str:
    return (
        "Unauthorized: config_* keys are admin-only; "
        "workers cannot create or modify policy/secret entries"
    )


def _creator_mismatch_error(context_key: str, creator: str) -> str:
    return (
        f"Unauthorized: key '{context_key}' was created by "
        f"'{creator}'; only its creator or admin can modify it"
    )

import mcp.types as mcp_types
from sqlalchemy import func, or_, select
from sqlalchemy.exc import SQLAlchemyError

from .registry import register_tool
from ..core.authorize import requires
from ..core.config import logger
from ..core import globals as g  # Not directly used here, but auth uses it
from ..core.auth import get_agent_id, verify_token
from ..utils.audit_utils import log_audit
from ..db.connection import get_db_connection
from ..db.engine import SessionLocal, get_session
from ..db.models import ProjectContext
from ..db.actions.agent_actions_db import log_agent_action_to_db


def _analyze_context_health(context_entries: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Analyze project context health and identify issues"""
    if not context_entries:
        return {"status": "no_data", "total": 0}

    total = len(context_entries)
    issues = []
    warnings = []
    stale_count = 0
    json_errors = 0
    large_entries = 0
    current_time = datetime.datetime.now()

    for entry in context_entries:
        context_key = entry.get("context_key", "unknown")
        value = entry.get("value", "")
        updated_at = entry.get("updated_at")

        # Check for JSON parsing issues
        try:
            if isinstance(value, str):
                json.loads(value)
        except json.JSONDecodeError:
            json_errors += 1
            issues.append(f"JSON parse error in '{context_key}'")

        # Check for stale entries (30+ days old)
        if updated_at:
            try:
                updated_time = datetime.datetime.fromisoformat(
                    updated_at.replace("Z", "+00:00").replace("+00:00", "")
                )
                days_old = (current_time - updated_time).days
                if days_old > 30:
                    stale_count += 1
                    if days_old > 90:
                        warnings.append(f"'{context_key}' is {days_old} days old")
            except:
                warnings.append(f"Invalid timestamp for '{context_key}'")

        # Check for oversized entries (>10KB)
        entry_size = len(str(value))
        if entry_size > 10240:  # 10KB
            large_entries += 1
            warnings.append(f"'{context_key}' is large ({entry_size//1024}KB)")

    # Calculate health score
    stale_ratio = stale_count / total
    error_ratio = json_errors / total
    large_ratio = large_entries / total

    health_score = max(
        0, min(100, 100 - (stale_ratio * 40) - (error_ratio * 50) - (large_ratio * 10))
    )

    health_status = (
        "excellent"
        if health_score >= 90
        else (
            "good"
            if health_score >= 70
            else "needs_attention" if health_score >= 50 else "critical"
        )
    )

    return {
        "status": health_status,
        "health_score": round(health_score, 1),
        "total": total,
        "stale_entries": stale_count,
        "json_errors": json_errors,
        "large_entries": large_entries,
        "issues": issues[:5],  # Limit to first 5
        "warnings": warnings[:5],  # Limit to first 5
        "recommendations": _generate_context_recommendations(
            stale_count, json_errors, large_entries, total
        ),
    }


def _generate_context_recommendations(
    stale_count: int, json_errors: int, large_entries: int, total: int
) -> List[str]:
    """Generate actionable recommendations based on context health"""
    recommendations = []

    if json_errors > 0:
        recommendations.append(
            f"Fix {json_errors} JSON parsing errors using validate_context_consistency"
        )

    if stale_count > total * 0.3:  # More than 30% stale
        recommendations.append(
            f"Review and update {stale_count} stale entries (30+ days old)"
        )

    if large_entries > 0:
        recommendations.append(
            f"Consider breaking down {large_entries} large entries into smaller components"
        )

    if total > 100:
        recommendations.append(
            "Consider archiving old context entries to improve performance"
        )

    if not recommendations:
        recommendations.append(
            "Context health is excellent - no immediate action required"
        )

    return recommendations


def _row_to_dict(row: ProjectContext) -> Dict[str, Any]:
    """Coerce a ProjectContext row into a plain dict for backup / health
    analysis / validation consumers. Post-Phase-7b shape: includes the
    new `created_at` / `created_by` columns and renames `last_updated`
    to `updated_at`."""
    return {
        "context_key": row.context_key,
        "value": row.value,
        "updated_at": row.updated_at,
        "updated_by": row.updated_by,
        "created_at": row.created_at,
        "created_by": row.created_by,
        "description": row.description,
    }


def _check_write_authorization(
    session,
    requesting_agent_id: str,
    context_key: str,
    *,
    is_admin: bool,
) -> Optional[str]:
    """Return None if the caller may write/delete `context_key`, else
    a specific human-readable error message.

    Rules (Phase 7b):
    - Admin: always authorized.
    - Non-admin + config_* key: forbidden (admin-only safety invariant).
    - Non-admin + existing key + creator != self: forbidden.
    - Non-admin + new key (no row yet) + non-config: allowed.
    - Non-admin + existing key + creator == self + non-config: allowed.

    Reads `created_by` via the same SQLAlchemy session, so the check
    sees pending changes inside an open transaction.
    """
    if is_admin:
        return None
    if _CONFIG_KEY_RE.match(context_key):
        return _config_key_error()
    existing = (
        session.query(ProjectContext.created_by)
        .filter(ProjectContext.context_key == context_key)
        .one_or_none()
    )
    if existing is None:
        return None
    creator = existing[0]
    # Legacy rows where created_by is NULL (pre-migration backfill edge
    # case) cannot be safely attributed — treat as admin-only so workers
    # can't claim them.
    if creator is None:
        return _creator_mismatch_error(context_key, "(unknown — legacy entry)")
    if creator != requesting_agent_id:
        return _creator_mismatch_error(context_key, creator)
    return None


def _create_context_backup(session, backup_name: str = None) -> Dict[str, Any]:
    """Create a backup of all project context data via the ORM."""
    if not backup_name:
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_name = f"context_backup_{timestamp}"

    rows = (
        session.query(ProjectContext)
        .order_by(ProjectContext.context_key)
        .all()
    )

    backup_data = {
        "backup_name": backup_name,
        "created_at": datetime.datetime.now().isoformat(),
        "total_entries": len(rows),
        "entries": [_row_to_dict(r) for r in rows],
    }

    return backup_data


# --- view_project_context tool ---
# Original logic from main.py: lines 1411-1465 (view_project_context_tool function)
@requires("any")
async def view_project_context_tool_impl(
    arguments: Dict[str, Any],
) -> List[mcp_types.TextContent]:
    agent_auth_token = arguments.get("token")
    context_key_filter = arguments.get("context_key")  # Optional specific key
    search_query_filter = arguments.get("search_query")  # Optional search query

    # Smart features
    show_health_analysis = arguments.get("show_health_analysis", False)
    show_stale_entries = arguments.get(
        "show_stale_entries", False
    )  # Show entries older than 30 days
    include_backup_info = arguments.get(
        "include_backup_info", False
    )  # Include backup status
    max_results = arguments.get("max_results", 50)  # Limit results
    # Sort by: key, updated_at, size. Accept `last_updated` as a
    # backward-compatible alias for `updated_at` so existing dashboard
    # builds + MCP clients don't break mid-deploy.
    sort_by = arguments.get("sort_by", "updated_at")
    if sort_by == "last_updated":
        sort_by = "updated_at"

    # @requires("any") guaranteed entry; resolve id for audit + the
    # admin-vs-worker secret-key redaction below (issue I).
    requesting_agent_id = get_agent_id(agent_auth_token)

    # Log audit (main.py:1417)
    log_audit(
        requesting_agent_id,
        "view_project_context",
        {"context_key": context_key_filter, "search_query": search_query_filter},
    )

    results_list: List[Dict[str, Any]] = []
    response_message: str = ""

    session = SessionLocal()
    try:
        # Build smart query based on filters
        stmt = select(
            ProjectContext.context_key,
            ProjectContext.value,
            ProjectContext.description,
            ProjectContext.updated_by,
            ProjectContext.updated_at,
            ProjectContext.created_by,
            ProjectContext.created_at,
            func.length(ProjectContext.value).label("value_size"),
        )

        conditions = []
        if context_key_filter:
            conditions.append(ProjectContext.context_key == context_key_filter)
        elif search_query_filter:
            like_pattern = f"%{search_query_filter}%"
            conditions.append(
                or_(
                    ProjectContext.context_key.like(like_pattern),
                    ProjectContext.description.like(like_pattern),
                    ProjectContext.value.like(like_pattern),
                )
            )

        if show_stale_entries:
            # Show entries older than 30 days
            thirty_days_ago = (
                datetime.datetime.now() - datetime.timedelta(days=30)
            ).isoformat()
            conditions.append(ProjectContext.updated_at < thirty_days_ago)

        if conditions:
            stmt = stmt.where(*conditions)

        # Smart sorting
        if sort_by == "size":
            stmt = stmt.order_by(func.length(ProjectContext.value).desc())
        elif sort_by == "key":
            stmt = stmt.order_by(ProjectContext.context_key.asc())
        else:  # updated_at (default)
            stmt = stmt.order_by(ProjectContext.updated_at.desc())

        stmt = stmt.limit(max_results)

        rows = session.execute(stmt).all()

        # Redact secret-looking keys for non-admin callers (issue I).
        # Admins continue to see everything; workers see everything
        # EXCEPT keys matching _SECRET_KEY_RE.
        if not verify_token(agent_auth_token, "admin"):
            rows = [r for r in rows if not _SECRET_KEY_RE.search(r.context_key)]

        # Process results with enhanced information
        for row_data in rows:
            try:
                value_parsed = json.loads(row_data.value)
                json_valid = True
            except json.JSONDecodeError:
                value_parsed = row_data.value
                json_valid = False

            # Calculate additional metadata
            entry_size = len(str(row_data.value))
            updated_at = row_data.updated_at
            days_old = None

            if updated_at:
                try:
                    updated_time = datetime.datetime.fromisoformat(
                        updated_at.replace("Z", "+00:00").replace("+00:00", "")
                    )
                    days_old = (datetime.datetime.now() - updated_time).days
                except:
                    pass

            entry_data = {
                "key": row_data.context_key,
                "value": value_parsed,
                "description": row_data.description,
                "updated_by": row_data.updated_by,
                "updated_at": updated_at,
                "created_by": row_data.created_by,
                "created_at": row_data.created_at,
                "_metadata": {
                    "size_bytes": entry_size,
                    "size_kb": round(entry_size / 1024, 2),
                    "json_valid": json_valid,
                    "days_old": days_old,
                    "is_stale": days_old and days_old > 30,
                    "is_large": entry_size > 10240,  # >10KB
                },
            }
            results_list.append(entry_data)

        # Generate smart response
        if not results_list:
            response_message = "No project context entries found matching the criteria."
        else:
            # Build header with filter information
            filter_info = []
            if context_key_filter:
                filter_info.append(f"key='{context_key_filter}'")
            if search_query_filter:
                filter_info.append(f"search='{search_query_filter}'")
            if show_stale_entries:
                filter_info.append("stale_only=true")

            header = f"Project Context ({len(results_list)} entries"
            if filter_info:
                header += f", filtered by: {', '.join(filter_info)}"
            header += f", sorted by: {sort_by})"

            response_parts = [header + "\n"]

            # Add health analysis if requested
            if show_health_analysis:
                # Fetch all entries for comprehensive health analysis
                all_rows = session.execute(
                    select(
                        ProjectContext.context_key,
                        ProjectContext.value,
                        ProjectContext.updated_at,
                    )
                ).all()
                all_entries = [
                    {
                        "context_key": r.context_key,
                        "value": r.value,
                        "updated_at": r.updated_at,
                    }
                    for r in all_rows
                ]
                health_analysis = _analyze_context_health(all_entries)

                health_status = health_analysis["status"]
                health_score = health_analysis["health_score"]

                health_icon = (
                    "🟢"
                    if health_status == "excellent"
                    else (
                        "🟡"
                        if health_status == "good"
                        else "🟠" if health_status == "needs_attention" else "🔴"
                    )
                )

                response_parts.append(
                    f"📊 **Context Health:** {health_icon} {health_status.title()} ({health_score}/100)"
                )
                response_parts.append(f"   Total: {health_analysis['total']} entries")
                response_parts.append(
                    f"   Issues: {health_analysis['json_errors']} JSON errors, {health_analysis['stale_entries']} stale, {health_analysis['large_entries']} large"
                )

                if health_analysis["recommendations"]:
                    response_parts.append(
                        f"   💡 {health_analysis['recommendations'][0]}"
                    )
                response_parts.append("")

            # Add backup info if requested
            if include_backup_info:
                response_parts.append(
                    "💾 **Backup Info:** Use bulk_update_project_context for backups"
                )
                response_parts.append("")

            # Format entries
            for i, entry in enumerate(results_list[:20]):  # Limit display to 20 entries
                metadata = entry.get("_metadata", {})

                # Entry header with smart indicators
                indicators = []
                if not metadata.get("json_valid", True):
                    indicators.append("❌ JSON_ERROR")
                if metadata.get("is_stale", False):
                    indicators.append(f"⏰ STALE({metadata.get('days_old')}d)")
                if metadata.get("is_large", False):
                    indicators.append(f"📦 LARGE({metadata.get('size_kb')}KB)")

                indicator_text = " " + " ".join(indicators) if indicators else ""

                response_parts.append(f"**{entry['key']}**{indicator_text}")
                response_parts.append(
                    f"  Description: {entry.get('description', 'No description')}"
                )
                response_parts.append(
                    f"  Updated: {entry.get('updated_at', 'Unknown')} by {entry.get('updated_by', 'Unknown')}"
                )
                if entry.get("created_by"):
                    response_parts.append(
                        f"  Created: {entry.get('created_at', 'Unknown')} by {entry.get('created_by')}"
                    )

                # Show value preview (truncated for large values)
                value_str = (
                    json.dumps(entry["value"], indent=2)
                    if isinstance(entry["value"], (dict, list))
                    else str(entry["value"])
                )
                if len(value_str) > 500:
                    value_str = value_str[:500] + "... [TRUNCATED]"
                response_parts.append(f"  Value: {value_str}")
                response_parts.append("")

            if len(results_list) > 20:
                response_parts.append(f"... and {len(results_list) - 20} more entries")
                response_parts.append(
                    "Use max_results parameter to see more, or add filters to narrow results"
                )

            # Add smart usage tips
            response_parts.append("\n💡 Smart Tips:")
            if not show_health_analysis:
                response_parts.append(
                    "• Add show_health_analysis=true for context health metrics"
                )
            if not show_stale_entries:
                response_parts.append(
                    "• Add show_stale_entries=true to see entries needing updates"
                )
            response_parts.append(
                "• Use sort_by=[key|size|updated_at] for different sorting"
            )
            response_parts.append(
                "• Use validate_context_consistency to fix JSON errors"
            )

            response_message = "\n".join(response_parts)

    except SQLAlchemyError as e_sql:
        logger.error(
            f"Database error viewing project context: {e_sql}", exc_info=True
        )  # main.py:1462
        response_message = f"Database error viewing project context: {e_sql}"
    except (
        json.JSONDecodeError
    ) as e_json:  # Should be caught per-item, but as a fallback
        logger.error(
            f"Error decoding JSON from project_context table during bulk view: {e_json}",
            exc_info=True,
        )  # main.py:1465
        response_message = f"Error decoding stored project context value(s)."
    except Exception as e:
        logger.error(f"Unexpected error viewing project context: {e}", exc_info=True)
        response_message = f"An unexpected error occurred: {e}"
    finally:
        session.close()

    return [mcp_types.TextContent(type="text", text=response_message)]


# --- update_project_context tool ---
def _single_update_inline(
    requesting_agent_id: str,
    context_key_to_update: str,
    value_json_str: str,
    description_for_context: Optional[str],
    *,
    is_admin: bool,
) -> Optional[str]:
    """Sync single-update body — returns an error message or None.

    Inline (not queued) for the same reason as the bulk path: tests +
    test-style entry points run tools on ad-hoc asyncio loops and
    deadlock when the body posts into the lifespan write queue.
    SQLAlchemy + SQLite WAL handles the actual write serialization.
    """
    session = SessionLocal()
    try:
        err = _check_write_authorization(
            session,
            requesting_agent_id,
            context_key_to_update,
            is_admin=is_admin,
        )
        if err is not None:
            session.rollback()
            return err

        now_iso = datetime.datetime.now().isoformat()

        existing = (
            session.query(ProjectContext)
            .filter(ProjectContext.context_key == context_key_to_update)
            .one_or_none()
        )
        if existing is None:
            session.add(
                ProjectContext(
                    context_key=context_key_to_update,
                    value=value_json_str,
                    description=description_for_context,
                    created_at=now_iso,
                    created_by=requesting_agent_id,
                    updated_at=now_iso,
                    updated_by=requesting_agent_id,
                )
            )
        else:
            existing.value = value_json_str
            existing.updated_at = now_iso
            existing.updated_by = requesting_agent_id
            existing.description = description_for_context
            # created_at / created_by stay frozen on UPDATE

        raw_conn = session.connection().connection
        cursor = raw_conn.cursor()
        log_agent_action_to_db(
            cursor,
            requesting_agent_id,
            "updated_context",
            details={"context_key": context_key_to_update, "action": "set/update"},
        )

        session.commit()
        logger.info(
            f"Project context for key '{context_key_to_update}' updated by '{requesting_agent_id}'."
        )
        return None
    finally:
        session.close()


async def _handle_single_context_update(
    requesting_agent_id: str,
    context_key_to_update: str,
    context_value_to_set: Any,
    description_for_context: Optional[str] = None,
    *,
    is_admin: bool,
) -> List[mcp_types.TextContent]:
    """Handle single context update operation.

    Phase 7b: authorizes the write per-key. Admins always pass; workers
    pass only if the key is non-`config_*` and either new or self-owned.
    On insert, stamps `created_at` / `created_by`; on update, leaves
    those untouched and refreshes `updated_at` / `updated_by`.
    """
    log_audit(
        requesting_agent_id,
        "update_project_context",
        {
            "context_key": context_key_to_update,
            "value_type": str(type(context_value_to_set)),
            "description": description_for_context,
        },
    )

    try:
        value_json_str = json.dumps(context_value_to_set)
    except TypeError as e_type:
        logger.error(
            f"Value provided for project context key '{context_key_to_update}' is not JSON serializable: {e_type}"
        )
        return [
            mcp_types.TextContent(
                type="text",
                text=f"Error: Provided context_value is not JSON serializable: {e_type}",
            )
        ]

    try:
        err = _single_update_inline(
            requesting_agent_id,
            context_key_to_update,
            value_json_str,
            description_for_context,
            is_admin=is_admin,
        )
    except (sqlite3.Error, SQLAlchemyError) as e_sql:
        logger.error(
            f"Database error updating project context for key '{context_key_to_update}': {e_sql}",
            exc_info=True,
        )
        return [
            mcp_types.TextContent(
                type="text", text=f"Database error updating project context: {e_sql}"
            )
        ]
    except Exception as e:
        logger.error(
            f"Unexpected error updating project context for key '{context_key_to_update}': {e}",
            exc_info=True,
        )
        return [
            mcp_types.TextContent(
                type="text", text=f"Unexpected error updating project context: {e}"
            )
        ]

    if err is not None:
        return [mcp_types.TextContent(type="text", text=err)]

    # Phase 4: notify subscribers when a worker-policy toggle flips.
    # The helper is best-effort and safe outside a request context.
    if _is_worker_policy_toggle(context_key_to_update):
        await _emit_tools_list_changed(context_key_to_update)

    return [
        mcp_types.TextContent(
            type="text",
            text=f"Project context updated successfully for key '{context_key_to_update}'.",
        )
    ]


async def _handle_bulk_context_update(
    requesting_agent_id: str,
    updates_list: List[Dict[str, Any]],
    *,
    is_admin: bool,
) -> List[mcp_types.TextContent]:
    """Handle bulk context update operations atomically.

    Phase 7b: every entry is authorized before any write lands. If a
    single entry fails the ownership check, the whole batch rolls back
    and the caller receives the specific error message.

    Routes through the shared inline helper for the same loop-affinity
    reason described in `bulk_update_project_context_tool_impl`.
    """
    log_audit(
        requesting_agent_id,
        "bulk_update_project_context",
        {"update_count": len(updates_list)},
    )

    try:
        err, response_parts = _bulk_update_inline(
            requesting_agent_id, updates_list, is_admin=is_admin
        )
    except (sqlite3.Error, SQLAlchemyError) as e_sql:
        logger.error(f"Database error in bulk context update: {e_sql}", exc_info=True)
        return [
            mcp_types.TextContent(
                type="text", text=f"Database error in bulk update: {e_sql}"
            )
        ]
    except Exception as e:
        logger.error(f"Unexpected error in bulk context update: {e}", exc_info=True)
        return [
            mcp_types.TextContent(
                type="text", text=f"Unexpected error in bulk update: {e}"
            )
        ]
    if err is not None:
        return [mcp_types.TextContent(type="text", text=err)]

    # Phase 4: if the bulk write touched any worker-policy toggle,
    # emit a single tools/list_changed notification — workers care
    # whether the set of visible tools changed, not which key
    # flipped it.
    if any(
        _is_worker_policy_toggle(u.get("context_key", ""))
        for u in updates_list
    ):
        await _emit_tools_list_changed("__bulk__")

    return [mcp_types.TextContent(type="text", text="\n".join(response_parts))]


@requires("any")
async def update_project_context_tool_impl(
    arguments: Dict[str, Any],
) -> List[mcp_types.TextContent]:
    auth_token = arguments.get("token")

    # Support both single and bulk operations
    context_key_to_update = arguments.get("context_key")
    context_value_to_set = arguments.get("context_value")
    description_for_context = arguments.get("description")
    updates_list = arguments.get("updates")  # For bulk operations

    # @requires("any") guaranteed entry; resolve id + admin flag for the
    # per-key creator-ownership matrix (PR #52 — admins write anything,
    # workers only their own non-`config_*` keys).
    requesting_agent_id = get_agent_id(auth_token)
    is_admin = verify_token(auth_token, "admin")

    # Determine operation mode
    is_bulk_operation = updates_list is not None

    if is_bulk_operation:
        if not isinstance(updates_list, list) or len(updates_list) == 0:
            return [
                mcp_types.TextContent(
                    type="text",
                    text="Error: updates must be a non-empty list for bulk operations.",
                )
            ]
        return await _handle_bulk_context_update(
            requesting_agent_id, updates_list, is_admin=is_admin
        )
    else:
        # Single operation (backward compatibility)
        if not context_key_to_update or context_value_to_set is None:
            return [
                mcp_types.TextContent(
                    type="text",
                    text="Error: context_key and context_value are required for single updates.",
                )
            ]
        return await _handle_single_context_update(
            requesting_agent_id,
            context_key_to_update,
            context_value_to_set,
            description_for_context,
            is_admin=is_admin,
        )


# --- bulk_update_project_context tool ---
def _bulk_update_inline(
    requesting_agent_id: str,
    updates_list: List[Dict[str, Any]],
    *,
    is_admin: bool,
) -> Tuple[Optional[str], List[str]]:
    """Sync bulk-update body shared by the queued + inline entry points.

    Returns (error_message, response_parts). On the unauthorized path
    the error_message is non-None and response_parts is empty. On the
    success path error_message is None and response_parts is the
    rendered per-entry summary.

    Both `_handle_bulk_context_update` (queued) and the standalone
    `bulk_update_project_context_tool_impl` (inline) call this so the
    ownership rules + atomicity can't drift between the two surfaces.
    """
    session = SessionLocal()
    results: List[str] = []
    failed_updates: List[str] = []
    try:
        # Phase 1 — authorize every key up front.
        for upd in updates_list:
            key = upd.get("context_key")
            if not key:
                continue
            err = _check_write_authorization(
                session, requesting_agent_id, key, is_admin=is_admin
            )
            if err is not None:
                session.rollback()
                return err, []

        now_iso = datetime.datetime.now().isoformat()
        raw_conn = session.connection().connection
        cursor = raw_conn.cursor()

        # Phase 2 — apply each update.
        for i, update in enumerate(updates_list):
            try:
                context_key = update["context_key"]
                context_value = update["context_value"]
                description = update.get(
                    "description", f"Bulk update operation {i+1}"
                )

                value_json_str = json.dumps(context_value)

                existing = (
                    session.query(ProjectContext)
                    .filter(ProjectContext.context_key == context_key)
                    .one_or_none()
                )
                if existing is None:
                    session.add(
                        ProjectContext(
                            context_key=context_key,
                            value=value_json_str,
                            description=description,
                            created_at=now_iso,
                            created_by=requesting_agent_id,
                            updated_at=now_iso,
                            updated_by=requesting_agent_id,
                        )
                    )
                else:
                    existing.value = value_json_str
                    existing.updated_at = now_iso
                    existing.updated_by = requesting_agent_id
                    existing.description = description

                session.flush()
                results.append(f"✓ Updated '{context_key}'")

                log_agent_action_to_db(
                    cursor,
                    requesting_agent_id,
                    "bulk_updated_context",
                    details={
                        "context_key": context_key,
                        "operation": f"bulk_update_{i+1}",
                    },
                )

            except (TypeError, json.JSONEncodeError) as e_json:
                failed_updates.append(
                    f"✗ Failed '{update.get('context_key', 'unknown')}': Invalid JSON - {e_json}"
                )
            except Exception as e_update:
                failed_updates.append(
                    f"✗ Failed '{update.get('context_key', 'unknown')}': {str(e_update)}"
                )

        session.commit()

        response_parts = [
            f"Bulk update completed: {len(results)} successful, {len(failed_updates)} failed"
        ]
        if results:
            response_parts.append("\nSuccessful updates:")
            response_parts.extend(results)
        if failed_updates:
            response_parts.append("\nFailed updates:")
            response_parts.extend(failed_updates)
        logger.info(
            f"Bulk context update by '{requesting_agent_id}': {len(results)} successful, {len(failed_updates)} failed."
        )
        return None, response_parts
    finally:
        session.close()


@requires("any")
async def bulk_update_project_context_tool_impl(
    arguments: Dict[str, Any],
) -> List[mcp_types.TextContent]:
    """Public-facing bulk-update entry point (inline, no write queue).

    The MCP framework dispatcher runs tools on arbitrary asyncio loops
    (including ad-hoc loops created by `asyncio.run` in test/CLI
    contexts). Routing through the lifespan write queue from a
    different loop deadlocks — so this surface does its writes inline
    via a SQLAlchemy session, sharing the ownership + atomicity logic
    with the queued surface (`_handle_bulk_context_update`) through
    `_bulk_update_inline`.
    """
    auth_token = arguments.get("token")
    updates = arguments.get("updates", [])  # List of update operations

    # @requires("any") guaranteed entry; resolve id for the per-key
    # ownership matrix below.
    requesting_agent_id = get_agent_id(auth_token)

    if not updates or not isinstance(updates, list):
        return [
            mcp_types.TextContent(type="text", text="Error: updates array is required.")
        ]

    # Validate each update operation
    for i, update in enumerate(updates):
        if not isinstance(update, dict):
            return [
                mcp_types.TextContent(
                    type="text", text=f"Error: Update {i} must be an object."
                )
            ]
        if "context_key" not in update:
            return [
                mcp_types.TextContent(
                    type="text",
                    text=f"Error: Update {i} missing required 'context_key'.",
                )
            ]
        if "context_value" not in update:
            return [
                mcp_types.TextContent(
                    type="text",
                    text=f"Error: Update {i} missing required 'context_value'.",
                )
            ]

    log_audit(
        requesting_agent_id,
        "bulk_update_project_context",
        {"update_count": len(updates)},
    )

    is_admin = verify_token(auth_token, "admin")
    try:
        err, response_parts = _bulk_update_inline(
            requesting_agent_id, updates, is_admin=is_admin
        )
    except (sqlite3.Error, SQLAlchemyError) as e_sql:
        return [
            mcp_types.TextContent(
                type="text", text=f"Database error in bulk update: {e_sql}"
            )
        ]
    except Exception as e:
        return [
            mcp_types.TextContent(
                type="text", text=f"Unexpected error in bulk update: {e}"
            )
        ]
    if err is not None:
        return [mcp_types.TextContent(type="text", text=err)]

    # Phase 4: emit tools/list_changed once if any update was a
    # worker-policy toggle.
    if any(
        _is_worker_policy_toggle(u.get("context_key", ""))
        for u in updates
    ):
        await _emit_tools_list_changed("__bulk__")

    return [mcp_types.TextContent(type="text", text="\n".join(response_parts))]


# --- backup_project_context tool ---
@requires("admin")
async def backup_project_context_tool_impl(
    arguments: Dict[str, Any],
) -> List[mcp_types.TextContent]:
    auth_token = arguments.get("token")
    backup_name = arguments.get("backup_name")  # Optional custom backup name
    include_health_report = arguments.get(
        "include_health_report", True
    )  # Include health analysis in backup

    # @requires("admin") guaranteed entry; admin id is always "admin".
    requesting_agent_id = get_agent_id(auth_token)

    log_audit(
        requesting_agent_id, "backup_project_context", {"backup_name": backup_name}
    )

    session = SessionLocal()
    try:
        # Create backup
        backup_data = _create_context_backup(session, backup_name)

        # Add health analysis if requested
        if include_health_report:
            all_entries = backup_data["entries"]
            health_analysis = _analyze_context_health(all_entries)
            backup_data["health_report"] = health_analysis

        # Save backup to a file in the project directory (optional - could be database too)
        import os

        project_dir = os.environ.get("MCP_PROJECT_DIR", ".")
        backup_dir = os.path.join(project_dir, ".agent", "backups", "context")
        os.makedirs(backup_dir, exist_ok=True)

        backup_filename = f"{backup_data['backup_name']}.json"
        backup_path = os.path.join(backup_dir, backup_filename)

        with open(backup_path, "w", encoding="utf-8") as f:
            json.dump(backup_data, f, indent=2, ensure_ascii=False)

        # Generate response
        response_parts = [
            f"✅ **Context Backup Created**",
            f"   Name: {backup_data['backup_name']}",
            f"   Entries: {backup_data['total_entries']}",
            f"   File: {backup_path}",
            f"   Created: {backup_data['created_at']}",
        ]

        if include_health_report and "health_report" in backup_data:
            health = backup_data["health_report"]
            health_icon = (
                "🟢"
                if health["status"] == "excellent"
                else (
                    "🟡"
                    if health["status"] == "good"
                    else "🟠" if health["status"] == "needs_attention" else "🔴"
                )
            )

            response_parts.extend(
                [
                    "",
                    f"📊 **Health Report:** {health_icon} {health['status'].title()} ({health['health_score']}/100)",
                    f"   Issues: {health['json_errors']} JSON errors, {health['stale_entries']} stale entries",
                    f"   Recommendations: {len(health['recommendations'])} items",
                ]
            )

        response_parts.extend(
            [
                "",
                "💡 **Backup Usage:**",
                "• Use this backup to restore context in case of corruption",
                "• Store backup files securely - they contain sensitive project data",
                "• Regular backups recommended before major context changes",
            ]
        )

        # log_agent_action_to_db expects a cursor; reuse the session's
        # raw connection so the action lives in the same transaction.
        raw_conn = session.connection().connection
        cursor = raw_conn.cursor()
        log_agent_action_to_db(
            cursor,
            requesting_agent_id,
            "backup_project_context",
            backup_name,
            {"total_entries": backup_data["total_entries"], "backup_path": backup_path},
        )
        session.commit()

        return [mcp_types.TextContent(type="text", text="\n".join(response_parts))]

    except Exception as e:
        session.rollback()
        logger.error(f"Error creating context backup: {e}", exc_info=True)
        return [mcp_types.TextContent(type="text", text=f"Error creating backup: {e}")]
    finally:
        session.close()


# --- validate_context_consistency tool ---
@requires("any")
async def validate_context_consistency_tool_impl(
    arguments: Dict[str, Any],
) -> List[mcp_types.TextContent]:
    auth_token = arguments.get("token")

    # @requires("any") guaranteed entry; resolve id for audit only.
    requesting_agent_id = get_agent_id(auth_token)

    # Log audit
    log_audit(requesting_agent_id, "validate_context_consistency", {})

    session = SessionLocal()
    try:
        issues = []
        warnings = []

        # Get all context entries
        all_rows = (
            session.query(ProjectContext)
            .order_by(ProjectContext.context_key)
            .all()
        )
        all_entries = [_row_to_dict(r) for r in all_rows]

        if not all_entries:
            return [
                mcp_types.TextContent(
                    type="text", text="No project context entries found."
                )
            ]

        # Check 1: Invalid JSON values
        for entry in all_entries:
            try:
                json.loads(entry["value"])
            except json.JSONDecodeError as e:
                issues.append(f"Invalid JSON in '{entry['context_key']}': {e}")

        # Check 2: Duplicate or conflicting keys (case-insensitive)
        key_map = {}
        for entry in all_entries:
            key_lower = entry["context_key"].lower()
            if key_lower in key_map:
                issues.append(
                    f"Potential duplicate keys: '{key_map[key_lower]}' and '{entry['context_key']}'"
                )
            else:
                key_map[key_lower] = entry["context_key"]

        # Check 3: Missing descriptions
        missing_desc = [
            entry["context_key"]
            for entry in all_entries
            if not entry.get("description")
        ]
        if missing_desc:
            warnings.extend(
                [f"Missing description: '{key}'" for key in missing_desc[:10]]
            )
            if len(missing_desc) > 10:
                warnings.append(
                    f"... and {len(missing_desc) - 10} more missing descriptions"
                )

        # Check 4: Very old entries (potential staleness)
        import datetime as dt

        cutoff_date = (dt.datetime.now() - dt.timedelta(days=30)).isoformat()
        old_entries = [
            entry["context_key"]
            for entry in all_entries
            if (entry.get("updated_at") or "") < cutoff_date
        ]
        if old_entries:
            warnings.extend(
                [f"Old entry (>30 days): '{key}'" for key in old_entries[:5]]
            )
            if len(old_entries) > 5:
                warnings.append(f"... and {len(old_entries) - 5} more old entries")

        # Check 5: Unusually large values (potential bloat)
        large_entries = []
        for entry in all_entries:
            if len(entry["value"]) > 10000:  # 10KB threshold
                large_entries.append(
                    f"{entry['context_key']} ({len(entry['value'])} chars)"
                )
        if large_entries:
            warnings.extend([f"Large entry: {entry}" for entry in large_entries[:5]])
            if len(large_entries) > 5:
                warnings.append(f"... and {len(large_entries) - 5} more large entries")

        # Build response
        response_parts = [f"Context Consistency Validation Results"]
        response_parts.append(f"Total entries: {len(all_entries)}")

        if not issues and not warnings:
            response_parts.append("\n✅ No issues found! Context appears consistent.")
        else:
            if issues:
                response_parts.append(f"\n🚨 Critical Issues ({len(issues)}):")
                response_parts.extend([f"  {issue}" for issue in issues])

            if warnings:
                response_parts.append(f"\n⚠️  Warnings ({len(warnings)}):")
                response_parts.extend([f"  {warning}" for warning in warnings])

            response_parts.append("\nRecommendations:")
            if issues:
                response_parts.append("- Fix critical issues immediately")
                response_parts.append(
                    "- Use bulk_update_project_context for corrections"
                )
            if warnings:
                response_parts.append("- Review warnings for potential cleanup")
                response_parts.append(
                    "- Consider using delete_project_context for unused entries"
                )

        return [mcp_types.TextContent(type="text", text="\n".join(response_parts))]

    except SQLAlchemyError as e_sql:
        logger.error(
            f"Database error validating context consistency: {e_sql}", exc_info=True
        )
        return [
            mcp_types.TextContent(
                type="text", text=f"Database error validating context: {e_sql}"
            )
        ]
    except Exception as e:
        logger.error(
            f"Unexpected error validating context consistency: {e}", exc_info=True
        )
        return [
            mcp_types.TextContent(
                type="text", text=f"Unexpected error validating context: {e}"
            )
        ]
    finally:
        session.close()


# --- Register project context tools ---
def register_project_context_tools():
    register_tool(
        name="view_project_context",
        description="Smart project context viewer with health analysis, stale entry detection, and advanced filtering. Provides comprehensive insights into context quality and usage.",
        input_schema={
            "type": "object",
            "properties": {
                "token": {"type": "string", "description": "Authentication token. Optional if Authorization: Bearer header is supplied (recommended)."},
                "context_key": {
                    "type": "string",
                    "description": "Exact key to view (optional). If provided, search_query is ignored.",
                },
                "search_query": {
                    "type": "string",
                    "description": "Keyword search query (optional). Searches keys, descriptions, and values.",
                },
                # Smart analysis features
                "show_health_analysis": {
                    "type": "boolean",
                    "description": "Include comprehensive health metrics and analysis (default: false)",
                },
                "show_stale_entries": {
                    "type": "boolean",
                    "description": "Show only entries older than 30 days needing review (default: false)",
                },
                "include_backup_info": {
                    "type": "boolean",
                    "description": "Include backup recommendations and info (default: false)",
                },
                # Display and sorting options
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of entries to return (default: 50)",
                    "minimum": 1,
                    "maximum": 200,
                },
                "sort_by": {
                    "type": "string",
                    "description": "Sort entries by specified field (default: updated_at). 'last_updated' is accepted as a deprecated alias.",
                    "enum": ["key", "updated_at", "last_updated", "size"],
                    "default": "updated_at",
                },
            },
            "required": [],
            "additionalProperties": False,
        },
        implementation=view_project_context_tool_impl,
    )

    register_tool(
        name="update_project_context",  # main.py:1825
        description="Add or update a project context entry with a specific key. The value can be any JSON-serializable type.",
        input_schema={  # From main.py:1826-1839
            "type": "object",
            "properties": {
                "token": {
                    "type": "string",
                    "description": "Authentication token (agent or admin). Optional if Authorization: Bearer header is supplied (recommended).",
                },
                "context_key": {
                    "type": "string",
                    "description": "The exact key for the context entry (e.g., 'api.service_x.url').",
                },
                "context_value": {
                    "description": "The JSON-serializable value to set (e.g., string, number, list, dict).",
                    "anyOf": [
                        {"type": "string"},
                        {"type": "number"},
                        {"type": "boolean"},
                        {"type": "null"},
                        {"type": "object", "additionalProperties": True},
                        {"type": "array"}
                    ]
                },
                "description": {
                    "type": "string",
                    "description": "Optional description of this context entry.",
                },
            },
            "required": ["context_key", "context_value"],
            "additionalProperties": False,
        },
        implementation=update_project_context_tool_impl,
    )

    register_tool(
        name="bulk_update_project_context",
        description="Update multiple project context entries atomically. Essential for large-scale context corrections.",
        input_schema={
            "type": "object",
            "properties": {
                "token": {"type": "string", "description": "Authentication token. Optional if Authorization: Bearer header is supplied (recommended)."},
                "updates": {
                    "type": "array",
                    "description": "Array of update operations",
                    "items": {
                        "type": "object",
                        "properties": {
                            "context_key": {
                                "type": "string",
                                "description": "The context key to update",
                            },
                            "context_value": {
                                "description": "The new value (any JSON-serializable type)",
                                "anyOf": [
                                    {"type": "string"},
                                    {"type": "number"},
                                    {"type": "boolean"},
                                    {"type": "null"},
                                    {"type": "object"},
                                    {"type": "array"}
                                ]
                            },
                            "description": {
                                "type": "string",
                                "description": "Optional description for this update",
                            },
                        },
                        "required": ["context_key", "context_value"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["updates"],
            "additionalProperties": False,
        },
        implementation=bulk_update_project_context_tool_impl,
    )

    register_tool(
        name="backup_project_context",
        description="Create comprehensive backup of all project context with health analysis. Admin-only operation for data safety and recovery.",
        input_schema={
            "type": "object",
            "properties": {
                "token": {
                    "type": "string",
                    "description": "Admin authentication token. Optional if Authorization: Bearer header is supplied (recommended).",
                },
                "backup_name": {
                    "type": "string",
                    "description": "Optional custom backup name (auto-generated if not provided)",
                },
                "include_health_report": {
                    "type": "boolean",
                    "description": "Include health analysis in backup (default: true)",
                    "default": True,
                },
            },
            "required": [],
            "additionalProperties": False,
        },
        implementation=backup_project_context_tool_impl,
    )

    register_tool(
        name="validate_context_consistency",
        description="Check for inconsistencies, conflicts, and quality issues in project context. Critical for preventing context poisoning.",
        input_schema={
            "type": "object",
            "properties": {
                "token": {"type": "string", "description": "Authentication token. Optional if Authorization: Bearer header is supplied (recommended)."}
            },
            "required": [],
            "additionalProperties": False,
        },
        implementation=validate_context_consistency_tool_impl,
    )

    register_tool(
        name="delete_project_context",
        description="Delete project context entries permanently. Admin-only operation with safety checks for critical system keys.",
        input_schema={
            "type": "object",
            "properties": {
                "token": {
                    "type": "string",
                    "description": "Admin authentication token. Optional if Authorization: Bearer header is supplied (recommended).",
                },
                "context_key": {
                    "type": "string",
                    "description": "Single context key to delete (alternative to context_keys)",
                },
                "context_keys": {
                    "type": "array",
                    "description": "List of context keys to delete",
                    "items": {"type": "string"},
                    "minItems": 1,
                },
                "force_delete": {
                    "type": "boolean",
                    "description": "Force deletion even for critical system keys (default: false)",
                    "default": False,
                },
            },
            "required": [],
            "additionalProperties": False,
        },
        implementation=delete_project_context_tool_impl,
    )


@requires("any")
async def delete_project_context_tool_impl(
    arguments: Dict[str, Any],
) -> List[mcp_types.TextContent]:
    """Delete project context entries permanently.

    Phase 7b: per-key creator-ownership check applies (admins can delete
    anything; workers can delete only entries they themselves created
    and which are not `config_*`). The `force_delete` safety net on
    "critical" system keys is preserved as belt-and-suspenders.
    """
    auth_token = arguments.get("token")
    context_keys = arguments.get("context_keys", [])
    context_key = arguments.get("context_key")
    force_delete = arguments.get("force_delete", False)

    # @requires("any") guaranteed entry; resolve id + admin flag for the
    # per-key creator-ownership matrix in _check_write_authorization.
    requesting_agent_id = get_agent_id(auth_token)
    is_admin = verify_token(auth_token, required_role="admin")

    # Prepare list of keys to delete
    keys_to_delete = []
    if context_key:
        keys_to_delete.append(context_key)
    if context_keys:
        keys_to_delete.extend(context_keys)

    if not keys_to_delete:
        return [
            mcp_types.TextContent(
                type="text", text="Error: No context keys specified for deletion"
            )
        ]

    # Critical system keys that require force_delete
    critical_keys = [
        "config_admin_token",
        "server_startup",
        "database_version",
        "system_config",
        "mcp_server_url",
    ]

    # Check for critical keys
    critical_keys_found = []
    for key in keys_to_delete:
        for critical_pattern in critical_keys:
            if (
                key.startswith(critical_pattern.split("_")[0] + "_")
                or key == critical_pattern
            ):
                critical_keys_found.append(key)
                break

    if critical_keys_found and not force_delete:
        return [
            mcp_types.TextContent(
                type="text",
                text=f"Error: Cannot delete critical system keys without force_delete=true: {critical_keys_found}",
            )
        ]

    session = SessionLocal()
    try:
        # Per-key ownership check before any deletion runs.
        for key in keys_to_delete:
            err = _check_write_authorization(
                session, requesting_agent_id, key, is_admin=is_admin
            )
            if err is not None:
                return [mcp_types.TextContent(type="text", text=err)]

        # Fetch existing rows up front so we know what's actually there.
        existing_rows = (
            session.query(ProjectContext)
            .filter(ProjectContext.context_key.in_(keys_to_delete))
            .all()
        )
        existing_map = {r.context_key: r for r in existing_rows}

        if not existing_map:
            return [
                mcp_types.TextContent(
                    type="text",
                    text=f"Error: None of the specified keys exist in project context: {keys_to_delete}",
                )
            ]

        # Delete the keys
        deleted_count = 0
        deletion_details = []

        for key, row in existing_map.items():
            description = row.description if row.description else ""
            session.delete(row)
            deleted_count += 1
            deletion_details.append(
                {
                    "key": key,
                    "description": description,
                    "was_critical": key in critical_keys_found,
                }
            )

        session.flush()

        # Log the deletion action via the shared connection.
        raw_conn = session.connection().connection
        cursor = raw_conn.cursor()
        log_agent_action_to_db(
            cursor=cursor,
            agent_id=requesting_agent_id,
            action_type="deleted_context",
            details={
                "deleted_keys": [d["key"] for d in deletion_details],
                "critical_keys_deleted": critical_keys_found,
                "force_delete": force_delete,
                "total_deleted": deleted_count,
            },
        )

        session.commit()

        # Prepare response
        response_parts = [
            f"Deleted {deleted_count} project context entries successfully:"
        ]

        for detail in deletion_details:
            key_info = f"  • {detail['key']}"
            if detail["description"]:
                key_info += f" ({detail['description']})"
            if detail["was_critical"]:
                key_info += " [CRITICAL]"
            response_parts.append(key_info)

        if critical_keys_found:
            response_parts.append(
                f"\n⚠️  WARNING: {len(critical_keys_found)} critical system keys were deleted!"
            )
            response_parts.append(
                "System functionality may be affected. Consider backing up before restart."
            )

        response_parts.append(
            f"\nDeletion completed at: {datetime.datetime.now().isoformat()}"
        )

        return [mcp_types.TextContent(type="text", text="\n".join(response_parts))]

    except Exception as e:
        session.rollback()
        logger.error(f"Error in delete_project_context_tool_impl: {e}", exc_info=True)
        return [
            mcp_types.TextContent(
                type="text", text=f"Error deleting project context: {str(e)}"
            )
        ]
    finally:
        session.close()


# Call registration when this module is imported
register_project_context_tools()
