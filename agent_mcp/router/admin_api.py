"""Router admin REST surface — ``/agent-mcp/api/router/...``.

The single reserved top-level segment ``router`` carves out an admin
namespace that can't collide with a project-named ``projects`` /
``overview`` / ``health``. Every operator-facing endpoint that used
to live at ``/agent-mcp/__*`` lives here now as a REST resource.

Auth: every handler in this module is gated by
``require_operator_session_middleware`` (PR D) because the paths fall
under ``/agent-mcp/api/...``. The one exception is
``GET /api/router/health``, which is allow-listed by
``_UNAUTH_PREFIXES`` so callers can probe liveness without a session.

URL map (legacy → new) is documented at:
``docs/adr/0014-rest-admin-api.md``.

Implementation note: rather than duplicate the create / delete /
rename / stop logic, the JSON-bodied handlers from PR-C (which
previously lived at ``/api/projects/...``) are reused here under the
new path. The thin wrapper functions in this module pull the
project name from the URL, normalise body shape where the new REST
contract differs from the PR-C shape (``PATCH`` body uses ``name``
where the legacy ``rename`` handler used ``new_name``), and delegate.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from pathlib import Path

from aiohttp import web


logger = logging.getLogger(__name__)


# ── Health (public) ─────────────────────────────────────────────────


async def health_handler(req: web.Request) -> web.Response:
    """``GET /agent-mcp/api/router/health`` — public service descriptor.

    The one admin endpoint that bypasses the operator-session gate
    (allow-listed in ``_UNAUTH_PREFIXES``). Lets external monitors
    probe liveness without minting an operator session.
    """
    from . import app as _app

    return web.json_response(
        {
            "ok": True,
            "service": "agent-mcp-router",
            "version": _app._PACKAGE_VERSION,
            "mode": (
                "single-tenant"
                if _app.SINGLE_TENANT_NAME is not None
                else "multi-tenant"
            ),
        },
        headers={"Cache-Control": "no-store"},
    )


# ── Projects collection ─────────────────────────────────────────────


async def list_projects_handler(req: web.Request) -> web.Response:
    """``GET /agent-mcp/api/router/projects`` — list of project names.

    Replaces the legacy ``GET /__projects``. JSON shape unchanged so
    project pickers that previously consumed ``{projects: [...]}``
    keep working with a one-line URL swap.
    """
    from . import app as _app

    return web.json_response(
        {"projects": sorted(_app._projects_dict().keys())},
        headers={"Cache-Control": "no-store"},
    )


async def create_project_handler(req: web.Request) -> web.Response:
    """``POST /agent-mcp/api/router/projects`` — register a new project.

    JSON body ``{"name": "<slug>"}``. Workspace location is fixed at
    DEFAULT_WORKSPACE_PARENT/<name>. Returns 201 with the unified
    envelope ``{success: true, project: {name, workspace}}`` on
    success, or a discriminated error envelope on failure (see
    ``_ERROR_*`` in ``app.py``).
    """
    from . import app as _app

    if _app.SINGLE_TENANT_NAME is not None:
        return _app._single_tenant_disabled_response()
    body = await _app._parse_json_body(req)
    name = (body.get("name") or "").strip()
    err = _app._validate_name(name, _app._projects_dict())
    if err is not None:
        if "already" in err:
            return _app._error_envelope(
                error=_app._ERROR_ALREADY_REGISTERED, message=err, status=409,
            )
        return _app._error_envelope(
            error=_app._ERROR_INVALID_NAME, message=err, status=400,
        )
    workspace = (_app.DEFAULT_WORKSPACE_PARENT / name).expanduser().resolve()
    try:
        workspace.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return _app._error_envelope(
            error=_app._ERROR_INTERNAL,
            message=f"could not create workspace {workspace}: {e.strerror}",
            status=500,
        )
    try:
        _app._REGISTRY.register(name, str(workspace))
    except ValueError as e:
        return _app._error_envelope(
            error=_app._ERROR_ALREADY_REGISTERED, message=str(e), status=409,
        )
    creator = req.get("user")
    if creator and creator.get("user_id"):
        from .identity import add_project_membership

        try:
            add_project_membership(creator["user_id"], name)
        except Exception:
            logger.exception(
                "Failed to add project_membership for creator=%s project=%s",
                creator.get("username"),
                name,
            )
    return _app._success_envelope(
        {"project": {"name": name, "workspace": str(workspace)}},
        status=201,
    )


# ── Per-project resource ────────────────────────────────────────────


async def rename_project_handler(req: web.Request) -> web.Response:
    """``PATCH /agent-mcp/api/router/projects/<name>`` — rename.

    JSON body ``{"name": "<new-slug>", "grace_days": int?}``. The
    new-name field is just ``name`` here (not ``new_name`` as in the
    legacy shape) because PATCH bodies describe the target state of
    the resource, not a verb argument. Grace-period alias semantics
    are unchanged.
    """
    from . import app as _app

    if _app.SINGLE_TENANT_NAME is not None:
        return _app._single_tenant_disabled_response()
    old_name = req.match_info["name"]
    body = await _app._parse_json_body(req)
    new_name = (body.get("name") or "").strip()
    grace_days_raw = body.get("grace_days", 30)
    try:
        grace_days = int(grace_days_raw)
    except (TypeError, ValueError):
        return _app._error_envelope(
            error=_app._ERROR_INVALID_NAME,
            message=f"grace_days must be an integer, got {grace_days_raw!r}",
            status=400,
        )
    if not _app._SLUG_RE.match(old_name):
        return _app._error_envelope(
            error=_app._ERROR_INVALID_NAME,
            message=f"old name {old_name!r} is not a valid slug",
            status=400,
        )
    err = _app._validate_name(new_name, _app._projects_dict())
    if err is not None:
        if "already" in err:
            return _app._error_envelope(
                error=_app._ERROR_NAME_TAKEN, message=err, status=409,
            )
        return _app._error_envelope(
            error=_app._ERROR_INVALID_NAME, message=err, status=400,
        )
    if old_name == new_name:
        return _app._error_envelope(
            error=_app._ERROR_INVALID_NAME,
            message="old and new names are identical",
            status=400,
        )
    old_row = _app._REGISTRY.get(old_name)
    if old_row is None:
        return _app._error_envelope(
            error=_app._ERROR_NOT_REGISTERED,
            message=f"unknown project: {old_name!r}",
            status=404,
        )
    if _app._REGISTRY.resolve_alias(new_name) is not None:
        return _app._error_envelope(
            error=_app._ERROR_ALIAS_COLLISION,
            message=f"name {new_name!r} is an active alias",
            status=409,
        )
    conns = _app.active_conns.get(old_name, 0)
    if conns > 0:
        return _app._error_envelope(
            error=_app._ERROR_ACTIVE_SESSIONS,
            message=(
                f"{old_name!r} has {conns} active connection(s); "
                f"disconnect them and retry"
            ),
            status=409,
            extra={"active_connections": conns, "agents": []},
        )
    _app._systemctl("stop", _app._unit_name(old_name, "backend"))
    workspace = Path(old_row.get("workspace", ""))
    new_workspace: Path | None = None
    if workspace.name == old_name and workspace.exists():
        new_workspace = workspace.with_name(new_name)
        try:
            os.rename(workspace, new_workspace)
        except OSError as e:
            return _app._error_envelope(
                error=_app._ERROR_INTERNAL,
                message=f"could not rename workspace dir: {e.strerror}",
                status=500,
            )
    token_dir = (
        Path(os.environ.get("AGENT_MCP_TOKENS_DIR", ""))
        or (Path.home() / ".config" / "agent-mcp" / "tokens")
    )
    if token_dir.is_dir():
        for tok in token_dir.glob(f"{old_name}--*.token"):
            suffix = tok.name[len(old_name) + len("--"):]
            try:
                tok.rename(token_dir / f"{new_name}--{suffix}")
            except OSError:
                pass
    try:
        _app._REGISTRY.rename(old_name, new_name, grace_days=grace_days)
    except (ValueError, KeyError) as e:
        if new_workspace is not None and new_workspace.exists():
            try:
                os.rename(new_workspace, workspace)
            except OSError:
                pass
        return _app._error_envelope(
            error=_app._ERROR_INTERNAL,
            message=f"registry rename failed: {e}",
            status=500,
        )
    new_row = _app._REGISTRY.get(new_name)
    alias_expires_at = ""
    for entry in (new_row or {}).get("aliases", []) or []:
        if entry.get("name") == old_name:
            alias_expires_at = entry.get("expires_at", "")
            break
    return _app._success_envelope({
        "renamed": {"from": old_name, "to": new_name},
        "alias": {
            "name": old_name,
            "grace_days": grace_days,
            "expires_at": alias_expires_at,
        },
    })


async def delete_project_handler(req: web.Request) -> web.Response:
    """``DELETE /agent-mcp/api/router/projects/<name>`` — unregister.

    Query param ``?delete_workspace=true`` opts in to recursive
    workspace removal (guarded by ``_is_within_default_workspace``).
    Active-session refusal mirrors the legacy handler with a 409.
    """
    from . import app as _app

    if _app.SINGLE_TENANT_NAME is not None:
        return _app._single_tenant_disabled_response()
    name = req.match_info["name"]
    projects = _app._projects_dict()
    if name not in projects:
        return _app._error_envelope(
            error=_app._ERROR_NOT_REGISTERED,
            message=f"unknown project: {name!r}",
            status=404,
        )
    conns = _app.active_conns.get(name, 0)
    if conns > 0:
        return _app._error_envelope(
            error=_app._ERROR_ACTIVE_SESSIONS,
            message=(
                f"{name!r} has {conns} active connection(s); disconnect "
                f"them and retry"
            ),
            status=409,
            extra={"active_connections": conns, "agents": []},
        )
    workspace_path = Path(projects[name])
    want_delete = (
        req.rel_url.query.get("delete_workspace", "").lower()
        in {"true", "1", "yes", "on"}
    )
    workspace_deleted = False
    workspace_delete_skipped_reason: str | None = None
    if want_delete:
        if not _app._is_within_default_workspace(workspace_path):
            workspace_delete_skipped_reason = (
                f"workspace {workspace_path} resolves outside the default "
                f"workspace parent; refusing recursive delete"
            )
        elif workspace_path.exists():
            import shutil
            try:
                shutil.rmtree(workspace_path)
                workspace_deleted = True
            except OSError as e:
                workspace_delete_skipped_reason = (
                    f"rmtree({workspace_path}) failed: {e.strerror}"
                )
        else:
            workspace_delete_skipped_reason = (
                f"workspace {workspace_path} did not exist on disk"
            )
            workspace_deleted = True
    _app._systemctl("stop", _app._unit_name(name, "backend"))
    try:
        _app._REGISTRY.unregister(name)
    except KeyError:
        pass
    payload: dict = {
        "unregistered": name,
        "workspace_deleted": workspace_deleted,
    }
    if workspace_delete_skipped_reason is not None:
        payload["workspace_delete_skipped_reason"] = workspace_delete_skipped_reason
    return _app._success_envelope(payload)


async def stop_project_handler(req: web.Request) -> web.Response:
    """``POST /agent-mcp/api/router/projects/<name>/stop`` — stop backend."""
    from . import app as _app

    name = req.match_info["name"]
    if name not in _app._projects_dict():
        return _app._error_envelope(
            error=_app._ERROR_NOT_REGISTERED,
            message=f"unknown project: {name!r}",
            status=404,
        )
    conns = _app.active_conns.get(name, 0)
    if conns > 0:
        return _app._error_envelope(
            error=_app._ERROR_ACTIVE_SESSIONS,
            message=(
                f"{name!r} has {conns} active connection(s); disconnect "
                f"them and retry"
            ),
            status=409,
            extra={"active_connections": conns, "agents": []},
        )
    unit = _app._unit_name(name, "backend")
    if _app._is_active(unit):
        r = _app._systemctl("stop", unit)
        if r.returncode != 0:
            return _app._error_envelope(
                error=_app._ERROR_INTERNAL,
                message=f"systemctl stop {unit} failed: {r.stderr.strip()}",
                status=500,
            )
    return _app._success_envelope({"stopped": name})


# ── Overview ────────────────────────────────────────────────────────


async def overview_handler(req: web.Request) -> web.Response:
    """``GET /agent-mcp/api/router/overview`` — cross-project envelope.

    Cached for ``_OVERVIEW_CACHE_TTL_SEC`` to coalesce dashboard
    first-paint fan-out. The cache is process-local.
    """
    from . import app as _app
    import time

    now = time.time()
    if _app._overview_cache is not None and _app._overview_cache[0] > now:
        envelope = _app._overview_cache[1]
    else:
        envelope = _app._build_overview_envelope()
        _app._overview_cache = (now + _app._OVERVIEW_CACHE_TTL_SEC, envelope)
    return web.json_response(envelope, headers={"Cache-Control": "no-store"})


# ── Wiring helpers (client-config / installer) ──────────────────────


async def client_config_handler(req: web.Request) -> web.Response:
    """``GET /agent-mcp/api/router/projects/<name>/client-config``.

    Returns the project's ``.mcp.json`` body with a vendor media
    type (``application/vnd.agent-mcp.client-config+json``) so a
    client can content-negotiate without sniffing the URL extension.
    The on-the-wire body shape is unchanged from the legacy
    ``/__client-config/<n>.mcp.json`` endpoint.
    """
    from . import app as _app

    name = req.match_info["name"]
    if name not in _app._projects_dict():
        raise web.HTTPNotFound(reason=f"unknown project: {name!r}")
    agent_id = req.rel_url.query.get("agent")
    token, _aid = await _app._resolve_agent_token(name, agent_id)
    body = json.dumps(_app._mcp_json_for(name, token=token), indent=2) + "\n"
    return web.Response(
        body=body,
        content_type="application/vnd.agent-mcp.client-config+json",
        headers={
            "Content-Disposition": 'attachment; filename=".mcp.json"',
            "Cache-Control": "no-store",
        },
    )


async def installer_handler(req: web.Request) -> web.Response:
    """``GET /agent-mcp/api/router/projects/<name>/installer``.

    Renders the installer shell template with the project's wiring
    URL + (optional) agent token substituted. Served as
    ``text/x-shellscript`` so a ``curl | bash`` chain works without
    a mismatched Content-Type tripping shells / proxies.
    """
    from . import app as _app

    name = req.match_info["name"]
    if name not in _app._projects_dict():
        raise web.HTTPNotFound(reason=f"unknown project: {name!r}")
    agent_id = req.rel_url.query.get("agent")
    token, _aid = await _app._resolve_agent_token(name, agent_id)
    return web.Response(
        text=_app._installer_script_for(name, token=token),
        content_type="text/x-shellscript",
        charset="utf-8",
        headers={"Cache-Control": "no-store"},
    )


# ── Alias usage / removal ───────────────────────────────────────────


async def alias_usage_handler(req: web.Request) -> web.Response:
    """``GET /agent-mcp/api/router/projects/<name>/aliases?alias=<n>``.

    Lists the agent_ids that have used ``<alias>`` against the
    project at ``<name>`` so an operator can see who's still on the
    old name before expiring the alias. The ``alias`` query param is
    required; the path is keyed by the resolved (real) project name.
    """
    from . import app as _app

    project_name = req.match_info["name"]
    alias = (req.rel_url.query.get("alias") or "").strip()
    if not alias:
        raise web.HTTPBadRequest(reason="missing 'alias' query parameter")
    real_name = _app._REGISTRY.resolve_alias(alias)
    if real_name is None:
        raise web.HTTPNotFound(
            reason=f"alias {alias!r} is not active on any project"
        )
    if real_name != project_name:
        # The path-keyed project doesn't own this alias. Treat as
        # 404 — same UX as "alias not found here".
        raise web.HTTPNotFound(
            reason=f"alias {alias!r} is not active on project {project_name!r}"
        )
    row = _app._REGISTRY.get(real_name)
    if row is None:
        raise web.HTTPNotFound(reason=f"alias {alias!r} no longer resolves")
    expires_at = ""
    for entry in row.get("aliases", []) or []:
        if entry.get("name") == alias:
            expires_at = entry.get("expires_at", "")
            break
    agents: list[str] = []
    db = _app._project_db_path(row["workspace"])
    if db.is_file():
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1.0)
            try:
                cur = con.cursor()
                rows = cur.execute(
                    "SELECT DISTINCT agent_id FROM mcp_sessions "
                    "WHERE alias_used = ? AND agent_id IS NOT NULL",
                    (alias,),
                ).fetchall()
                agents = [r[0] for r in rows if r[0]]
            finally:
                con.close()
        except Exception:
            logger.exception("alias-usage: query failed for %s", db)
    return web.json_response(
        {
            "alias": alias,
            "project": real_name,
            "expires_at": expires_at,
            "agents": agents,
        },
        headers={"Cache-Control": "no-store"},
    )


async def remove_alias_handler(req: web.Request) -> web.Response:
    """``DELETE /agent-mcp/api/router/projects/<name>/aliases/<alias>``.

    Expires the alias immediately, skipping the grace reaper. Body
    matches the legacy ``__remove-alias`` shape so downstream
    consumers that key on ``{removed, project, remaining_aliases}``
    keep working.
    """
    from . import app as _app

    if _app.SINGLE_TENANT_NAME is not None:
        return _app._single_tenant_disabled_response()
    name = req.match_info["name"]
    alias = req.match_info["alias"]
    if not name or not alias:
        raise web.HTTPBadRequest(reason="missing 'name' or 'alias'")
    row = _app._REGISTRY.get(name)
    if row is None:
        raise web.HTTPNotFound(reason=f"unknown project: {name!r}")
    _app._REGISTRY.expire_alias(name, alias)
    updated = _app._REGISTRY.get(name)
    remaining = list((updated or {}).get("aliases", []) or [])
    return web.json_response(
        {
            "removed": alias,
            "project": name,
            "remaining_aliases": remaining,
        },
        headers={"Cache-Control": "no-store"},
    )


# ── Admin create-agent wrapper ──────────────────────────────────────


async def create_agent_handler(req: web.Request) -> web.Response:
    """``POST /agent-mcp/api/router/projects/<name>/agents``.

    Router-level admin wrapper that proxies via ``_mcp_call_admin``
    to seed a bootstrap task and create the agent on the per-project
    backend. Distinct from the per-project ``POST /api/<project>/agents``
    in ``agent_mcp/app/routes.py`` — that one is the direct create
    from within a project's MCP session; this one is the router-side
    "wire a new agent into this project" admin operation.

    JSON body: ``{"agent_id": "<slug>"}``. Returns the seed task_id
    and the new agent_id on success.
    """
    from . import app as _app
    import asyncio
    import re

    name = req.match_info["name"]
    body = await _app._parse_json_body(req)
    agent_id = (body.get("agent_id") or "").strip()
    if not agent_id:
        raise web.HTTPBadRequest(reason="missing 'agent_id'")
    if name not in _app._projects_dict():
        raise web.HTTPNotFound(reason=f"unknown project: {name!r}")
    if not _app._SLUG_RE.match(agent_id):
        raise web.HTTPBadRequest(
            reason=f"agent_id must match {_app._SLUG_RE.pattern}"
        )
    try:
        seed = await _app._mcp_call_admin(
            name,
            "assign_task",
            {
                "task_title": f"agent {agent_id}: bootstrap",
                "task_description": (
                    "Auto-created so the agent has at least one task "
                    "to attach to. Close or repurpose freely."
                ),
                "auto_suggest_parent": False,
                "validate_agent_workload": False,
                "override_rag": True,
                "override_reason": "router create-agent helper",
            },
        )
    except (asyncio.TimeoutError, Exception) as e:
        raise web.HTTPBadGateway(reason=f"seed task failed: {e}")
    seed_text = "\n".join(
        p.get("text", "") for p in seed.get("content", []) or []
    )
    m = re.search(r"task_\d+", seed_text)
    if not m:
        raise web.HTTPBadGateway(
            reason="seed task created but task_id not parseable"
        )
    seed_task_id = m.group(0)
    try:
        result = await _app._mcp_call_admin(
            name,
            "create_agent",
            {
                "agent_id": agent_id,
                "task_ids": [seed_task_id],
                "send_prompt": False,
            },
        )
    except (asyncio.TimeoutError, Exception) as e:
        raise web.HTTPBadGateway(reason=f"create_agent failed: {e}")
    text = "\n".join(
        p.get("text", "") for p in result.get("content", []) or []
    )
    if result.get("isError") or text.lstrip().lower().startswith("error"):
        raise web.HTTPBadRequest(reason=text[:200] or "create_agent error")
    # Invalidate token cache so the freshly-created agent shows up.
    _app._agent_token_cache.pop(name, None)
    return _app._success_envelope({
        "agent_id": agent_id,
        "seed_task_id": seed_task_id,
        "project": name,
    }, status=201)


# ── Route registration ──────────────────────────────────────────────


def register_admin_routes(app: web.Application) -> None:
    """Wire every ``/api/router/...`` route into ``app``.

    Called from ``router.app.make_app()`` after the per-project
    catch-all is mounted (the more-specific paths win in aiohttp's
    source-order dispatcher). Each handler is wrapped with the
    Accept-header gate (PR-A) so version-pinned clients reach the
    JSON envelope and unversioned ones get the v1-required error.
    """
    from . import app as _app

    gated = _app._rest_gated

    app.router.add_get(
        "/agent-mcp/api/router/health", gated(health_handler),
    )
    app.router.add_get(
        "/agent-mcp/api/router/projects", gated(list_projects_handler),
    )
    app.router.add_post(
        "/agent-mcp/api/router/projects", gated(create_project_handler),
    )
    app.router.add_get(
        "/agent-mcp/api/router/overview", gated(overview_handler),
    )
    app.router.add_patch(
        "/agent-mcp/api/router/projects/{name}",
        gated(rename_project_handler),
    )
    app.router.add_delete(
        "/agent-mcp/api/router/projects/{name}",
        gated(delete_project_handler),
    )
    app.router.add_post(
        "/agent-mcp/api/router/projects/{name}/stop",
        gated(stop_project_handler),
    )
    app.router.add_get(
        "/agent-mcp/api/router/projects/{name}/client-config",
        gated(client_config_handler),
    )
    app.router.add_get(
        "/agent-mcp/api/router/projects/{name}/installer",
        gated(installer_handler),
    )
    app.router.add_get(
        "/agent-mcp/api/router/projects/{name}/aliases",
        gated(alias_usage_handler),
    )
    app.router.add_delete(
        "/agent-mcp/api/router/projects/{name}/aliases/{alias}",
        gated(remove_alias_handler),
    )
    app.router.add_post(
        "/agent-mcp/api/router/projects/{name}/agents",
        gated(create_agent_handler),
    )


__all__ = ["register_admin_routes"]
