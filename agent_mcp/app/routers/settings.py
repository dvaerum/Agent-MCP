"""Settings router — the project_settings store + config-shaped reads.

Wave 8 PR 1 of prancy-napping-pie: the configuration-shaped reads
mechanically moved out of ``app/routes.py`` onto this router:
``tokens``, ``prompts_catalog``.

Wave 11 PR 0 (ADR-0016): the router also owns the CRUD surface for
the dedicated ``project_settings`` store — ``GET /api/settings-data``
plus ``POST/PUT/DELETE /api/settings...`` — thin adapters that
dispatch the gated MCP settings tools
(``tools/project_settings_tools.py``) exactly the way
``routers/memories.py`` dispatches the context tools: ONE enforcement
path (the ``system.config.write`` cap gate lives in the tool, never
re-implemented here).

The router uses the bare ``/api`` prefix (rather than a
per-resource sub-prefix) and each handler registers its own
full path (e.g. ``/settings-data`` → mounted at ``/api/settings-data``).

Auth: handler-level ``Depends(require_operator_session)`` is kept
verbatim on the handlers that currently have it. The router-level
gate is deferred to a follow-up PR — ``GET /api/prompts/catalog``
is currently open today and hoisting the gate to the router would
silently flip its auth behavior, which is out of scope for a
mechanical URL-stable move.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from starlette.requests import Request

from .._dispatch_helpers import _build_route_principal, handle_options
from ._wire_validation import require_str as _require_str
from ..deps import require_operator_session
from ..rest_principal import RestPrincipal
from .composition import is_confirmed_operator_tier
from ...core.authorize import AuthRejected
from ...core.config import logger
from ...core import globals as g
from ...core.tool_result import (
    Ok,
    tool_result_error_message,
    tool_result_to_http,
)
from ...tools.registry import ToolInputValidationError, dispatch_tool_call
from ...utils.json_utils import get_sanitized_json_body
from ...utils.string_utils import (
    SETTING_KEY_ERROR,
    UNSAFE_KEY_ERROR,
    has_unsafe_unicode_for_identifier,
    is_valid_memory_key,
)


router = APIRouter(
    prefix="/api",
    tags=["settings"],
)


@router.api_route("/tokens", methods=["GET", "OPTIONS"])
async def tokens_api_route(
    request: Request,
    auth: RestPrincipal = Depends(require_operator_session),
) -> JSONResponse:
    """GET /api/tokens — dashboard's source of agent bearer tokens.

    Wave 1 (prancy-napping-pie): cookie-migrated. The dep accepts:

      * an ``agent_mcp_session`` cookie pointing at a live operator
        session (the new dashboard path), OR
      * an ``Authorization: Bearer <manager-agent-token>`` header
        (operator-tier per-agent bearer; legacy admin-scripts and the
        agent CLI), OR
      * the same token in a body / query-string field (oldest
        backwards-compat path; nothing in the dashboard sends this).

    The manual ``Authorization: Bearer`` worker-rejection ladder that
    used to live here is now superseded by the dep — any non-operator-
    tier bearer fails ``_bearer_is_operator_tier`` and the request
    401s before reaching this handler.

    Wave 3 (prancy-napping-pie) dropped the legacy ``admin_token``
    field from the response. The dashboard no longer reads it (Wave 2
    stripped the frontend reads). Out-of-tree clients that POST'd to
    this endpoint expecting an ``admin_token`` field must migrate to
    per-agent bearer tokens; see ``docs/external-mcp-client.md`` for
    the provisioning walkthrough. retire-system-token Wave 3 deleted
    the underlying god-key; there is no equivalent value to surface.
    """
    if request.method == 'OPTIONS':
        return await handle_options(request)
    # SECURITY: this endpoint returns the full agent bearer-token list.
    # ``require_operator_session`` admits viewer-tier operators via the
    # cookie/forwarding path on GET (the router only gates mutations on
    # tier, and the backend cannot resolve the caller's project role),
    # so a read-only viewer could otherwise harvest every agent's bearer
    # and replay it to escalate to write. Only a CONFIRMED operator-tier
    # bearer may read the list; everything else gets 403. See
    # ``is_confirmed_operator_tier``.
    if not is_confirmed_operator_tier(auth):
        return JSONResponse(
            {
                "error": "forbidden",
                "message": (
                    "Agent bearer tokens are operator-tier only. Use an "
                    "operator-tier bearer (agent CLI / admin script) to "
                    "read this endpoint."
                ),
            },
            status_code=403,
        )
    try:
        # arch-deepening F: use the canonical live-agent predicate. The
        # old ``!= 'terminated'`` filter was the WEAKER variant and let a
        # 'tombstone' entry (`[deleted-<id>]`, reserved ``__tombstone_*``
        # token) leak its bearer here if one ever reached the in-memory
        # allow-list. ``is_live_status`` excludes tombstone too.
        from ...repositories.agent_repository import is_live_status

        agent_tokens_list = []
        for token, data in g.active_agents.items():
            if is_live_status(data.get("status")):
                agent_tokens_list.append({"agent_id": data.get("agent_id"), "token": token})
        return JSONResponse({"agent_tokens": agent_tokens_list})
    except Exception as e:
        logger.error(f"Error retrieving tokens for dashboard: {e}", exc_info=True)
        # BL-R5-2 / SD-R6-1: generic message — ``str(e)`` on a
        # SQLAlchemyError / sqlite3.*Error embeds SQL text + bound
        # params (schema disclosure). Detail stays in the log above.
        return JSONResponse({"error": "Error retrieving tokens"}, status_code=500)


# --- Prompt Book catalog (plan Phase 6) ---
@router.api_route("/prompts/catalog", methods=["GET", "OPTIONS"])
async def prompts_catalog_api_route(request: Request) -> JSONResponse:
    """`GET /api/prompts/catalog` — the single source of truth
    for the Prompt Book catalogue.

    Sourced from `agent_mcp/prompts/catalog.json` so MCP
    `prompts/list` and the dashboard read the same data.
    """
    if request.method == 'OPTIONS':
        return await handle_options(request)
    from ...prompts import load_catalog
    return JSONResponse(load_catalog())


# --- project_settings store (ADR-0016, Wave 11) ---
#
# CRITICAL (F009): the read endpoint must NOT gate the whole response on
# ``is_confirmed_operator_tier`` — cookie/forwarding operator sessions
# are conservatively NON-confirmed (the per-project backend can't verify
# their project role), and a blanket 403/redaction is exactly the bug
# that broke the Settings toggles. Real values go out to every admitted
# operator; ONLY the two genuinely secret keys
# (``_SECRET_SETTING_KEYS`` — the store's own literal classification)
# redact for non-confirmed tiers.


@router.api_route("/settings-data", methods=["GET", "OPTIONS"])
async def settings_data_api_route(
    request: Request,
    auth: RestPrincipal = Depends(require_operator_session),
) -> JSONResponse:
    """GET /api/settings-data — every ``project_settings`` row.

    The Settings dashboard's read seam (replaces its former
    ``getAllData().context`` filtering — config rows no longer live in
    ``project_context``). Row shape matches the memories rows
    (``context_key`` / ``value`` / ``description`` / ownership stamps);
    ``value`` stays the raw JSON-encoded string the store carries.
    """
    if request.method == 'OPTIONS':
        return await handle_options(request)

    from ...db.unit_of_work import unit_of_work
    from ...repositories import project_settings_repository as settings_repo
    from ...tools.project_settings_tools import redact_settings_row

    try:
        with unit_of_work() as u:
            rows = settings_repo.list_all(connection=u.cursor)
    except Exception as e:
        logger.error(f"Error reading project settings: {e}", exc_info=True)
        return JSONResponse(
            {"error": "Failed to read project settings"}, status_code=500,
        )

    confirmed = is_confirmed_operator_tier(auth)
    return JSONResponse(
        {
            "settings": [
                redact_settings_row(r, confirmed_operator_tier=confirmed)
                for r in rows
            ]
        }
    )


def _schema_as_json() -> list[dict]:
    """Serialise the single-source settings schema for the wire.

    One row per :class:`agent_mcp.core.settings_schema.SettingSpec`,
    in registry order. Secret specs carry ``default: null`` — a secret
    has no plaintext default to disclose.
    """
    from ...core.settings_schema import SETTINGS_SCHEMA

    return [
        {
            "key": s.key,
            "type": s.type,
            "default": s.default,
            "tier": s.tier,
            "group": s.group,
            "title": s.title,
            "description": s.description,
            "widget": s.widget,
        }
        for s in SETTINGS_SCHEMA
    ]


@router.api_route("/settings-schema", methods=["GET", "OPTIONS"])
async def settings_schema_api_route(
    request: Request,
    auth: RestPrincipal = Depends(require_operator_session),
) -> JSONResponse:
    """GET /api/settings-schema — the single-source settings schema.

    ADR-0018: the backend registry (``core/settings_schema``) is the one
    owner of every ``config_*`` setting's default / group / tier / human
    copy. The Settings dashboard fetches this to render its controls
    data-driven (type→widget) instead of hardcoding the schema, so the
    FE and BE can no longer drift.

    Auth mirrors the ``/api/tokens`` pattern: ``require_operator_session``
    admits the caller, then a CONFIRMED operator-tier check gates the
    body — a non-confirmed (viewer / bare-forwarding) caller gets 403.
    The schema itself is not sensitive, but gating it to confirmed
    operators keeps it consistent with the settings-store read surface.

    The ``caller`` block lets the frontend disable sysadmin-tier widgets
    for a non-sysadmin operator (fixing the silent-403 mis-tier) without
    a second round-trip.
    """
    if request.method == 'OPTIONS':
        return await handle_options(request)

    if not is_confirmed_operator_tier(auth):
        return JSONResponse(
            {
                "error": "forbidden",
                "message": (
                    "The settings schema is operator-tier only. Use an "
                    "operator-tier session or bearer to read it."
                ),
            },
            status_code=403,
        )

    return JSONResponse(
        {
            "schema": _schema_as_json(),
            "caller": {
                "sysadmin": auth.sysadmin,
                "confirmed_operator": is_confirmed_operator_tier(auth),
            },
        }
    )


async def _dispatch_settings_write(
    request: Request,
    auth: RestPrincipal,
    context_key: str,
    data: dict,
) -> JSONResponse:
    """Shared upsert adapter for POST /api/settings and
    PUT /api/settings/{context_key} — both dispatch the gated
    ``update_project_settings`` tool (upsert semantics), mirroring how
    ``routers/memories.py`` dispatches the context tools."""
    # Wire hygiene, matching the memories handlers: a structured JSON
    # ``context_key``/``description`` must 400, not 500; unsafe-unicode
    # keys are spoofing vectors (F005).
    _err = _require_str(context_key, "context_key")
    if _err is not None:
        return _err
    description = data.get("description")
    _err = _require_str(description, "description")
    if _err is not None:
        return _err
    if has_unsafe_unicode_for_identifier(context_key):
        return JSONResponse(UNSAFE_KEY_ERROR, status_code=400)

    # R20-F2: positive ASCII allowlist, matching memories.py's pattern.
    # The denylist above misses Unicode categories Lo/So (invisible
    # "filler"/blank glyphs like U+115F, U+2800) that widening the
    # denylist would also strip legitimate non-Latin letters (Lo covers
    # CJK/Hangul/Arabic base letters). config_* keys are internal
    # identifiers, not user-facing text, so an ASCII allowlist is the
    # strictly correct fit -- see string_utils.SETTING_KEY_ERROR.
    if not is_valid_memory_key(context_key):
        return JSONResponse(SETTING_KEY_ERROR, status_code=400)

    principal = _build_route_principal(auth=auth)

    arguments: dict = {
        "context_key": context_key,
        "context_value": data.get("context_value"),
    }
    if description is not None:
        arguments["description"] = description

    try:
        result = await dispatch_tool_call(
            "update_project_settings", arguments, principal=principal,
        )
    except AuthRejected as e:
        # AC-R5-1 / R21-F1 class: a tool's @requires_* gate RAISES, so
        # without this arm a routine denial (e.g. a forwarding VIEWER that
        # passes require_operator_session but lacks the tool's cap) lands
        # in the generic 500 below. Rationale + sweep:
        # tests/test_arch_enforced_authrejected_403.py.
        return JSONResponse({"error": e.reason}, status_code=403)
    except ToolInputValidationError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:  # pragma: no cover - defensive
        logger.error(
            f"Error dispatching update_project_settings: {e}", exc_info=True,
        )
        return JSONResponse(
            {"error": "Failed to update setting"}, status_code=500,
        )

    if isinstance(result, Ok):
        return JSONResponse(
            {
                "success": True,
                "message": (
                    result.message
                    or f"Setting '{context_key}' updated successfully"
                ),
            }
        )

    status, _ = tool_result_to_http(result)
    return JSONResponse(
        {"error": tool_result_error_message(result, "Failed to update setting")},
        status_code=status,
    )


@router.post("/settings")
async def create_setting_api_route(
    request: Request,
    auth: RestPrincipal = Depends(require_operator_session),
) -> JSONResponse:
    """POST /api/settings — upsert a setting (body carries the key)."""
    try:
        data = await get_sanitized_json_body(request)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    context_key = data.get("context_key")
    if not context_key:
        return JSONResponse(
            {"error": "context_key is required"}, status_code=400,
        )
    return await _dispatch_settings_write(request, auth, context_key, data)


@router.put("/settings/{context_key}")
async def update_setting_api_route(
    context_key: str,
    request: Request,
    auth: RestPrincipal = Depends(require_operator_session),
) -> JSONResponse:
    """PUT /api/settings/<context_key> — upsert a setting."""
    try:
        data = await get_sanitized_json_body(request)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return await _dispatch_settings_write(request, auth, context_key, data)


@router.delete("/settings/{context_key}")
async def delete_setting_api_route(
    context_key: str,
    request: Request,
    auth: RestPrincipal = Depends(require_operator_session),
) -> JSONResponse:
    """DELETE /api/settings/<context_key> — thin adapter over the gated
    ``delete_project_settings`` tool (the ``system.config.write`` cap
    gate lives in the tool; the post-delete wake set fires there too)."""
    if has_unsafe_unicode_for_identifier(context_key):
        return JSONResponse(UNSAFE_KEY_ERROR, status_code=400)

    # R20-F2: same ASCII allowlist gate as the write path above.
    if not is_valid_memory_key(context_key):
        return JSONResponse(SETTING_KEY_ERROR, status_code=400)

    principal = _build_route_principal(auth=auth)

    try:
        result = await dispatch_tool_call(
            "delete_project_settings",
            {"context_key": context_key},
            principal=principal,
        )
    except AuthRejected as e:
        # AC-R5-1 / R21-F1 class: a tool's @requires_* gate RAISES, so
        # without this arm a routine denial (e.g. a forwarding VIEWER that
        # passes require_operator_session but lacks the tool's cap) lands
        # in the generic 500 below. Rationale + sweep:
        # tests/test_arch_enforced_authrejected_403.py.
        return JSONResponse({"error": e.reason}, status_code=403)
    except ToolInputValidationError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:  # pragma: no cover - defensive
        logger.error(
            f"Error dispatching delete_project_settings: {e}", exc_info=True,
        )
        return JSONResponse(
            {"error": "Failed to delete setting"}, status_code=500,
        )

    if isinstance(result, Ok):
        return JSONResponse(
            {
                "success": True,
                "message": f"Setting '{context_key}' deleted successfully",
            }
        )

    status, _ = tool_result_to_http(result)
    return JSONResponse(
        {
            "error": tool_result_error_message(
                result, "Failed to delete setting", not_found_label="Setting",
            )
        },
        status_code=status,
    )


# --- Catch-all OPTIONS handler ---
# Mirrors the legacy `('/api/{path:path}', handle_options, ['OPTIONS'], None)`
# entry at the bottom of `_dashboard_route_specs`. Registered last
# (settings is included after every other `/api`-prefix router) so
# the per-route OPTIONS registrations on agents/tasks/memories/messages
# still win the match. See `routers/__init__.py` for the include order.
@router.options("/{path:path}")
async def options_catch_all(request: Request, path: str):
    return await handle_options(request)
