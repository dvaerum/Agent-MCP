# Agent-MCP/mcp_template/mcp_server_src/tools/project_context_tools.py
import json
import datetime
import re
import sqlite3
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

# ADR-0016: the ``config_*`` namespace lives in the ``project_settings``
# store (tools/project_settings_tools.py), NOT in project_context. This
# regex backs the WRITE/DELETE rejection only — ``_check_write_authorization``
# and the bulk-delete guard reject the whole namespace for every caller
# (admin included) with a pointer to the settings tools. It plays no part
# in the config write/delete rejection only.
_CONFIG_KEY_RE = re.compile(r"^config_", re.IGNORECASE)


# ADR-0017 (Wave 12 PR B): content-based secret detection is GONE.
# ``is_secret_key`` and the value/description embedded-secret scanner
# used to redact project_context reads + skip RAG indexing here. Both
# were unreliable in both directions (false positives hid legitimate
# memory notes; false negatives gave false confidence) and are deleted.
# memory (project_context), tasks, code, and markdown are shared project
# content — indexed and returned AS-IS. Real secrets live in sops refs
# or the operator-only, non-RAG project_settings store; protection is by
# AUTHORIZATION (who may read the project), not by guessing content.


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


# Global event-loop toggle (Phase: event-coordination). Flipping it
# OFF must wake every in-flight `wait_for_events` so they re-evaluate
# and return `stop_listening`; the wake fn is `wake_all_for_flag_recheck`
# (state.py). Kept separate from the worker-policy toggle above because
# the two changes require *different* wakes.
_LOOP_TOGGLE_KEY = "config_auto_event_loop_global"


def _is_loop_toggle(context_key: str) -> bool:
    """True if `context_key` is the global event-loop toggle whose flip
    must wake in-flight `wait_for_events` (see
    `agent_communication_tools.py`)."""
    return context_key == _LOOP_TOGGLE_KEY


async def emit_context_write_wakes(context_key: str) -> None:
    """Fire the post-write notify wakes a `project_context` write on
    `context_key` requires — the single source of truth for
    REST-vs-MCP notify parity (BL-R14-1).

    Two independent wakes, each keyed on what the write changed:

    * `config_allow_worker_*` (worker-capability toggle) →
      `_emit_tools_list_changed`: push `notifications/tools/list_changed`
      so subscribed workers re-fetch `tools/list` and can see/invoke a
      newly granted tool without waiting for a periodic refresh.
    * `config_auto_event_loop_global` (global event-loop toggle) →
      `wake_all_for_flag_recheck`: wake in-flight `wait_for_events` so
      they re-evaluate and return `stop_listening` when flipped OFF.

    BOTH operator-reachable write surfaces — REST `/api/memories`
    (create/update) and MCP `update_project_context` — call this so
    each fires the SAME wake set. Before this helper each surface fired
    only one: a capability grant over REST never pushed
    `tools/list_changed`, and a loop flip over MCP never woke waiters.

    Best-effort + defensive: a wake failure is logged, never raised —
    the toggle write itself is the source of truth, so clients converge
    on their next refresh even if the push/wake is lost.
    """
    if _is_worker_policy_toggle(context_key):
        await _emit_tools_list_changed(context_key)
    if _is_loop_toggle(context_key):
        try:
            g.wake_all_for_flag_recheck()
        except Exception as e:  # pragma: no cover - defensive
            logger.warning(
                "wake_all_for_flag_recheck failed after global "
                "toggle write (key=%s): %s",
                context_key,
                e,
            )


async def emit_context_write_wakes_bulk(context_keys) -> None:
    """Bulk sibling of :func:`emit_context_write_wakes` (arch-r6 #1).

    A bulk write touches N keys in one call; fire each wake AT MOST
    ONCE for the whole batch (not once per matching key) if ANY key in
    the batch matches, using the SAME predicates
    (`_is_worker_policy_toggle` / `_is_loop_toggle`) and the SAME
    underlying calls as the single-key seam above — so the two seams
    can't drift on what counts as a toggle or how the wake fires.

    Both bulk write surfaces (`_handle_bulk_context_update`, the
    write-queue path, and the standalone
    `bulk_update_project_context_tool_impl`, the inline path) must
    route through this so they fire the SAME wake set as each other
    and as the single-key `update_project_context` surface. Before
    this helper the two bulk surfaces hand-rolled the fan-out and
    drifted: `_handle_bulk_context_update` fired both halves, but
    `bulk_update_project_context_tool_impl` fired only the
    worker-policy half — a `config_auto_event_loop_global` flip via
    the standalone bulk tool left in-flight `wait_for_events` waiters
    hanging. This is the REST-vs-MCP notify-parity class BL-R14-1
    (see the docstring above) reintroduced on the third write surface.
    """
    keys = list(context_keys)
    if any(_is_worker_policy_toggle(k) for k in keys):
        await _emit_tools_list_changed("__bulk__")
    if any(_is_loop_toggle(k) for k in keys):
        try:
            g.wake_all_for_flag_recheck()
        except Exception as e:  # pragma: no cover - defensive
            logger.warning(
                "wake_all_for_flag_recheck failed after bulk "
                "loop-toggle write: %s",
                e,
            )


def _config_key_error() -> "PermissionDenied":
    # ADR-0016 (Wave 11): the config_* namespace lives in the dedicated
    # project_settings store — the knowledge write path rejects it for
    # EVERYONE (admin included), so config can't be smuggled back into
    # the memory store (where it would be RAG-indexed and re-open the
    # F009 redaction class).
    return PermissionDenied(
        reason=(
            "config_* keys moved to the project settings store "
            "(ADR-0016); use update_project_settings"
        )
    )


def _creator_mismatch_error(context_key: str, creator: str) -> "PermissionDenied":
    return PermissionDenied(
        reason=(
            f"key '{context_key}' was created by '{creator}'; only "
            f"its creator or admin can modify it"
        )
    )

from sqlalchemy import func, or_, select
from sqlalchemy.exc import SQLAlchemyError

from .registry import register_tool
from ..core.config import logger
from ..core import globals as g  # Not directly used here, but auth uses it
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
from ..db.engine import get_session
from ..db.models import ProjectContext
from ..db.actions.agent_actions_db import log_agent_action_to_db
from ..db.unit_of_work import unit_of_work
from ..repositories import project_context_repository as project_context_repo


# ── Wave 6 PR 3 helpers ──────────────────────────────────────────────


def _actor_label(principal: Optional[Principal]) -> str:
    """Best-effort audit-attribution label for a Principal.

    Mirrors :meth:`Principal.actor_label` with a defensive fallback to
    ``"unknown"`` if the principal is None or carries no identity. The
    pre-Wave-6 code passed ``get_agent_id(token)`` which would return
    ``None`` for an empty/invalid token; this helper guarantees a
    non-empty string so downstream audit-log INSERTs always have an
    ``agent_id`` value.
    """
    if principal is None:
        return "unknown"
    return principal.actor_label() or "unknown"


def _is_admin_principal(principal: Optional[Principal]) -> bool:
    """Operator-tier check using :meth:`Principal.has_capability`.

    Wave 9 PR 3: gates on ``system.config.write`` — the per-project
    operator write marker present in
    ``PROJECT_ROLE_BUNDLES["operator"]`` and short-circuited by the
    sysadmin wildcard. Replaces the legacy ``has_role("admin")``
    bridge call. The new check is strictly tighter than the bridge:

    * REST seam → ``operator_session`` Principal with
      ``project_role="operator"`` → has the cap → admitted.
    * REST seam → ``operator_session`` Principal with
      ``project_role="viewer"`` → does NOT have the cap → denied.
      Viewers shouldn't bypass the secret-redaction filter or
      override the per-key creator-ownership matrix on writes; the
      bridge admitted them by accident (``has_role("admin")`` was
      identity-only and ignored ``project_role``).
    * MCP wire (any agent) → ``agent_bearer`` Principal → no
      ``system.config.write`` in either agent-role bundle → denied;
      the worker/manager distinction doesn't matter at this gate.
    * Sysadmin → wildcard short-circuit → admitted regardless of
      ``project_role``.

    Callers that bypassed the dispatcher (None principal) are simply
    not admitted as admin.
    """
    if principal is None:
        return False
    return principal.has_capability("system.config.write")


def _deny_viewer_tier_write(
    principal: Optional[Principal], capability: str
) -> Optional[PermissionDenied]:
    """Reject operator-path callers who lack the memories-write cap.

    SEC1-class fix (companion to #273 / #274). The REST surface 403s
    every viewer mutation at ``router/auth_middleware.py``
    (``method in _MUTATION_METHODS and role != "operator"``). The MCP
    wire, however, signs a ``role="viewer"`` forwarding header and
    delegates authorization to each tool — and the project_context
    write tools gated ONLY on identity
    (:func:`_requires_authenticated_caller`) plus a per-key
    creator-ownership matrix (:func:`_check_write_authorization`) that
    treated a viewer exactly like a worker. A read-only viewer could
    therefore create arbitrary new context keys and edit / delete their
    own. project_context rows feed the RAG corpus that operators +
    worker agents consume, so this was a stored-injection /
    RAG-poisoning primitive from a read-only principal.

    This gate mirrors the REST intent (viewer = read-only) by requiring
    the operator-held memories-write ``capability`` that viewers do NOT
    carry — see ``PROJECT_ROLE_BUNDLES`` in ``core/capabilities.py``:
    the viewer bundle holds ``memories.view`` only, the operator bundle
    adds ``memories.create`` / ``memories.update`` / ``memories.delete``.

    Scope is deliberately the operator-path kinds ONLY
    (``operator_session`` / ``forwarding_header``). Agent bearers
    (worker / manager) are untouched — they legitimately author context
    — so the per-key ownership matrix keeps governing them exactly as
    before. ``None`` is left to the earlier
    :func:`_requires_authenticated_caller` gate (this helper is only
    ever called after it).
    """
    if principal is None:
        return None
    if (
        principal.kind in ("operator_session", "forwarding_header")
        and not principal.has_capability(capability)
    ):
        return PermissionDenied(
            reason=(
                "viewer-tier operator cannot mutate project context "
                "(read-only project membership)"
            )
        )
    return None


def _requires_authenticated_caller(
    principal: Optional[Principal],
) -> Optional[PermissionDenied]:
    """Mirror ``@requires("any")`` semantics in the migrated tools.

    Returns ``PermissionDenied`` (a :data:`ToolResult` variant) when
    no caller identity is in hand. Admits:

    * any ``agent_bearer`` Principal (the legacy ``@requires("any")``
      gate which checked ``get_agent_id(token)`` admitted exactly
      these);
    * any operator-path Principal (``operator_session`` /
      ``forwarding_header``), so the migrated tools are also callable
      from the REST seam if a future handler decides to dispatch
      through here instead of writing the table directly. Includes
      viewer-tier operators (read-only project members) — the gate
      here is "do we have an authenticated identity?", per-key
      authorization happens further down via
      :func:`_is_admin_principal`.
    * any sysadmin caller.

    Wave 9 PR 3: the operator branch is an identity check on
    ``principal.kind`` (not a capability check) — per the Wave 9
    plan's mapping, "any caller?" questions stay as identity checks
    rather than back-fitting a capability. ``_is_admin_principal``
    (the per-key admin override) is what migrated to a cap; this
    helper's contract is broader.
    """
    if principal is None:
        return PermissionDenied(
            reason="Valid token or operator session required"
        )
    if principal.kind == "agent_bearer":
        return None
    if principal.kind in ("operator_session", "forwarding_header"):
        return None
    return PermissionDenied(
        reason="Valid token or operator session required"
    )


def _analyze_context_health(context_entries: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Analyze project context health and identify issues"""
    if not context_entries:
        # Empty context still returns the FULL shape — every consumer
        # (backup report, view_project_context health block) reads
        # health_score / stale_entries / json_errors / recommendations
        # unconditionally, so a truncated dict here KeyError'd them.
        # ``status`` stays "no_data" (a distinct value the dashboard's
        # lib/api.ts models); the numeric fields default to a clean
        # zero-issue / full-score baseline.
        return {
            "status": "no_data",
            "health_score": 100.0,
            "total": 0,
            "stale_entries": 0,
            "json_errors": 0,
            "large_entries": 0,
            "issues": [],
            "warnings": [],
            "recommendations": [
                "No context entries yet - add project context to enable "
                "health analysis"
            ],
        }

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
    connection,
    requesting_agent_id: str,
    context_key: str,
    *,
    is_admin: bool,
) -> Optional[PermissionDenied]:
    """Return None if the caller may write/delete `context_key`, else
    a :class:`PermissionDenied` carrying a specific human-readable
    denial reason.

    Rules (Phase 7b, amended by ADR-0016):
    - config_* key: forbidden for EVERYONE (admin included) — the
      config namespace lives in the project_settings store now; this
      write path only holds agent-authored knowledge.
    - Admin: otherwise always authorized.
    - Non-admin + existing key + creator != self: forbidden.
    - Non-admin + new key (no row yet): allowed.
    - Non-admin + existing key + creator == self: allowed.

    arch-r4 #6: reads `created_by` via ``project_context_repo.get()``
    against the caller's open ``unit_of_work().cursor``, so the check
    sees pending changes inside the same open transaction (was a
    SQLAlchemy session query pre-migration; same in-transaction
    visibility guarantee, now on the raw-sqlite seam).

    Returns the TYPED denial directly (round-3 arch-deepening #3) —
    callers propagate it as-is instead of round-tripping through a
    ``"Unauthorized: "``-prefixed string that they then strip and
    re-wrap in :class:`PermissionDenied`. The MCP wire renderer
    (``core/tool_result.py::render_as_text_content``) already prefixes
    every ``PermissionDenied`` with ``"Unauthorized: "``, so the old
    round trip was a dead no-op that just duplicated the 4-line
    strip-and-rewrap at every call site.
    """
    # ADR-0016: checked BEFORE the admin early-return — config_* is
    # rejected for every caller on the knowledge write path.
    if _CONFIG_KEY_RE.match(context_key):
        return _config_key_error()
    if is_admin:
        return None
    existing = project_context_repo.get(context_key, connection=connection)
    if existing is None:
        return None
    creator = existing["created_by"]
    # Legacy rows where created_by is NULL (pre-migration backfill edge
    # case) cannot be safely attributed — treat as admin-only so workers
    # can't claim them.
    if creator is None:
        return _creator_mismatch_error(context_key, "(unknown — legacy entry)")
    if creator != requesting_agent_id:
        return _creator_mismatch_error(context_key, creator)
    return None


def _create_context_backup(
    connection, backup_name: str = None
) -> Dict[str, Any]:
    """Create a backup of all project context data.

    arch-r4 #6: reads via ``project_context_repo.list_all()`` against
    the caller's open ``unit_of_work().cursor`` (was a SQLAlchemy
    session query pre-migration).
    """
    if not backup_name:
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_name = f"context_backup_{timestamp}"

    rows = project_context_repo.list_all(connection=connection)

    backup_data = {
        "backup_name": backup_name,
        "created_at": datetime.datetime.now().isoformat(),
        "total_entries": len(rows),
        "entries": rows,
    }

    return backup_data


# --- view_project_context tool ---
# Original logic from main.py: lines 1411-1465 (view_project_context_tool function)
async def view_project_context_tool_impl(
    arguments: Dict[str, Any],
    *,
    principal: Optional[Principal] = None,
) -> ToolResult:
    """Wave 6 PR 3 — Principal + ToolResult migration.

    Policy: was ``@requires("any")`` — any authenticated caller
    admits. Now expressed via :func:`_requires_authenticated_caller`
    which admits agent bearers + operator-tier callers, then gated on
    the ``memories.view`` capability. ADR-0017: project_context is
    shared knowledge — there is no content-based secret redaction on the
    read path any more.
    """
    denied = _requires_authenticated_caller(principal)
    if denied is not None:
        return denied

    # Reads require the ``memories.view`` capability. This is a no-op for
    # every legitimate caller — viewer + operator project-role bundles carry
    # it, so do the worker + manager agent-role bundles, and sysadmin holds
    # the wildcard. What it DENIES is the one real over-admit the identity-
    # only gate above let through: an ``agent_bearer`` whose ``agent_role``
    # is ``None`` (a malformed token → empty capability bundle) could read
    # project context with zero caps. This mirrors the ``rag.query`` gate
    # ``ask_project_rag`` already added to close the same empty-bearer class.
    if not principal.has_capability("memories.view"):
        return PermissionDenied(
            reason="memories.view capability required to read project context"
        )

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
    # Limit results. Clamp to [1, 200] defensively: the tool schema
    # already declares minimum/maximum, but jsonschema validation is
    # skipped when the optional dependency is absent — an unclamped
    # ``max_results=-1`` becomes ``LIMIT -1`` (SQLite = no limit → full
    # table dump). Coerce + clamp so the LIMIT is always sane.
    try:
        max_results = max(1, min(int(arguments.get("max_results", 50)), 200))
    except (TypeError, ValueError):
        max_results = 50
    # Sort by: key, updated_at, size. Accept `last_updated` as a
    # backward-compatible alias for `updated_at` so existing dashboard
    # builds + MCP clients don't break mid-deploy.
    sort_by = arguments.get("sort_by", "updated_at")
    if sort_by == "last_updated":
        sort_by = "updated_at"

    # Wave 6 PR 3: identity for audit comes from the Principal, not a
    # ``get_agent_id(token)`` resolve.
    requesting_agent_id = _actor_label(principal)

    # Log audit (main.py:1417)
    log_audit(
        requesting_agent_id,
        "view_project_context",
        {"context_key": context_key_filter, "search_query": search_query_filter},
    )

    results_list: List[Dict[str, Any]] = []
    response_message: str = ""

    # arch-r4 #6: read-only body — ``get_session()`` (commit-on-exit,
    # rollback-on-exception, always-close) replaces the hand-rolled
    # ``SessionLocal()`` + manual close-in-every-branch pattern. Since
    # this body never writes, the commit-on-exit is a no-op; the win is
    # a single owner for the session lifecycle instead of one
    # ``session.close()`` call per except branch plus a defensive
    # double-close in ``finally``.
    with get_session() as session:
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

            # ADR-0017 (Wave 12 PR B): no content-based secret redaction.
            # project_context is shared project knowledge, returned in full
            # to any authenticated caller (read gated on ``memories.view``
            # above). Real secrets belong in the operator-only
            # project_settings store, not memory.

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
            return Failed(
                message=f"Database error viewing project context: {e_sql}"
            )
        except (
            json.JSONDecodeError
        ) as e_json:  # Should be caught per-item, but as a fallback
            logger.error(
                f"Error decoding JSON from project_context table during bulk view: {e_json}",
                exc_info=True,
            )  # main.py:1465
            return Failed(
                message="Error decoding stored project context value(s)."
            )
        except Exception as e:
            logger.error(f"Unexpected error viewing project context: {e}", exc_info=True)
            return Failed(message=f"An unexpected error occurred: {e}")

    return Ok(
        data={
            "entries": results_list,
            "count": len(results_list),
            "filters": {
                "context_key": context_key_filter,
                "search_query": search_query_filter,
                "show_stale_entries": show_stale_entries,
                "sort_by": sort_by,
                "max_results": max_results,
            },
        },
        message=response_message,
    )


# --- update_project_context tool ---
def _single_update_inline(
    requesting_agent_id: str,
    context_key_to_update: str,
    value_json_str: str,
    description_for_context: Optional[str],
    *,
    is_admin: bool,
) -> Optional[PermissionDenied]:
    """Sync single-update body — returns a typed denial or None.

    arch-r4 #6: the unit-of-work owns the transaction. The upsert (via
    ``project_context_repo``) and its ``updated_context`` DB-audit row
    commit atomically on ``u.cursor`` — replaces the
    ``session.connection().connection`` ORM-drill-through and the
    hand-rolled ``session.commit()``/``rollback()``/``close()`` trio.
    An authorization failure (or any exception) returns/raises before
    anything is written, so the scope's clean-exit commit is a no-op
    and nothing needs to be undone; any exception after a write is
    rolled back by the scope itself (emit-iff-commit).

    Still inline (not queued) for the same reason as the bulk path:
    tests + test-style entry points run tools on ad-hoc asyncio loops
    and deadlock when the body posts into the lifespan write queue.
    """
    with unit_of_work() as u:
        cursor = u.cursor

        err = _check_write_authorization(
            cursor,
            requesting_agent_id,
            context_key_to_update,
            is_admin=is_admin,
        )
        if err is not None:
            return err

        # BL-R22-1: partial-update parity with the REST handler
        # (memories.py: `if description is not None: row.description =
        # description`). `description_for_context` is
        # `arguments.get("description")`, so `None` means the caller
        # omitted it — a value-only update must PRESERVE the existing
        # description, not NULL it. On INSERT the repo stores whatever
        # `description_for_context` is (including None) unconditionally.
        project_context_repo.upsert(
            context_key_to_update,
            value_json_str,
            description_for_context,
            description_provided=description_for_context is not None,
            actor=requesting_agent_id,
            connection=cursor,
        )

        log_agent_action_to_db(
            cursor,
            requesting_agent_id,
            "updated_context",
            details={"context_key": context_key_to_update, "action": "set/update"},
        )

        logger.info(
            f"Project context for key '{context_key_to_update}' updated by '{requesting_agent_id}'."
        )
        return None


async def _handle_single_context_update(
    requesting_agent_id: str,
    context_key_to_update: str,
    context_value_to_set: Any,
    description_for_context: Optional[str] = None,
    *,
    is_admin: bool,
) -> ToolResult:
    """Handle single context update operation.

    Phase 7b: authorizes the write per-key. Admins always pass; workers
    pass only if the key is non-`config_*` and either new or self-owned.
    On insert, stamps `created_at` / `created_by`; on update, leaves
    those untouched and refreshes `updated_at` / `updated_by`.

    Wave 6 PR 3 — returns :data:`ToolResult` instead of legacy
    ``list[TextContent]``. The authorisation-error path returns
    :class:`PermissionDenied` (which the REST adapter maps to 403 and
    the MCP renderer renders as ``Unauthorized: ...``); the JSON-
    serialisation path returns :class:`Invalid` (400); DB errors
    return :class:`Failed` (500).
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
        return Invalid(
            field="context_value",
            message=(
                f"Provided context_value is not JSON serializable: {e_type}"
            ),
        )

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
        return Failed(message=f"Database error updating project context: {e_sql}")
    except Exception as e:
        logger.error(
            f"Unexpected error updating project context for key '{context_key_to_update}': {e}",
            exc_info=True,
        )
        return Failed(message=f"Unexpected error updating project context: {e}")

    if err is not None:
        # ``_single_update_inline`` propagates the typed
        # :class:`PermissionDenied` straight from
        # ``_check_write_authorization`` — no string round trip.
        return err

    # BL-R14-1: fire the full wake set this key requires — worker-policy
    # toggle → tools/list_changed, loop toggle → wake_all_for_flag_recheck.
    # The MCP surface previously fired only the tools/list_changed half,
    # so a loop-toggle flip over MCP never woke in-flight waiters. The
    # helper is best-effort and safe outside a request context.
    await emit_context_write_wakes(context_key_to_update)

    return Ok(
        data={"context_key": context_key_to_update},
        message=(
            f"Project context updated successfully for key "
            f"'{context_key_to_update}'."
        ),
    )


async def _handle_bulk_context_update(
    requesting_agent_id: str,
    updates_list: List[Dict[str, Any]],
    *,
    is_admin: bool,
) -> ToolResult:
    """Handle bulk context update operations atomically.

    Phase 7b: every entry is authorized before any write lands. If a
    single entry fails the ownership check, the whole batch rolls back
    and the caller receives the specific error message.

    Routes through the shared inline helper for the same loop-affinity
    reason described in `bulk_update_project_context_tool_impl`.

    Wave 6 PR 3 — returns :data:`ToolResult`. The ownership-error
    path returns :class:`PermissionDenied`; the data payload on
    success carries the per-entry results list so REST callers see
    structured "what landed and what didn't" information.
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
        return Failed(message=f"Database error in bulk update: {e_sql}")
    except Exception as e:
        logger.error(f"Unexpected error in bulk context update: {e}", exc_info=True)
        return Failed(message=f"Unexpected error in bulk update: {e}")
    if err is not None:
        # ``_bulk_update_inline`` propagates the typed
        # :class:`PermissionDenied` straight from
        # ``_check_write_authorization`` — no string round trip.
        return err

    # arch-r6 #1: fire the full wake set this batch requires —
    # worker-policy toggle → tools/list_changed, loop toggle →
    # wake_all_for_flag_recheck — via the SAME seam the single-key
    # path uses, so the two bulk surfaces can't drift from each other
    # or from `update_project_context`.
    await emit_context_write_wakes_bulk(
        u.get("context_key", "") for u in updates_list
    )

    return Ok(
        data={
            "updates_attempted": len(updates_list),
            "summary_lines": response_parts,
        },
        message="\n".join(response_parts),
    )


async def update_project_context_tool_impl(
    arguments: Dict[str, Any],
    *,
    principal: Optional[Principal] = None,
) -> ToolResult:
    """Wave 6 PR 3 — Principal + ToolResult migration.

    The decorator was ``@requires("any")``; the gate now lives in
    :func:`_requires_authenticated_caller`. The per-key
    creator-ownership matrix uses :func:`_is_admin_principal` (with
    the bridge-era ContextVar fallback) for ``is_admin`` so the
    behaviour matches the pre-migration legacy
    ``verify_token(token, "admin")`` semantics during the Wave 6
    window.
    """
    denied = _requires_authenticated_caller(principal)
    if denied is not None:
        return denied

    # SEC1: operator-path viewers are read-only; deny them here before
    # the per-key ownership matrix (which treats them like a worker).
    viewer_denied = _deny_viewer_tier_write(principal, "memories.update")
    if viewer_denied is not None:
        return viewer_denied

    # Support both single and bulk operations
    context_key_to_update = arguments.get("context_key")
    context_value_to_set = arguments.get("context_value")
    description_for_context = arguments.get("description")
    updates_list = arguments.get("updates")  # For bulk operations

    requesting_agent_id = _actor_label(principal)
    is_admin = _is_admin_principal(principal)

    # ADR-0016: the config_aoe_* sysadmin gate that used to sit here is
    # unreachable now — _check_write_authorization rejects the WHOLE
    # config_* namespace (admin included) before any write; the AoE
    # tier-gate lives on the settings write path
    # (tools/project_settings_tools.py).

    # Determine operation mode
    is_bulk_operation = updates_list is not None

    if is_bulk_operation:
        if not isinstance(updates_list, list) or len(updates_list) == 0:
            return Invalid(
                field="updates",
                message="updates must be a non-empty list for bulk operations.",
            )
        return await _handle_bulk_context_update(
            requesting_agent_id, updates_list, is_admin=is_admin
        )
    else:
        # Single operation (backward compatibility)
        if not context_key_to_update or context_value_to_set is None:
            return Invalid(
                field="context_key" if not context_key_to_update else "context_value",
                message=(
                    "context_key and context_value are required for "
                    "single updates."
                ),
            )
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
) -> Tuple[Optional[PermissionDenied], List[str]]:
    """Sync bulk-update body shared by the queued + inline entry points.

    Returns (denial, response_parts). On the unauthorized path the
    denial is a non-None :class:`PermissionDenied` and response_parts
    is empty. On the success path denial is None and response_parts is
    the rendered per-entry summary.

    Both `_handle_bulk_context_update` (queued) and the standalone
    `bulk_update_project_context_tool_impl` (inline) call this so the
    ownership rules + atomicity can't drift between the two surfaces.

    arch-r4 #6: one ``unit_of_work()`` scope owns the whole batch — the
    Phase 1 all-keys-authorized-first / Phase 2 per-item apply-and-log
    shape is unchanged, but every write + its ``bulk_updated_context``
    DB-audit row goes through ``u.cursor`` (was
    ``session.connection().connection``). A Phase 1 authorization
    failure returns before any write, so the scope's commit is a no-op;
    a per-item exception in Phase 2 is still swallowed into
    ``failed_updates`` (intentional partial-success semantics — the
    batch is atomic on AUTHORIZATION, not on per-item success) and the
    scope commits whatever succeeded, exactly matching the legacy
    single ``session.commit()`` at the end of the batch.
    """
    results: List[str] = []
    failed_updates: List[str] = []
    with unit_of_work() as u:
        cursor = u.cursor

        # Phase 1 — authorize every key up front.
        for upd in updates_list:
            key = upd.get("context_key")
            if not key:
                continue
            err = _check_write_authorization(
                cursor, requesting_agent_id, key, is_admin=is_admin
            )
            if err is not None:
                return err, []

        # Phase 2 — apply each update.
        for i, update in enumerate(updates_list):
            try:
                context_key = update["context_key"]
                context_value = update["context_value"]
                # BL-R22-1: distinguish "caller explicitly supplied a
                # description for this item" from "omitted". The junk
                # `"Bulk update operation N"` default must ONLY seed a
                # fresh CREATE — on an UPDATE a value-only item must
                # PRESERVE the existing description (REST partial-update
                # parity), so `.get(..., default)` alone can't be used.
                description_provided = "description" in update
                description = update.get(
                    "description", f"Bulk update operation {i+1}"
                )

                value_json_str = json.dumps(context_value)

                project_context_repo.upsert(
                    context_key,
                    value_json_str,
                    description,
                    description_provided=description_provided,
                    actor=requesting_agent_id,
                    connection=cursor,
                )

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


async def bulk_update_project_context_tool_impl(
    arguments: Dict[str, Any],
    *,
    principal: Optional[Principal] = None,
) -> ToolResult:
    """Public-facing bulk-update entry point (inline, no write queue).

    The MCP framework dispatcher runs tools on arbitrary asyncio loops
    (including ad-hoc loops created by `asyncio.run` in test/CLI
    contexts). Routing through the lifespan write queue from a
    different loop deadlocks — so this surface does its writes inline
    via a SQLAlchemy session, sharing the ownership + atomicity logic
    with the queued surface (`_handle_bulk_context_update`) through
    `_bulk_update_inline`.

    Wave 6 PR 3 — Principal + ToolResult migration. Authentication
    gate moves from ``@requires("any")`` to
    :func:`_requires_authenticated_caller`; ``is_admin`` resolved
    from :func:`_is_admin_principal`.
    """
    denied = _requires_authenticated_caller(principal)
    if denied is not None:
        return denied

    # SEC1: operator-path viewers are read-only (see
    # _deny_viewer_tier_write); block before the per-key ownership matrix.
    viewer_denied = _deny_viewer_tier_write(principal, "memories.update")
    if viewer_denied is not None:
        return viewer_denied

    updates = arguments.get("updates", [])  # List of update operations
    requesting_agent_id = _actor_label(principal)

    # ADR-0016: the batch-level config_aoe_* sysadmin gate is gone —
    # _check_write_authorization (run per-entry before any write lands)
    # rejects every config_* key for every caller now.

    if not updates or not isinstance(updates, list):
        return Invalid(
            field="updates", message="updates array is required."
        )

    # Validate each update operation
    for i, update in enumerate(updates):
        if not isinstance(update, dict):
            return Invalid(
                field=f"updates[{i}]",
                message=f"Update {i} must be an object.",
            )
        if "context_key" not in update:
            return Invalid(
                field=f"updates[{i}].context_key",
                message=f"Update {i} missing required 'context_key'.",
            )
        if "context_value" not in update:
            return Invalid(
                field=f"updates[{i}].context_value",
                message=f"Update {i} missing required 'context_value'.",
            )

    log_audit(
        requesting_agent_id,
        "bulk_update_project_context",
        {"update_count": len(updates)},
    )

    is_admin = _is_admin_principal(principal)
    try:
        err, response_parts = _bulk_update_inline(
            requesting_agent_id, updates, is_admin=is_admin
        )
    except (sqlite3.Error, SQLAlchemyError) as e_sql:
        return Failed(message=f"Database error in bulk update: {e_sql}")
    except Exception as e:
        return Failed(message=f"Unexpected error in bulk update: {e}")
    if err is not None:
        # ``_bulk_update_inline`` propagates the typed
        # :class:`PermissionDenied` straight from
        # ``_check_write_authorization`` — no string round trip.
        return err

    # arch-r6 #1: fire the full wake set this batch requires — was
    # worker-policy-toggle-only (the standalone bulk tool never woke
    # in-flight `wait_for_events` waiters when a batch flipped
    # `config_auto_event_loop_global`, unlike the queued bulk path and
    # the single-key `update_project_context`). Routes through the
    # same seam as both siblings so the three write surfaces can't
    # drift again.
    await emit_context_write_wakes_bulk(
        u.get("context_key", "") for u in updates
    )

    return Ok(
        data={
            "updates_attempted": len(updates),
            "summary_lines": response_parts,
        },
        message="\n".join(response_parts),
    )


# --- create_project_context tool ---
def _create_context_inline(
    requesting_agent_id: str,
    context_key: str,
    value_json_str: str,
    description: Optional[str],
    *,
    is_admin: bool,
) -> Optional[ToolResult]:
    """Sync INSERT-only body — returns an error :data:`ToolResult` or None.

    INSERT-only (unlike ``_single_update_inline``'s upsert): an existing
    key yields :class:`Conflict` (REST 409) rather than an overwrite. The
    per-key creator-ownership matrix (:func:`_check_write_authorization`)
    still governs which caller may create the key — operators always pass
    (``is_admin``); agent bearers pass for a non-``config_*`` key exactly
    as the update path admits them.

    Inline (not queued) for the same loop-affinity reason as the update
    path — see :func:`_single_update_inline`: tools run on ad-hoc asyncio
    loops that deadlock on the lifespan write queue.

    arch-r4 #6: the unit-of-work owns the transaction — the INSERT (via
    ``project_context_repo.create_new``) and its ``created_memory``
    DB-audit row commit atomically on ``u.cursor``, replacing the
    ``session.connection().connection`` ORM-drill-through.
    """
    with unit_of_work() as u:
        cursor = u.cursor

        err = _check_write_authorization(
            cursor, requesting_agent_id, context_key, is_admin=is_admin
        )
        if err is not None:
            # ``_check_write_authorization`` returns the typed
            # :class:`PermissionDenied` directly — no string round trip.
            return err

        row = project_context_repo.create_new(
            context_key,
            value_json_str,
            description,
            actor=requesting_agent_id,
            connection=cursor,
        )
        if row is None:
            return Conflict(reason="Memory with this key already exists")

        # Audit through the same cursor so the action lands in the SAME
        # transaction as the project_context insert.
        log_agent_action_to_db(
            cursor,
            requesting_agent_id,
            "created_memory",
            details={"context_key": context_key},
        )
        logger.info(
            f"Project context created for key '{context_key}' by "
            f"'{requesting_agent_id}'."
        )
        return None


async def create_project_context_tool_impl(
    arguments: Dict[str, Any],
    *,
    principal: Optional[Principal] = None,
) -> ToolResult:
    """Create a NEW project_context entry (INSERT-only; 409 on existing).

    E3 (arch-deepening): the create choreography that
    ``app/routers/memories.py::create_memory_api_route`` hand-rolled inline
    (SQLAlchemy INSERT + ``created_memory`` audit + the BL-R14-1 post-write
    wake set) now lives here ONCE, so the REST route and this MCP tool are
    ONE implementation.

    arch-r4 #6: routes through ``unit_of_work()`` + ``project_context_repo``
    (parameterized SQL on ``u.cursor``), matching its ``update_``/``delete_``
    siblings — this is the "ORM-session-aware unit_of_work follow-up" the
    arch-deepening plan's design notes flagged as a separate PR.

    Auth mirrors the siblings: authenticated caller
    (:func:`_requires_authenticated_caller`) + viewer-tier operators denied
    (:func:`_deny_viewer_tier_write`, ``memories.create``) + the per-key
    creator-ownership matrix (:func:`_check_write_authorization`). Operator
    sessions (the REST seam) always pass; agent bearers pass subject to the
    ownership matrix — the same surface ``update_project_context`` already
    exposes for its insert branch, so no new privilege opens.
    """
    denied = _requires_authenticated_caller(principal)
    if denied is not None:
        return denied

    # SEC1: operator-path viewers are read-only; deny before the per-key
    # ownership matrix (which would otherwise treat them like a worker).
    viewer_denied = _deny_viewer_tier_write(principal, "memories.create")
    if viewer_denied is not None:
        return viewer_denied

    context_key = arguments.get("context_key")
    context_value = arguments.get("context_value")
    description = arguments.get("description")

    if not context_key:
        return Invalid(
            field="context_key", message="context_key is required"
        )

    # Positive key allowlist (string_utils.MEMORY_KEY_RE): letters, digits,
    # and . _ / - only. '/' is the allowed namespacing convention. Enforced
    # here so MCP-wire agents get the same gate as the REST create handler.
    from ..utils.string_utils import is_valid_memory_key

    if not is_valid_memory_key(context_key):
        return Invalid(
            field="context_key",
            message=(
                "context_key may contain only letters, digits, and . _ / - "
                "(A-Z a-z 0-9 . _ / -)."
            ),
        )

    # ADR-0016: config_* (AoE gate included) is rejected wholesale by
    # _check_write_authorization inside _create_context_inline.

    requesting_agent_id = _actor_label(principal)
    is_admin = _is_admin_principal(principal)

    log_audit(
        requesting_agent_id,
        "create_project_context",
        {"context_key": context_key},
    )

    try:
        value_json_str = json.dumps(context_value)
    except TypeError as e_type:
        logger.error(
            f"Value provided for project context key '{context_key}' is "
            f"not JSON serializable: {e_type}"
        )
        return Invalid(
            field="context_value",
            message=(
                f"Provided context_value is not JSON serializable: {e_type}"
            ),
        )

    try:
        err_result = _create_context_inline(
            requesting_agent_id,
            context_key,
            value_json_str,
            description,
            is_admin=is_admin,
        )
    except (sqlite3.Error, SQLAlchemyError) as e_sql:
        logger.error(
            f"Database error creating project context for key "
            f"'{context_key}': {e_sql}",
            exc_info=True,
        )
        return Failed(
            message=f"Database error creating project context: {e_sql}"
        )
    except Exception as e:
        logger.error(
            f"Unexpected error creating project context for key "
            f"'{context_key}': {e}",
            exc_info=True,
        )
        return Failed(
            message=f"Unexpected error creating project context: {e}"
        )

    if err_result is not None:
        return err_result

    # BL-R14-1: fire the full post-write wake set this key requires —
    # worker-policy toggle → tools/list_changed, loop toggle →
    # wake_all_for_flag_recheck. Shared with the update surfaces.
    await emit_context_write_wakes(context_key)

    return Ok(
        data={"context_key": context_key},
        message=f"Memory '{context_key}' created successfully",
    )


# --- backup_project_context tool ---
async def backup_project_context_tool_impl(
    arguments: Dict[str, Any],
    *,
    principal: Optional[Principal] = None,
) -> ToolResult:
    """Wave 6 PR 3 — Principal + ToolResult migration.

    Policy: was ``@requires_role("operator")`` — operator-only. Now
    expressed via :func:`_is_admin_principal` (admits operator
    sessions + sysadmins; rejects worker / manager-tier agents). The
    `visibility="operator"` kwarg on the register_tool() call below
    keeps the tools/list filter aligned so worker / manager bearers
    don't even see this tool in their catalogue.
    """
    if not _is_admin_principal(principal):
        return PermissionDenied(
            reason="Operator session required to back up project context"
        )

    backup_name = arguments.get("backup_name")  # Optional custom backup name
    include_health_report = arguments.get(
        "include_health_report", True
    )  # Include health analysis in backup

    requesting_agent_id = _actor_label(principal)

    log_audit(
        requesting_agent_id, "backup_project_context", {"backup_name": backup_name}
    )

    # arch-r4 #6: the unit-of-work owns the transaction — the full-table
    # read (via project_context_repo.list_all) and the
    # ``backup_project_context`` DB-audit row run on the SAME
    # ``u.cursor``, and a failure anywhere in the scope (including the
    # file-write below) rolls back before any audit row lands. Replaces
    # the ``session.connection().connection`` ORM-drill-through.
    try:
        with unit_of_work() as u:
            cursor = u.cursor

            # Create backup
            backup_data = _create_context_backup(cursor, backup_name)

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
            # VULN-003 defense-in-depth: resolve the candidate backup path
            # and verify it stays inside the backup directory before we
            # open() it for write. The tool-schema `pattern` on backup_name
            # is the primary gate (rejects anything outside
            # ``[A-Za-z0-9._-]{1,128}``, so `../`, absolute paths, NUL bytes
            # etc. never reach the impl); this check is a belt-and-suspenders
            # second layer for in-process callers that bypass schema
            # validation (direct impl invocation in tests, future internal
            # callers). Path traversal here matters under
            # stolen-operator-cookie + the VULN-001 CORS exploit vector,
            # where an attacker who has reached this tool could otherwise
            # write arbitrary JSON anywhere the server process can write.
            try:
                # ``.resolve()`` raises ValueError on an embedded-null-byte
                # path (R6-F3 class completion) — fold it into the same
                # Invalid path as the containment check below rather than let
                # it propagate to an unhandled 500. The schema `pattern` on
                # backup_name is the primary gate; this is the in-process /
                # test-caller belt-and-suspenders.
                backup_path_resolved = Path(backup_dir, backup_filename).resolve()
                backup_dir_resolved = Path(backup_dir).resolve()
                backup_path_resolved.relative_to(backup_dir_resolved)
            except (ValueError, OSError):
                return Invalid(
                    field="backup_name",
                    message=(
                        "backup_name resolves outside the backup directory"
                    ),
                )
            backup_path = str(backup_path_resolved)

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

            # log_agent_action_to_db expects a cursor; reuse the scope's
            # cursor so the action lives in the same transaction.
            log_agent_action_to_db(
                cursor,
                requesting_agent_id,
                "backup_project_context",
                backup_name,
                {"total_entries": backup_data["total_entries"], "backup_path": backup_path},
            )

            return Ok(
                data={
                    "backup_name": backup_data["backup_name"],
                    "backup_path": backup_path,
                    "total_entries": backup_data["total_entries"],
                    "created_at": backup_data["created_at"],
                    "health_report": backup_data.get("health_report"),
                },
                message="\n".join(response_parts),
            )

    except Exception as e:
        # The unit-of-work already rolled back + closed the connection.
        logger.error(f"Error creating context backup: {e}", exc_info=True)
        return Failed(message=f"Error creating backup: {e}")


# --- validate_context_consistency tool ---
async def validate_context_consistency_tool_impl(
    arguments: Dict[str, Any],
    *,
    principal: Optional[Principal] = None,
) -> ToolResult:
    """Wave 6 PR 3 — Principal + ToolResult migration.

    Gate: was ``@requires("any")``. Behaviour preserved via
    :func:`_requires_authenticated_caller`.
    """
    denied = _requires_authenticated_caller(principal)
    if denied is not None:
        return denied

    requesting_agent_id = _actor_label(principal)

    # Log audit
    log_audit(requesting_agent_id, "validate_context_consistency", {})

    # arch-r4 #6: read-only body — ``get_session()`` replaces the
    # hand-rolled ``SessionLocal()`` + ``finally: session.close()`` pair.
    with get_session() as session:
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
                return Ok(
                    data={"total_entries": 0, "issues": [], "warnings": []},
                    message="No project context entries found.",
                )

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

            return Ok(
                data={
                    "total_entries": len(all_entries),
                    "issues": issues,
                    "warnings": warnings,
                },
                message="\n".join(response_parts),
            )

        except SQLAlchemyError as e_sql:
            logger.error(
                f"Database error validating context consistency: {e_sql}", exc_info=True
            )
            return Failed(message=f"Database error validating context: {e_sql}")
        except Exception as e:
            logger.error(
                f"Unexpected error validating context consistency: {e}", exc_info=True
            )
            return Failed(message=f"Unexpected error validating context: {e}")


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
        name="create_project_context",
        description="Create a NEW project context entry with a specific key. Fails with a conflict if the key already exists (use update_project_context to overwrite). The value can be any JSON-serializable type.",
        input_schema={
            "type": "object",
            "properties": {
                "token": {
                    "type": "string",
                    "description": "Authentication token (agent or admin). Optional if Authorization: Bearer header is supplied (recommended).",
                },
                "context_key": {
                    "type": "string",
                    "description": "The exact key for the new context entry (e.g., 'api.service_x.url').",
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
            "required": ["context_key"],
            "additionalProperties": False,
        },
        implementation=create_project_context_tool_impl,
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
                    # VULN-003: pattern rejects path-traversal payloads
                    # (`../`, absolute paths, NUL bytes, shell
                    # metacharacters) at the dispatcher's jsonschema
                    # validation step — anything not matching
                    # ``[A-Za-z0-9._-]{1,128}`` never reaches the impl.
                    # Paired with the resolve()/relative_to() check in
                    # ``backup_project_context_tool_impl`` as defense in
                    # depth for in-process callers that bypass schema.
                    "pattern": "^[A-Za-z0-9._-]{1,128}$",
                    "description": (
                        "Optional custom backup name "
                        "(auto-generated if not provided). Slug — "
                        "alphanumeric plus . _ - only, max 128 chars."
                    ),
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
        visibility="operator",
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


async def delete_project_context_tool_impl(
    arguments: Dict[str, Any],
    *,
    principal: Optional[Principal] = None,
) -> ToolResult:
    """Delete project context entries permanently.

    Phase 7b: per-key creator-ownership check applies (admins can delete
    anything; workers can delete only entries they themselves created
    and which are not `config_*`). The `force_delete` safety net on
    "critical" system keys is preserved as belt-and-suspenders.

    Wave 6 PR 3 — Principal + ToolResult migration. Gate moves from
    ``@requires("any")`` to :func:`_requires_authenticated_caller`;
    ``is_admin`` for the per-key creator-ownership matrix resolved
    via :func:`_is_admin_principal`.
    """
    denied = _requires_authenticated_caller(principal)
    if denied is not None:
        return denied

    # SEC1: operator-path viewers are read-only (see
    # _deny_viewer_tier_write); block deletes before the per-key
    # ownership matrix would otherwise let a viewer remove its own keys.
    viewer_denied = _deny_viewer_tier_write(principal, "memories.delete")
    if viewer_denied is not None:
        return viewer_denied

    context_keys = arguments.get("context_keys", [])
    context_key = arguments.get("context_key")
    force_delete = arguments.get("force_delete", False)

    requesting_agent_id = _actor_label(principal)
    is_admin = _is_admin_principal(principal)

    # Prepare list of keys to delete
    keys_to_delete = []
    if context_key:
        keys_to_delete.append(context_key)
    if context_keys:
        keys_to_delete.extend(context_keys)

    if not keys_to_delete:
        return Invalid(
            field="context_key",
            message="No context keys specified for deletion",
        )

    # ADR-0016: config_* rows live in the project_settings store and can
    # no longer exist in project_context (migration 0016 moved them; the
    # write path rejects them for everyone). Reject a config_* delete up
    # front with the same pointer the write path gives — checked BEFORE
    # the critical-key guard so the caller gets the category error, not
    # a misleading force_delete hint. This also retires the AoE
    # sysadmin delete-gate here (it lives on delete_project_settings).
    for key in keys_to_delete:
        if key and _CONFIG_KEY_RE.match(key):
            return _config_key_error()

    # Critical system keys that require force_delete. The legacy
    # ``config_system_token`` / ``config_admin_token`` entries are gone:
    # migration 0015 deleted the rows and the config_* rejection above
    # makes any config-key delete unreachable past this point.
    critical_keys = [
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
        return Invalid(
            field="force_delete",
            message=(
                f"Cannot delete critical system keys without "
                f"force_delete=true: {critical_keys_found}"
            ),
        )

    # arch-r4 #6: the unit-of-work owns the transaction — the per-key
    # authorization check runs BEFORE any deletion (so an early return
    # here is a commit-of-nothing, not a dangling transaction the old
    # code relied on ``session.close()`` to implicitly roll back), and
    # the DELETE + its ``deleted_context`` DB-audit row + the RAG
    # chunk-purge all commit atomically on the SAME ``u.cursor``.
    # Replaces the ``session.connection().connection`` ORM-drill-through.
    try:
        with unit_of_work() as u:
            cursor = u.cursor

            # Per-key ownership check before any deletion runs.
            for key in keys_to_delete:
                err = _check_write_authorization(
                    cursor, requesting_agent_id, key, is_admin=is_admin
                )
                if err is not None:
                    # ``_check_write_authorization`` returns the typed
                    # :class:`PermissionDenied` directly — no string
                    # round trip.
                    return err

            deleted_rows = project_context_repo.delete_many(
                keys_to_delete, connection=cursor,
            )

            if not deleted_rows:
                return NotFound(
                    resource="project_context",
                    identifier=", ".join(keys_to_delete),
                )

            # Delete the keys
            deleted_count = 0
            deletion_details = []

            for row in deleted_rows:
                key = row["context_key"]
                description = row["description"] if row["description"] else ""
                deleted_count += 1
                deletion_details.append(
                    {
                        "key": key,
                        "description": description,
                        "was_critical": key in critical_keys_found,
                    }
                )

            # Log the deletion action via the shared cursor.
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

            # BL-R4-1: prune each deleted key's RAG chunk + hash watermark
            # in the SAME transaction as the row delete. The incremental
            # indexer keys on ``updated_at`` and never sweeps orphans, so a
            # deleted context row's ``source_type='context'`` chunk would
            # otherwise stay queryable via ``ask_project_rag`` forever.
            # Clearing the hash also lets a future re-add of the same key
            # re-index instead of being skipped as unchanged.
            from ..repositories import rag_repo

            for detail in deletion_details:
                rag_repo.purge_source(
                    "context", detail["key"], connection=cursor,
                )

        # SEC-C / F5: fire the SAME wake seam every other project_context
        # write surface uses (single update, queued bulk, inline bulk,
        # create) — this delete path was the last one that bypassed it
        # entirely, so deleting `config_auto_event_loop_global` reverted
        # the flag to its default without waking in-flight
        # `wait_for_events` callers, and deleting a `config_allow_worker_*`
        # key reverted worker tool visibility without pushing
        # `notifications/tools/list_changed`. Placed AFTER the `with
        # unit_of_work()` block exits cleanly (== committed), matching the
        # emit-after-commit placement of every sibling write surface.
        await emit_context_write_wakes_bulk(
            detail["key"] for detail in deletion_details
        )

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

        return Ok(
            data={
                "deleted_count": deleted_count,
                "deleted_keys": [d["key"] for d in deletion_details],
                "critical_keys_deleted": critical_keys_found,
                "force_delete": force_delete,
            },
            message="\n".join(response_parts),
        )

    except Exception as e:
        # The unit-of-work already rolled back + closed the connection.
        logger.error(f"Error in delete_project_context_tool_impl: {e}", exc_info=True)
        return Failed(message=f"Error deleting project context: {str(e)}")


# Call registration when this module is imported
register_project_context_tools()
