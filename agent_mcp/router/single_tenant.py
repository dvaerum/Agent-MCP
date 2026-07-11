"""Names the two single-tenant authorization policies (arch-r4 #8).

Single-tenant mode (ADR-0008) pins a router deploy to one
operator-owned host, seeded with exactly one project at install time.
That single fact — ``agent_mcp.router.app.SINGLE_TENANT_NAME is not
None`` — used to be re-derived inline at ~9 call sites across
``auth_middleware.py``, ``perm_gates.py``, ``admin_api.py`` and
``admin_users_api.py``, and it drives TWO semantically different
policies depending on which call site asks:

  1. :func:`bypasses_operator_gate` — "there is no second operator/
     tenant to gate against, so skip the operator-session /
     capability check and admit the request." Consumed by:

       * ``auth_middleware.require_operator_session_middleware`` —
         skips the cookie/session check for the whole dashboard
         surface.
       * ``perm_gates.require_capability`` — skips the per-route
         capability check.
       * ``admin_api._require_project_operator_membership`` — mirrors
         the capability gate's bypass for the DiD-R7 membership
         check on the client-config/installer wiring routes.
       * ``admin_users_api._caller_is_sysadmin`` — treats the caller
         as sysadmin, since the gate that would normally populate
         ``request['principal']`` never ran.
       * ``app._visible_project_names`` — skips the cross-tenant
         project-visibility filter (there is no second tenant's
         projects to hide).

  2. :func:`disables_write_endpoint` — "the project topology is fixed
     for the lifetime of this deploy (one project, seeded at install
     time), so refuse this router-admin write with a 410 instead of
     performing it." Consumed by the four ``admin_api`` handlers that
     mutate the project registry: create, rename, delete, and
     remove-alias.

These are DELIBERATELY CONTRADICTORY-LOOKING policies — one admits
through a gate that would otherwise reject, the other rejects a
request that would otherwise be admitted — which is exactly why they
need to be named separately rather than left as an unlabelled
``is not None`` scattered through the router package. A new gated
route has to pick one of the two named predicates (or neither, if it
has no single-tenant-specific behaviour); there's no third,
unnamed option to reach for by accident.

This module owns the raw ``SINGLE_TENANT_NAME is not None``
comparison for both policies. It does NOT own every read of
``SINGLE_TENANT_NAME`` in the router package — call sites that use
the configured name as DATA (the W1 redirect target, the service
descriptor's ``single_tenant_name`` field, the health probe's
``mode`` string) are a different concern and keep reading the module
global on ``agent_mcp.router.app`` directly.
"""

from __future__ import annotations


def _configured() -> bool:
    """True iff the router was built with a single-tenant name.

    Lazy import so this module stays free of ``app``-level import-time
    side effects and is safe to import eagerly from every call site
    (``app.py`` included) without introducing an import cycle.
    """
    try:
        from . import app as _app
        return _app.SINGLE_TENANT_NAME is not None
    except Exception:  # pragma: no cover - defensive
        return False


def bypasses_operator_gate() -> bool:
    """True iff single-tenant mode skips the operator-session /
    capability gate for this request.

    Single-tenant deploys (ADR-0008) are pinned to one operator-owned
    host; there is no second operator/tenant audience to gate
    against, so the session and capability checks are a no-op here.
    Phase 1 of the operator-login plan does not gate this audience —
    Phase 3 revisits when groups + system perms arrive.
    """
    return _configured()


def disables_write_endpoint() -> bool:
    """True iff single-tenant mode disables this router-admin write
    endpoint, returning a 410 instead of performing the mutation.

    Project topology is fixed for the lifetime of a single-tenant
    deploy (one project, seeded at install time by the home-manager
    module); create/rename/delete/remove-alias have no valid target
    to act on, so they're refused outright rather than silently
    admitted and then failing some other way.
    """
    return _configured()
