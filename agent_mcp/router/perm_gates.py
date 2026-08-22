"""Route-level permission gates for the router admin surface.

Phase 3 Wave 2 (v5.0.69) of prancy-napping-pie. Wave 1b shipped the
user / group / project-membership CRUD routes behind a permissive
"any logged-in operator" gate. Wave 2 shipped the per-route
``@require_sysadmin`` wrapper this module owned exclusively until
Wave 9 PR 4 replaced it.

Wave 9 PR 4 (prancy-napping-pie) superseded ``@require_sysadmin``
with :func:`require_capability` — a capability-shaped decorator
that consults the per-request :class:`Principal` built by
``require_operator_session_middleware`` (Wave 6 PR 0). Wave 9 PR 6
deleted ``require_sysadmin``; :func:`require_capability` is now the
only route-level gate this module exposes.

WHY a separate module from ``auth_middleware``? The middleware owns
the global "do we have a session at all?" gate; this module owns
per-handler authorisation. Splitting the two keeps the middleware
small (still a single decision matrix) and lets per-handler gates
get richer over time (Wave 3 OIDC group-claim mapping in particular
needed new helpers here; Wave 9 PR 4's capability gate is the next
step in that progression).
"""

from __future__ import annotations

import contextlib
import functools
import sqlite3
from typing import AsyncIterator, Awaitable, Callable

from aiohttp import web

from .single_tenant import bypasses_operator_gate


__all__ = [
    "require_capability",
    "revalidate_capability_or_403",
    "read_body_and_revalidate",
    "revalidated_lock",
    "revalidate_after",
]


def _forbidden_response(req: web.Request, cap: str) -> web.Response:
    """Shared 403 envelope for both the entry gate and the revalidation
    re-check below — same shape, same discriminator, one definition."""
    user = req.get("user") or {}
    username = user.get("username", "<unknown>")
    return web.json_response(
        {
            "success": False,
            "error": "forbidden",
            "message": (
                f"operator {username!r} lacks capability "
                f"{cap!r}; this action requires it"
            ),
        },
        status=403,
        headers={"Cache-Control": "no-store"},
    )


async def revalidate_capability_or_403(
    req: web.Request, cap: str,
) -> web.Response | None:
    """Re-check ``cap`` against a FRESH DB read; refresh the cached Principal.

    R6-F2 (HIGH, live-exploited): ``require_operator_session_middleware``
    resolves the caller's Principal (sysadmin flag + capability set) ONCE
    at request entry, before any body-bearing admin handler's genuine
    yield point — ``await req.read()`` inside ``admin_users_api._json_body``.
    An attacker who controls body-delivery pacing (a slow-drip POST/PATCH)
    can hold that read open while a concurrent request revokes their
    privilege in the DB; the paused handler then resumes and completes its
    write against the PRE-revocation snapshot cached at entry —
    ``require_capability`` below and every ``_caller_is_sysadmin`` /
    ``_caps_caller_lacks`` / ``_membership_grant_denied`` read in
    ``admin_users_api`` all consult that same stale snapshot.

    Mirrors R5-F1's fix for the sibling bug class (one-shot-authenticated
    long-lived operation must re-validate before it matters — there, the
    ``/api/events`` SSE stream re-running its open-time gate before every
    dispatch; here, a body-bearing handler re-running its entry-time gate
    before its write). Call this immediately AFTER the handler's
    ``_json_body`` (or any other body-read await) returns and BEFORE
    anything else that trusts the caller's privilege.

    On success this OVERWRITES ``req['principal']`` and
    ``req['is_sysadmin']`` in place with the freshly-resolved values, so
    every downstream self-escalation guard in the same handler — which
    reads those exact request-scoped keys — sees the live post-
    revalidation state too, not the snapshot taken at entry.

    R9-F4 (HIGH, live-exploited): the checks above re-derive capability
    and group/sysadmin state fresh, but until this fix the caller's
    IDENTITY was still trusted from the entry-time ``req['user']``
    snapshot — a session logged out DURING the yield point was
    invisible to the revalidation, so a privileged write could
    complete using an already-logged-out session. This function now
    also re-runs ``login.resolve_current_user`` against the request's
    own session cookie (when one is present) and denies if the session
    no longer resolves, before ever re-deriving capability/group state.

    Single-tenant mode (ADR-0008) bypasses, mirroring
    :func:`require_capability`. Fail-closed: a missing session user, an
    invalidated session, or any resolution error along the way, denies
    with the same 403 shape the entry gate uses.
    """
    if bypasses_operator_gate():
        return None

    user = req.get("user")
    user_id = user.get("user_id") if user else None
    if not user_id:
        # Reaching a body-bearing admin handler at all implies the
        # middleware already resolved a user; a missing id here means
        # something upstream is broken. Fail closed rather than let a
        # capability check run against no identity.
        return _forbidden_response(req, cap)

    # R9-F4 (HIGH, live-exploited): everything above and below this
    # block re-derives capability/group/sysadmin state from a FRESH DB
    # read, but the caller's IDENTITY itself — ``user`` / ``user_id`` —
    # still comes from ``req.get("user")``, the value the middleware
    # cached ONCE at request entry, before this handler's own yield
    # point. A session invalidated (logged out) DURING that yield point
    # is invisible to every check below: they all faithfully re-confirm
    # that the (now-stale) ``user_id`` still has the capability, never
    # that the SESSION which authenticated it is still live. Re-run the
    # exact same live lookup the entry-time middleware gate uses
    # (``login.resolve_current_user``, backed by ``identity.get_session``)
    # against the request's OWN session cookie, and deny if it no longer
    # resolves. Scoped to requests that actually carry a session cookie —
    # proxy-header SSO identities (Phase 3 Wave 3) have no session row to
    # invalidate and are re-verified fresh on every request already, so
    # they're left untouched here.
    from .login import SESSION_COOKIE_NAME, resolve_current_user

    session_id = req.cookies.get(SESSION_COOKIE_NAME, "")
    if session_id:
        try:
            live_user = resolve_current_user(req)
        except Exception:  # pragma: no cover - defensive
            live_user = None
        if live_user is None:
            return _forbidden_response(req, cap)

    from . import group_resolver
    from ..core.principal_builder import build_operator_principal

    try:
        groups: set[str] | None = set(
            group_resolver.resolve_user_groups(user_id)
        )
    except sqlite3.OperationalError:
        groups = None
    except Exception:  # pragma: no cover - defensive
        groups = None

    try:
        sysadmin = group_resolver.resolve_user_is_sysadmin(
            user_id, groups=groups,
        )
    except sqlite3.OperationalError:
        sysadmin = False
    except Exception:  # pragma: no cover - defensive
        sysadmin = False

    # These 8 handlers all live under ``/agent-mcp/api/router/...``,
    # which ``auth_middleware._project_from_path`` treats as the
    # non-project ``router`` admin segment (never project-scoped) — the
    # ENTRY-time Principal built for this route family always carries
    # ``project_role=None`` too, so re-deriving it here with the same
    # ``None`` keeps this revalidation exactly equivalent to a fresh
    # run of the entry-time construction, just later in the request.
    principal = build_operator_principal(
        user_id=str(user_id),
        kind="operator_session",
        project_role=None,
        sysadmin=sysadmin,
        groups=groups,
    )
    req["principal"] = principal
    req["is_sysadmin"] = sysadmin

    if not principal.has_capability(cap):
        return _forbidden_response(req, cap)
    return None


async def read_body_and_revalidate(
    req: web.Request,
    parse_body: Callable[[web.Request], Awaitable[dict]],
    cap: str,
    project_name: str | None = None,
    *,
    min_role: str | None = None,
) -> tuple[dict, web.Response | None]:
    """Read the JSON body AND revalidate — ONE call, not two.

    OBS-R11-1 (architectural): every one of R6-F2 / R7-F1 / R7-F3 / R8-F3 /
    R9-F2 / R9-F3 / R9-F4 was the SAME shape — a handler snapshots the
    caller's Principal at entry, does a genuine ``await`` (a body-read
    here; a lock-acquire for ``revalidated_lock`` below), and trusts the
    stale pre-await snapshot for the write that follows. The revalidation
    helpers (this module's ``revalidate_capability_or_403`` and
    ``admin_api._revalidate_capability_and_membership_or_403``) already
    re-derive every axis correctly — capability, membership, role-tier,
    session-liveness — but calling them was OPT-IN: a handler author had
    to remember to (1) call the body-read, (2) separately call the
    revalidator, immediately after, with the right project/min_role
    arguments, and NOT do anything privileged in between. Six rounds
    across two files rediscovered that "remember to" gap seven times.

    Fusing the read and the revalidation into one coroutine removes the
    opt-in step entirely: there is no way to obtain ``body`` without the
    revalidation ALSO having run, because they're the same call. A future
    handler that awaits ``parse_body`` directly and skips this wrapper is
    exactly the shape the static-analysis test in
    ``tests/router/test_arch_enforced_revalidation.py`` flags — the
    structural guarantee is enforced by that test, not by hoping the next
    author reads this docstring.

    ``parse_body`` is the caller's own body-parser (``admin_api``'s
    ``_parse_json_body`` / ``admin_users_api``'s ``_json_body`` — kept as
    a parameter rather than duplicated or unified here, since the two
    emit slightly different malformed-JSON error discriminators and
    unifying that is out of this refactor's scope). It still raises
    ``web.HTTPBadRequest`` the same way it always did on a malformed
    body; only the shape of what happens on a WELL-FORMED body changes.

    ``project_name=None`` (the create/user/group-CRUD shape, no resource
    to scope to yet) revalidates capability alone via
    ``revalidate_capability_or_403``. A given ``project_name`` (the
    rename/membership shape) revalidates capability AND membership
    together via ``admin_api._revalidate_capability_and_membership_or_403``
    — imported lazily here (mirroring this module's existing lazy-import
    convention for ``.login`` / ``.group_resolver`` above) to avoid a
    module-load-time cycle with ``admin_api``, which itself lazily
    imports ``revalidate_capability_or_403`` from this module.
    ``min_role`` threads straight through to that combined check.

    Returns ``(body, denied)`` — ``denied`` is ``None`` on success (caller
    proceeds using ``body``) or the 403 envelope to return immediately.
    """
    body = await parse_body(req)
    if project_name is not None:
        from .admin_api import _revalidate_capability_and_membership_or_403

        denied = await _revalidate_capability_and_membership_or_403(
            req, cap, project_name, min_role=min_role,
        )
    else:
        denied = await revalidate_capability_or_403(req, cap)
    return body, denied


@contextlib.asynccontextmanager
async def revalidated_lock(
    req: web.Request,
    cap: str,
    project_name: str,
    *,
    min_role: str | None = None,
    role: str = "backend",
) -> AsyncIterator[web.Response | None]:
    """Acquire the per-project ``_ensure_lock`` AND revalidate — as ONE
    atomic unit, before control ever reaches the ``async with`` body.

    OBS-R11-1: the lock-based sibling of ``read_body_and_revalidate``
    above, for handlers whose genuine yield point is lock CONTENTION
    (``delete_project_handler`` / ``stop_project_handler``) rather than a
    body-read. ``rename_project_handler`` also holds an ``_ensure_lock``
    (for the destructive stop/move/registry-rename sequence); OBS-R11-1's
    initial consolidation left its revalidation wired ONLY to the earlier
    body-read yield point (see ``read_body_and_revalidate``), deliberately
    not widening scope during that refactor. R13-F1 (HIGH, live-exploited)
    found that this left rename's OWN ``_ensure_lock`` contention — a real,
    independently-exploitable yield point, up to a full backend cold-boot
    (~9s) — completely unrevalidated: a caller's capability/membership
    revoked while blocked on this lock could still complete the rename.
    ``rename_project_handler`` now routes this lock acquisition through
    ``revalidated_lock`` too, exactly like delete/stop, IN ADDITION to
    (not instead of) its existing body-read revalidation — both of
    rename's genuine yield points are revalidated now. Lock acquisition
    and revalidation used to be two separate statements
    inside a handler (``async with _app._ensure_lock(...): ...
    await _revalidate_capability_and_membership_or_403(...)``) — nothing
    stopped a future handler from acquiring the lock and forgetting the
    second line, or reordering them so privileged work ran before the
    re-check. Folding both into one ``@asynccontextmanager`` makes that
    reordering impossible: the revalidation runs INSIDE the lock,
    immediately after acquisition, before this generator's ``yield``
    ever hands control back to the caller's ``async with`` body — so
    there is no statement position left where "acquire, then act, then
    (maybe) revalidate" could be written.

    Yields the ``denied`` value (``None`` on success, else the 403/404
    envelope) — the caller checks it exactly like every other gate in
    this codebase (``if denied is not None: return denied``) before
    doing the destructive work, still inside the ``async with`` block so
    the lock is held for the whole privileged section and released on
    exit either way (mirrors ``_app._ensure_lock``'s own release
    semantics — this wraps it, not replaces it).

    R14-F2 (HIGH, live-exploited): this ``yield`` only ever revalidates
    ONCE, at entry. Holding the lock does NOT protect anything a caller
    does after this point from a concurrent, unrelated revocation — see
    ``revalidate_after`` below, which every ``await``/``asyncio.to_thread``
    call INSIDE this block must now be routed through instead of being
    awaited bare, so the destructive write that follows always sits
    right after a fresh re-check, not this generator's one-shot entry
    snapshot.
    """
    from . import app as _app
    from .admin_api import _revalidate_capability_and_membership_or_403

    async with _app._ensure_lock(project_name, role):
        denied = await _revalidate_capability_and_membership_or_403(
            req, cap, project_name, min_role=min_role,
        )
        yield denied


async def revalidate_after(
    awaitable: Awaitable[object],
    req: web.Request,
    cap: str,
    project_name: str,
    *,
    min_role: str | None = None,
) -> tuple[object, web.Response | None]:
    """Await ``awaitable`` AND revalidate immediately after it resolves —
    ONE call, not two.

    R14-F2 (HIGH, live-exploited): ``revalidated_lock`` above revalidates
    capability+membership exactly ONCE, immediately after lock
    acquisition, then ``yield``s control to the caller's protected
    ``async with`` body. Everything the caller does AFTER that —
    while STILL lexically inside the block, still holding the lock —
    ran completely un-rechecked. A held ``asyncio.Lock`` only blocks
    OTHER coroutines racing for the SAME lock; it does nothing to stop
    an unrelated capability/membership DELETE (a different request
    entirely) from committing to the DB while THIS coroutine is
    suspended mid-``await`` inside the "protected" block.
    ``rename_project_handler`` / ``delete_project_handler`` /
    ``stop_project_handler`` each held a bare ``asyncio.to_thread``
    systemctl-stop (and, for stop, an ``_is_active`` probe too) await
    immediately inside their ``revalidated_lock`` block, with the
    destructive registry/orchestrator write straight after — a caller
    whose authority was revoked mid-``systemctl stop`` still completed
    the write on the lock's one-shot ENTRY snapshot. Confirmed live: a
    rename request racing a concurrent membership revocation timed to
    land during the ``systemctl stop`` await still renamed the project
    on already-revoked authority.

    Wrap that inner await in this helper instead of calling it bare:
    the revalidation runs the INSTANT the awaitable resolves, so there
    is no statement position left between "the last genuine yield
    point inside the lock" and "the next revalidation" for a
    concurrent revocation to land in undetected. Mirrors
    ``read_body_and_revalidate``'s fusion idiom one level in — that one
    fuses a body-read with the ENTRY revalidation; this one fuses an
    in-lock await with a FRESH re-check right before the destructive
    write that follows it. A handler with more than one await inside
    the lock (``stop_project_handler``'s ``_is_active`` then
    ``systemctl stop``) wraps EACH one, so no matter which is the last
    one actually executed on a given code path, a fresh revalidation
    always sits between it and the write.

    Returns ``(result, denied)`` — ``result`` is whatever ``awaitable``
    resolved to, ``denied`` is ``None`` on success (caller proceeds) or
    the 403/404 envelope to return immediately, exactly like
    ``read_body_and_revalidate``.
    """
    result = await awaitable
    from .admin_api import _revalidate_capability_and_membership_or_403

    denied = await _revalidate_capability_and_membership_or_403(
        req, cap, project_name, min_role=min_role,
    )
    return result, denied


def require_capability(
    cap: str,
) -> Callable[
    [Callable[[web.Request], Awaitable[web.StreamResponse]]],
    Callable[[web.Request], Awaitable[web.StreamResponse]],
]:
    """Reject the request with 403 unless the caller carries ``cap``.

    Wave 9 PR 4 of prancy-napping-pie. The capability-shaped gate for
    router-admin routes; Wave 9 PR 6 deleted the legacy
    ``require_sysadmin`` wrapper this function replaced.

    Reads the per-request :class:`agent_mcp.core.principal.Principal`
    that ``require_operator_session_middleware`` stashes at
    ``request['principal']`` (Wave 6 PR 0) and consults
    :meth:`Principal.has_capability` directly — no second resolution
    chain, no second DB round-trip. Sysadmins admit unconditionally
    via the ``SYSADMIN_WILDCARD`` short-circuit inside
    :meth:`Principal.has_capability`; non-sysadmins admit when their
    resolved capability set (project-role bundle ∪ group-capability
    grants) contains ``cap``.

    Single-tenant mode (ADR-0008) bypasses the gate so the deploy is
    pinned to one operator-owned host. The legacy 410 / validation
    responses for single-tenant-disabled routes surface in their
    natural place rather than being pre-empted by a 403.

    Returns a JSON error envelope (``success: False``,
    ``error: "forbidden"``, ``message`` naming the missing cap and
    the caller) on reject. The dashboard's ApiClient keys off the
    status code (403) plus the ``error`` discriminator.

    Fail-closed: when ``request['principal']`` is missing — which
    happens only if a route is mounted in front of a path that
    bypasses the auth middleware entirely (an unintentional unauth
    allow-list slip) — the wrapper rejects with 403. The middleware
    constructs the Principal in a defensive try/except so a real
    construction failure under load also fails closed here rather
    than admitting silently.
    """

    def decorator(
        handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
    ) -> Callable[[web.Request], Awaitable[web.StreamResponse]]:

        @functools.wraps(handler)
        async def wrapper(request: web.Request) -> web.StreamResponse:
            # Single-tenant mode (ADR-0008): the deploy is pinned to
            # one operator box; there's no audience to gate against
            # here.
            if bypasses_operator_gate():
                return await handler(request)

            principal = request.get("principal")
            if principal is None or not principal.has_capability(cap):
                return _forbidden_response(request, cap)
            return await handler(request)

        return wrapper

    return decorator
