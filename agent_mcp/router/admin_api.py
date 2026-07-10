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

import asyncio
import functools
import json
import logging
import os
import sqlite3
from pathlib import Path

from aiohttp import web


logger = logging.getLogger(__name__)


def _token_dir() -> Path:
    """Resolve the on-disk agent-token directory.

    SEC (owner-authorised, defensive) FINDING 5 [LOW] — ``Path("")`` is
    TRUTHY (it evaluates to ``PosixPath('.')``), so the previous
    ``Path(os.environ.get("AGENT_MCP_TOKENS_DIR", "")) or <default>``
    idiom resolved an UNSET env var to the process CWD rather than the
    intended default. ``token_dir.is_dir()`` then usually pointed at the
    working directory, so the ``<name>--*.token`` purge on delete /
    rename was a silent no-op and stale token files survived. Branch on
    the env var's PRESENCE explicitly so an unset var falls through to
    the real default.
    """
    env = os.environ.get("AGENT_MCP_TOKENS_DIR")
    if env:
        return Path(env)
    return Path.home() / ".config" / "agent-mcp" / "tokens"


def _reject_non_str_name(value) -> web.Response | None:
    """PF-R8-1: guard the ``name`` body field against a non-string JSON value.

    The create / rename handlers read ``name`` via ``(body.get("name") or
    "").strip()``. For a JSON ``dict`` / ``list`` / ``int``, ``value or ""``
    returns ``value`` and ``value.strip()`` raises ``AttributeError`` →
    uncaught 500. ``_validate_name``'s slug check only runs AFTER the strip,
    so it can't catch this. Reject a non-``str`` here, up front, with the
    handler's existing 400 ``invalid_name`` envelope. ``None`` (missing
    field) is allowed through so the ``"" `` default + ``_validate_name``
    still emit the canonical "name is required" message. Returns the error
    response to hand back, or ``None`` when the value is fine.
    """
    from . import app as _app

    if value is not None and not isinstance(value, str):
        return _app._error_envelope(
            error=_app._ERROR_INVALID_NAME,
            message="name must be a string",
            status=400,
        )
    return None


# ── Health (public) ─────────────────────────────────────────────────


async def health_handler(req: web.Request) -> web.Response:
    """``GET /agent-mcp/api/router/health`` — public service descriptor.

    The one admin endpoint that bypasses the operator-session gate
    (allow-listed in ``_UNAUTH_PREFIXES``). Lets external monitors
    probe liveness without minting an operator session.
    """
    from . import app as _app

    # SEC (owner-authorised, defensive): this endpoint is PUBLIC
    # (allow-listed, no session required). It must not echo the internal
    # package version — an unauthenticated liveness probe should learn
    # only "the router is up", not the deployed build number.
    return web.json_response(
        {
            "ok": True,
            "service": "agent-mcp-router",
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

    # SEC FINDING 4: filter to projects the caller may see (sysadmin →
    # all). Was an unfiltered cross-tenant listing.
    names = sorted(_app._projects_dict().keys())
    visible = _app._visible_project_names(req, names)
    return web.json_response(
        {"projects": [n for n in names if n in visible]},
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
    raw_name = body.get("name")
    bad = _reject_non_str_name(raw_name)
    if bad is not None:
        return bad
    name = (raw_name or "").strip()
    err = _app._validate_name(name, _app._projects_dict())
    if err is not None:
        if "already" in err:
            return _app._error_envelope(
                error=_app._ERROR_ALREADY_REGISTERED, message=err, status=409,
            )
        return _app._error_envelope(
            error=_app._ERROR_INVALID_NAME, message=err, status=400,
        )
    # BL-R33-1 [data-integrity/routing]: refuse to create a REAL project
    # whose name is a currently-active grace-period alias of ANOTHER
    # project. ``orchestrator.resolve()`` returns the real project BEFORE
    # falling back to the alias, so a create on an active-alias name would
    # silently SHADOW the alias and redirect legacy clients (still hitting
    # the old name during its ADR-0010 deprecation window) into a DIFFERENT
    # project/DB — with no 409, no deprecation header, no warning. Mirror
    # the sibling ``rename_project_handler`` guard exactly (same
    # ``resolve_alias`` check, ``_ERROR_ALIAS_COLLISION``, 409, message
    # shape) so create enforces the same invariant as rename / add_alias.
    # An EXPIRED alias returns None here, so it stays reclaimable.
    if _app._REGISTRY.resolve_alias(name) is not None:
        return _app._error_envelope(
            error=_app._ERROR_ALIAS_COLLISION,
            message=f"name {name!r} is an active alias",
            status=409,
        )
    workspace = (_app.DEFAULT_WORKSPACE_PARENT / name).expanduser().resolve()
    try:
        workspace.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        # SD-R15-2: don't reflect the resolved ABSOLUTE workspace path
        # (server home dir / username) into the client body — mirror the
        # already-hardened rename handler and keep only the generic OS
        # category (``e.strerror``, e.g. "Permission denied"). Log the
        # real path server-side.
        logger.error(
            "could not create workspace %s: %s", workspace, e.strerror,
        )
        return _app._error_envelope(
            error=_app._ERROR_INTERNAL,
            message=f"could not create project workspace: {e.strerror}",
            status=500,
        )
    try:
        _app._REGISTRY.register(name, str(workspace))
    except ValueError as e:
        # SD-R34-1 class-sweep: ``ProjectRegistry.register`` raises a
        # ValueError whose text embeds the ABSOLUTE workspace path(s)
        # (``… already registered at '<abs>'; refusing to re-point at
        # '<abs>'``). Reflecting ``str(e)`` here would disclose the same
        # server filesystem layout the success-envelope fix + the SD-R6-2
        # rename registry-error scrub already close. This branch is a
        # narrow TOCTOU (``_validate_name`` above already 409s a known
        # name in-memory), but scrub defensively: log the detail
        # server-side, hand back a generic message under the existing
        # discriminator so the caller still sees ``already_registered``.
        logger.warning("register %r failed: %s", name, e)
        return _app._error_envelope(
            error=_app._ERROR_ALREADY_REGISTERED,
            message="project name is already registered",
            status=409,
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
    # SD-R34-1 [info-disclosure]: don't reflect the fully-resolved
    # ABSOLUTE workspace path (server home dir / deployment filesystem
    # layout) into the SUCCESS envelope — it's readable by any
    # ``system.projects.manage`` holder, including a delegated-cap
    # non-sysadmin operator. Emit the SAME project-relative label the
    # overview handler uses (``_workspace_label``), so create / overview
    # are consistent. Missed sibling of the overview scrub + the SD-R15
    # create/rename/delete ERROR-path scrubs; the create SUCCESS path was
    # the last leaker.
    return _app._success_envelope(
        {
            "project": {
                "name": name,
                "workspace": _app._workspace_label(str(workspace)),
            },
        },
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
    raw_new_name = body.get("name")
    bad = _reject_non_str_name(raw_new_name)
    if bad is not None:
        return bad
    new_name = (raw_new_name or "").strip()
    grace_days_raw = body.get("grace_days", 30)
    try:
        grace_days = int(grace_days_raw)
    except (TypeError, ValueError, OverflowError):
        # PF-R18-1: ``int(float('inf'))`` raises OverflowError (not
        # ValueError/TypeError). A JSON number token like ``1e400`` parses
        # to ``float('inf')`` via ``json.loads``, so an overflowing
        # ``{"grace_days": 1e400}`` slipped past the ``(TypeError,
        # ValueError)`` guard to a 500. Catch it too so it 400s like the
        # non-numeric cases before any destructive rename step runs.
        return _app._error_envelope(
            error=_app._ERROR_INVALID_NAME,
            message=f"grace_days must be an integer, got {grace_days_raw!r}",
            status=400,
        )
    # SEC (owner-authorised, defensive) FINDING 3 [MED-HIGH] — bound
    # grace_days BEFORE any destructive step. ``_REGISTRY.rename``
    # computes ``datetime.now(UTC) + timedelta(days=grace_days)``; an
    # unbounded huge int raises ``OverflowError``, which the rollback
    # ``except (ValueError, KeyError)`` below does NOT catch. By then the
    # backend has been stopped and the workspace + token files renamed,
    # so the project is left half-renamed (bricked): registry still on
    # the old name, disk on the new. Reject out-of-range up front —
    # before the ``systemctl stop`` / workspace rename / token rename /
    # registry rename — so nothing destructive runs on a bad value.
    # 0..3650 days (10 years) is a generous alias grace window.
    if not (0 <= grace_days <= 3650):
        return _app._error_envelope(
            error=_app._ERROR_INVALID_NAME,
            message="grace_days must be between 0 and 3650",
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
    # BL-R35-1 [data-integrity/availability]: hold ``_ensure_lock(old_name,
    # "backend")`` across the WHOLE destructive sequence (stop → workspace
    # move → token move → registry rename), mirroring delete's BL-R6-1. The
    # registry-existence probe above runs OUTSIDE this lock, so without it a
    # concurrent ``_ensure(old_name)`` warm-start (a member/dashboard
    # ``GET /app/<old>/`` → ``_schedule_backend_warm``) could, in the
    # stop→os.rename window, ``systemctl start agent-mcp@old_name`` against
    # the ALREADY-MOVED workspace — a half-renamed / orphan backend the idle
    # reaper only clears after ``IDLE_SEC`` (~4 h). Holding the lock forces
    # such a warm-start to either (a) run to completion BEFORE we acquire —
    # then our ``stop`` reaps its unit and the state-pop below clears its
    # orchestrator state — or (b) block on this lock and, on acquiring it
    # after we release, hit ``_ensure``'s inside-lock registry re-check
    # (BL-R6-1), which sees ``old_name`` gone (renamed, not alias-resolved by
    # ``registry.get``) and aborts with 404. ``_ensure_lock`` is a plain
    # per-(name,role) ``asyncio.Lock`` (non-reentrant); nothing we call under
    # it re-acquires the same key, so holding it across the awaited
    # ``to_thread`` stop cannot deadlock (delete does exactly this).
    async with _app._ensure_lock(old_name, "backend"):
        # OBS-R34-RENAME-ONLOOP [availability]: run the blocking ``systemctl
        # stop`` OFF the event loop (mirrors delete's BL-R7-3).
        # ``_systemctl`` is a synchronous ``subprocess.run`` (~15-150 ms, or
        # up to the SC-R7-2 timeout on a D-Bus stall); calling it directly
        # while holding ``_ensure_lock`` stalls the single aiohttp event
        # loop — every other concurrent router request — for the duration.
        await asyncio.to_thread(
            _app._systemctl, "stop", _app._unit_name(old_name, "backend"),
        )
        # BL-R35-1 (mirrors delete's BL-R7-2): purge the per-OLD-name
        # orchestrator lifecycle state, inside the lock (atomic with
        # stop+registry rename) so a warm-start that raced in before we
        # acquired the lock can't leave stale old-name state, and a
        # ``_schedule_backend_warm`` for a same-name RE-created project isn't
        # skipped by the ``(name,"backend") in last_active`` dedup. After the
        # registry rename lands, ``old_name`` is an inactive alias and
        # ``_ensure(old_name)`` 404s, so none of these get repopulated.
        _app.last_active.pop((old_name, "backend"), None)
        _app.ensure_failures.pop((old_name, "backend"), None)
        _app.active_conns.pop(old_name, None)
        _app._po.unit_start_times.pop((old_name, "backend"), None)
        _app._po.forwarding_hmac_keys.pop(old_name, None)
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
        token_dir = _token_dir()
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
            # SD-R6-2: don't reflect the raw ``ValueError``/``KeyError`` text
            # (which can echo registry internals / caller input) into the
            # client envelope. Log the detail server-side; hand back a
            # generic message.
            logger.warning(
                "registry rename %r -> %r failed: %s", old_name, new_name, e,
            )
            return _app._error_envelope(
                error=_app._ERROR_INTERNAL,
                message="registry rename failed",
                status=500,
            )
    # SC-R8-1 (mirrors delete): drop the per-OLD-name ``_ensure`` lock now
    # that the block above has RELEASED it — otherwise create/rename of N
    # distinct names leaks N ``asyncio.Lock`` objects. Only reached on the
    # success path (both early returns above leave ``old_name`` a live
    # project whose lock must stay). A concurrent ``_ensure`` already
    # awaiting this lock keeps its own reference and aborts on the
    # inside-lock registry re-check; a later ``_ensure`` mints a fresh lock.
    _app.ensure_locks.pop((old_name, "backend"), None)
    # SEC (owner-authorised, defensive) FINDING AZ-R13-1 [MED] — migrate
    # the router.db authority table now that the registry rename has
    # landed. ``project_membership`` keys per-user AND per-group grants on
    # a bare TEXT ``project_name`` (no FK cascade), so a rename that
    # touched only the registry / workspace / token files ORPHANED every
    # membership row under the OLD name: (1) members lose access under the
    # new name (lockout), and (2) re-creating a NEW project reusing the
    # old name silently RESURRECTS the prior members' roles on that fresh
    # project (cross-tenant privilege resurrection). This is the RENAME
    # sibling of the round-3 delete-purge fix (#283), which class-swept
    # delete but missed rename. ``project_membership`` is the ONLY
    # project_name-keyed table in router.db; per-project data lives in the
    # per-project DB under the workspace dir, which the rename already
    # moved via ``os.rename``. Best-effort + idempotent (single atomic
    # UPDATE) — a cleanup failure is logged, never fails the rename.
    try:
        from .identity import _connect

        with _connect() as conn:
            conn.execute(
                "UPDATE project_membership SET project_name = ? "
                "WHERE project_name = ?",
                (new_name, old_name),
            )
    except Exception:
        logger.exception(
            "rename_project: failed to migrate project_membership %r -> %r",
            old_name,
            new_name,
        )
    # BL-R35-1 parity (SC-3 sibling): the systemd unit sets
    # ``RuntimeDirectoryPreserve=yes`` (nix/module.nix), so the OLD-name
    # runtime dir ``$AGENT_MCP_SOCK_DIR/<old_name>/`` (its ``backend.sock``
    # and per-project ``forwarding_hmac`` key) survives the ``systemctl
    # stop`` above and is left on disk for a project that no longer exists
    # under that name (``os.rename`` only moved the WORKSPACE, which lives
    # under the projects root — not the runtime dir under SOCK_DIR). Delete
    # purges this; rename didn't. Purge it here so a stale socket + HMAC key
    # for the gone name don't linger. Best-effort + ignore-if-absent; a
    # cleanup failure must not fail the rename. Slug-guarded before an
    # ``rmtree`` under the shared runtime root (``old_name`` is already a
    # validated slug — belt-and-suspenders, mirroring delete).
    if _app._SLUG_RE.match(old_name):
        runtime_dir = _app.SOCK_DIR / old_name
        if runtime_dir.is_dir():
            import shutil
            try:
                shutil.rmtree(runtime_dir)
            except OSError:
                logger.exception(
                    "rename_project: failed to purge runtime dir %s",
                    runtime_dir,
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
        # SD-R15 class-sweep: the ``workspace_delete_skipped_reason`` is
        # surfaced to the client, so it must not carry the resolved
        # ABSOLUTE workspace path (server home dir / username). Log the
        # path server-side; keep the reason generic (and, for the rmtree
        # failure, only the generic OS category via ``e.strerror``).
        if not _app._is_within_default_workspace(workspace_path):
            logger.warning(
                "refusing recursive delete: workspace %s resolves outside "
                "the default workspace parent", workspace_path,
            )
            workspace_delete_skipped_reason = (
                "workspace resolves outside the default workspace parent; "
                "refusing recursive delete"
            )
        elif workspace_path.exists():
            import shutil
            try:
                shutil.rmtree(workspace_path)
                workspace_deleted = True
            except OSError as e:
                logger.error(
                    "rmtree(%s) failed: %s", workspace_path, e.strerror,
                )
                workspace_delete_skipped_reason = (
                    f"could not delete project workspace: {e.strerror}"
                )
        else:
            logger.info(
                "workspace %s did not exist on disk at delete time",
                workspace_path,
            )
            workspace_delete_skipped_reason = (
                "workspace did not exist on disk"
            )
            workspace_deleted = True
    # BL-R6-1 (belt-and-suspenders): hold ``_ensure_lock(name,
    # "backend")`` across the stop+unregister so a concurrent
    # ``_ensure`` warm-start can't interleave — either it spawns before
    # us and our ``stop`` reaps it, or it blocks on this lock and its
    # inside-lock registry re-check (project_orchestrator._ensure) sees
    # the unregister and aborts. Without this pairing, a warm-start that
    # started the backend between our ``stop`` and our ``unregister``
    # would leave an orphan the reaper only clears after ``IDLE_SEC``.
    async with _app._ensure_lock(name, "backend"):
        # BL-R7-3: run the blocking ``systemctl stop`` OFF the event
        # loop (mirrors the round-6 BL-R6-2b fix in ``_ensure``).
        # ``_systemctl`` is a synchronous ``subprocess.run`` (~15-150 ms,
        # or up to the SC-R7-2 timeout on a D-Bus stall); calling it
        # directly while holding ``_ensure_lock`` stalls every other
        # tenant's request on this single event loop for the duration.
        await asyncio.to_thread(
            _app._systemctl, "stop", _app._unit_name(name, "backend"),
        )
        try:
            _app._REGISTRY.unregister(name)
        except KeyError:
            pass
        # BL-R7-2: purge the per-name orchestrator lifecycle state.
        # This handler stops the unit directly instead of routing
        # through ``orchestrator.stop()`` (which pops ``last_active``),
        # so without this the deleted project lingered in
        # ``last_active`` / ``list_active()`` until the idle reaper —
        # and ``_schedule_backend_warm``'s ``(name,"backend") in
        # last_active`` dedup would then skip warm-starts for a
        # same-name RE-created project. Pop every sibling map keyed by
        # this name, inside the lock (atomic with stop+unregister) so a
        # concurrent ``_ensure`` that just released can't repopulate
        # them. ``_ensure``'s inside-lock registry re-check (BL-R6-1)
        # aborts before it writes any of these once we've unregistered.
        _app.last_active.pop((name, "backend"), None)
        _app.ensure_failures.pop((name, "backend"), None)
        _app.active_conns.pop(name, None)
        _app._po.unit_start_times.pop((name, "backend"), None)
        _app._po.forwarding_hmac_keys.pop(name, None)
    # SC-R8-1: drop the per-name ``_ensure`` lock too — but only AFTER
    # the ``async with _ensure_lock(...)`` block above has RELEASED it.
    # Popping while holding would break the lock's release semantics, so
    # the sibling-map purge above (which must be atomic with
    # stop+unregister) can't include it. Without this pop, create+delete
    # of N distinct project names leaks N ``asyncio.Lock`` objects
    # forever. A concurrent ``_ensure`` that was already awaiting this
    # lock still holds its own reference and will abort on the round-6
    # inside-lock registry re-check (the project is now unregistered); a
    # later ``_ensure`` mints a fresh lock via ``_ensure_lock``.
    # Idempotent.
    _app.ensure_locks.pop((name, "backend"), None)
    # SEC (owner-authorised, defensive) FINDING 2: purge router.db
    # membership + on-disk agent-token files. ``project_membership``
    # keys per-user AND per-group grants on a bare TEXT
    # ``project_name`` (no FK), so without this delete the rows
    # survive; re-creating the same-named project (deterministic
    # workspace path) would silently resurrect every prior member's
    # caps. The ``<name>--*.token`` files were likewise cleaned only
    # on rename. Both are best-effort + idempotent — a cleanup failure
    # must not fail the delete.
    try:
        from .identity import _connect

        with _connect() as conn:
            conn.execute(
                "DELETE FROM project_membership WHERE project_name = ?",
                (name,),
            )
    except Exception:
        logger.exception(
            "delete_project: failed to purge project_membership for %s",
            name,
        )
    token_dir = _token_dir()
    if token_dir.is_dir():
        for tok in token_dir.glob(f"{name}--*.token"):
            try:
                tok.unlink()
            except OSError:
                pass
    # SC-3 [LOW]: the systemd unit sets ``RuntimeDirectoryPreserve=yes``
    # (nix/module.nix) so the per-project ``forwarding_hmac`` key survives
    # stop/start cycles — but that same preservation leaves the key (and
    # backend.sock) at ``$AGENT_MCP_SOCK_DIR/<name>/`` after a delete, on
    # disk for a now-gone project. Purge the runtime dir here (systemd
    # won't, precisely because we asked it to preserve). Best-effort +
    # ignore-if-absent; a cleanup failure must not fail the delete. The
    # slug guard mirrors the token/workspace cleanup's defensive posture:
    # ``name`` is already a validated, registered project slug, but we
    # re-check before an ``rmtree`` under a shared runtime root.
    if _app._SLUG_RE.match(name):
        runtime_dir = _app.SOCK_DIR / name
        if runtime_dir.is_dir():
            import shutil
            try:
                shutil.rmtree(runtime_dir)
            except OSError:
                logger.exception(
                    "delete_project: failed to purge runtime dir %s",
                    runtime_dir,
                )
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
    # OBS-R34-RENAME-ONLOOP class-sweep: ``_is_active`` and ``_systemctl``
    # both shell out (blocking ``subprocess.run``), so calling them
    # directly stalls the single aiohttp event loop — every other
    # concurrent router request — for the duration. Run both off-loop via
    # ``asyncio.to_thread`` (mirrors the orchestrator's BL-R6-2b and
    # delete's BL-R7-3).
    if await asyncio.to_thread(_app._is_active, unit):
        r = await asyncio.to_thread(_app._systemctl, "stop", unit)
        if r.returncode != 0:
            # SD-R15-1: sibling of SC-R8-2 (project_orchestrator._ensure).
            # Reachable via the delegatable ``system.projects.manage`` cap,
            # so a non-sysadmin delegate could otherwise read raw systemd
            # stderr (unit-file paths, "Failed at step EXEC …"). Log the
            # detail server-side; hand the client a static message with no
            # unit path and no stderr.
            logger.error(
                "systemctl stop %s failed (rc=%s): %s",
                unit, r.returncode, r.stderr.strip(),
            )
            return _app._error_envelope(
                error=_app._ERROR_INTERNAL,
                message="failed to stop project backend",
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
        # OBS-R34-RENAME-ONLOOP class-sweep: ``_build_overview_envelope``
        # loops over every project calling ``_is_active`` (blocking
        # ``systemctl is-active`` subprocess) plus a blocking SQLite COUNT
        # fan-out, so building it directly stalls the single aiohttp event
        # loop — every other concurrent router request — for N sequential
        # subprocess calls. The builder is read-only (``_REGISTRY.list()``
        # returns a fresh snapshot; no shared-state mutation), so run the
        # whole thing off-loop via ``asyncio.to_thread`` (mirrors the
        # rename/stop fixes and delete's BL-R7-3).
        envelope = await asyncio.to_thread(_app._build_overview_envelope)
        _app._overview_cache = (now + _app._OVERVIEW_CACHE_TTL_SEC, envelope)
    # SEC FINDING 4: the envelope is cached process-wide (full, cross-
    # tenant); filter the ``projects`` list per-request to the caller's
    # memberships (sysadmin → all) so an operator only sees THEIR
    # projects' names / stats / aliases. Shallow-copy so the cached
    # full envelope is never mutated.
    all_projects = envelope.get("projects", [])
    visible = _app._visible_project_names(
        req, [p["name"] for p in all_projects],
    )
    filtered = dict(envelope)
    filtered["projects"] = [p for p in all_projects if p["name"] in visible]
    return web.json_response(filtered, headers={"Cache-Control": "no-store"})


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
        # Fixed reason phrase — never reflect the caller-supplied name
        # into the HTTP status line (SEC4 pattern).
        raise web.HTTPNotFound(reason="unknown project")
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
        # Fixed reason phrase — see client_config_handler (SEC4 pattern).
        raise web.HTTPNotFound(reason="unknown project")
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
    # Fixed reason phrases — never reflect the caller-supplied alias or
    # project name into the HTTP status line (SEC4 pattern).
    if real_name is None:
        raise web.HTTPNotFound(reason="unknown alias")
    if real_name != project_name:
        # The path-keyed project doesn't own this alias. Treat as
        # 404 — same UX as "alias not found here".
        raise web.HTTPNotFound(reason="unknown alias")
    row = _app._REGISTRY.get(real_name)
    if row is None:
        raise web.HTTPNotFound(reason="unknown alias")
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
        # Fixed reason phrase — see client_config_handler (SEC4 pattern).
        raise web.HTTPNotFound(reason="unknown project")
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


# ── Admin create-agent wrapper (retired) ────────────────────────────
#
# retire-system-token Wave 5 (2026-06-23) deleted ``create_agent_handler``
# and the route registration for ``POST /agent-mcp/api/router/projects/
# <name>/agents``. The handler proxied through ``_mcp_call_admin``,
# which Wave 3 broke (the helper fetched an ``admin_token`` field that
# the backend no longer returns). The dashboard never called this
# router-level endpoint — it hits the per-project ``POST /api/agents``
# in ``agent_mcp/app/routes.py`` directly — and no test pinned the
# behaviour, so removing it is safe. Callers who need to programmatically
# create agents should use the per-project REST surface; the bootstrap
# task can be created with a separate ``assign_task`` call.


# ── DiD-R7: operator-membership gate for the wiring routes ──────────


def _require_project_operator_membership(handler):
    """Require SYSADMIN or ``operator``-membership of the ``{name}`` project.

    DiD-R7 (defense-in-depth, 2026-07-09). The two wiring routes —
    ``client-config`` and ``installer`` — embed a LIVE agent bearer for
    the target project. Round-6 (#322) gated them on the delegable
    ``system.projects.manage`` capability, same as the sibling create /
    delete / rename routes. But that cap is DEPLOYMENT-WIDE: a sysadmin
    can grant it to a group whose members are NOT members of the target
    project (the Wave-9 delegation model), so a delegated-cap-only
    non-member could pull another tenant's live agent bearer.

    This is currently INERT — ``_resolve_agent_token`` yields an empty
    map because ``GET /api/tokens`` requires confirmed-operator tier, so
    the embedded token is empty for everyone — but it's a latent
    landmine if that token-map wiring is ever repaired. Cheap DiD: layer
    an operator-membership check on top of the cap gate for these two
    routes only.

    Composed INSIDE ``require_capability("system.projects.manage")`` (see
    ``register_admin_routes``): a caller lacking the cap is already
    denied at the cap layer with the cap-named message the round-6
    cross-tenant tests pin; this wrapper only runs for cap-holders and
    then additionally demands sysadmin OR project-``operator`` role. A
    viewer-tier member, a non-member, and a delegated-cap-only
    non-member are all denied here.
    """

    @functools.wraps(handler)
    async def wrapper(req: web.Request) -> web.StreamResponse:
        from . import app as _app

        # Single-tenant (ADR-0008): one operator-owned box; the session
        # middleware bypasses gating entirely, so mirror
        # ``require_capability`` and pass through.
        if _app.SINGLE_TENANT_NAME is not None:
            return await handler(req)
        # Sysadmin admits unconditionally (their cap set is the wildcard).
        if req.get("is_sysadmin"):
            return await handler(req)
        name = req.match_info.get("name", "")
        user = req.get("user") or {}
        user_id = user.get("user_id")
        role = None
        if user_id:
            from . import group_resolver

            try:
                role = group_resolver.resolve_user_project_role(user_id, name)
            except Exception:  # pragma: no cover - defensive
                # router.db not migrated / transient DB error → fail
                # closed (deny) rather than over-disclose the bearer.
                role = None
        if role == "operator":
            return await handler(req)
        username = user.get("username", "<unknown>")
        return web.json_response(
            {
                "success": False,
                "error": "forbidden",
                "message": (
                    f"operator {username!r} must be an operator member of "
                    f"the target project to fetch its wiring "
                    f"(client-config / installer embed a live agent bearer)"
                ),
            },
            status=403,
            headers={"Cache-Control": "no-store"},
        )

    return wrapper


# ── Route registration ──────────────────────────────────────────────


def register_admin_routes(app: web.Application) -> None:
    """Wire every ``/api/router/...`` route into ``app``.

    Called from ``router.app.make_app()`` after the per-project
    catch-all is mounted (the more-specific paths win in aiohttp's
    source-order dispatcher). Each handler is wrapped with the
    Accept-header gate (PR-A) so version-pinned clients reach the
    JSON envelope and unversioned ones get the v1-required error.

    Phase 3 Wave 2 (v5.0.69): project create / delete / rename are
    additionally wrapped with the project-lifecycle gate — the system
    perm matrix reserves project lifecycle (and the rename, which
    is a re-key with grace-alias semantics) for sysadmins.

    SEC FINDING 1 (owner-authorised, defensive, 2026-07-09): the
    per-project wiring / lifecycle routes ``stop``, ``client-config``,
    ``installer``, ``aliases`` (GET), and ``aliases/{alias}`` (DELETE)
    are ALSO wrapped with the project-lifecycle gate. They were
    previously session-only; because ``router`` is exempt from the
    project-membership middleware, ``{name}`` was never checked, so a
    viewer of an unrelated project could read another project's live
    agent token (client-config / installer embed a live bearer) or
    DoS / mutate its backend and aliases. Gating on
    ``system.projects.manage`` matches the sibling create / delete /
    rename routes and closes the cross-tenant hole.

    Wave 9 PR 4 (prancy-napping-pie): the lifecycle gate moved from
    ``require_sysadmin`` to
    ``require_capability("system.projects.manage")``. Sysadmins still
    admit unconditionally (their cap set is the wildcard); the new
    shape ALSO lets a sysadmin grant the cap to a delegated group
    via the Wave 9 PR 5 dashboard UI without promoting the operator
    to sysadmin.
    """
    from . import app as _app
    from .perm_gates import require_capability

    gated = _app._rest_gated
    project_lifecycle_gate = require_capability("system.projects.manage")

    app.router.add_get(
        "/agent-mcp/api/router/health", gated(health_handler),
    )
    app.router.add_get(
        "/agent-mcp/api/router/projects", gated(list_projects_handler),
    )
    app.router.add_post(
        "/agent-mcp/api/router/projects",
        gated(project_lifecycle_gate(create_project_handler)),
    )
    app.router.add_get(
        "/agent-mcp/api/router/overview", gated(overview_handler),
    )
    app.router.add_patch(
        "/agent-mcp/api/router/projects/{name}",
        gated(project_lifecycle_gate(rename_project_handler)),
    )
    app.router.add_delete(
        "/agent-mcp/api/router/projects/{name}",
        gated(project_lifecycle_gate(delete_project_handler)),
    )
    # SEC (owner-authorised, defensive) FINDING 1: these five routes
    # were previously ``gated(...)``-only (session presence). Because
    # ``router`` is in ``_NON_PROJECT_API_SEGMENTS``, the project-
    # membership middleware never checks ``{name}`` either — so any
    # authenticated caller (even a viewer of an unrelated project)
    # could read another project's live agent bearer via client-config
    # / installer, or DoS / mutate its backend + aliases. Gate them on
    # the SAME capability as the sibling create / rename / delete
    # lifecycle routes.
    app.router.add_post(
        "/agent-mcp/api/router/projects/{name}/stop",
        gated(project_lifecycle_gate(stop_project_handler)),
    )
    # DiD-R7: client-config / installer embed a LIVE agent bearer, so
    # they carry an EXTRA operator-membership gate on top of the shared
    # cap gate — a delegated-cap-only non-member must not pull another
    # tenant's bearer. The membership wrapper is composed INSIDE the cap
    # gate so a caller lacking the cap is still denied with the cap
    # message the round-6 cross-tenant tests assert.
    app.router.add_get(
        "/agent-mcp/api/router/projects/{name}/client-config",
        gated(project_lifecycle_gate(
            _require_project_operator_membership(client_config_handler),
        )),
    )
    app.router.add_get(
        "/agent-mcp/api/router/projects/{name}/installer",
        gated(project_lifecycle_gate(
            _require_project_operator_membership(installer_handler),
        )),
    )
    app.router.add_get(
        "/agent-mcp/api/router/projects/{name}/aliases",
        gated(project_lifecycle_gate(alias_usage_handler)),
    )
    app.router.add_delete(
        "/agent-mcp/api/router/projects/{name}/aliases/{alias}",
        gated(project_lifecycle_gate(remove_alias_handler)),
    )
    # retire-system-token Wave 5: the router-level admin create-agent
    # endpoint (``POST .../{name}/agents``) was deleted along with its
    # broken ``_mcp_call_admin`` helper. The dashboard's "Create Agent"
    # flow hits the per-project ``POST /api/agents`` directly.


__all__ = ["register_admin_routes"]
