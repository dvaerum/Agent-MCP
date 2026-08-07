"""Enumeration invariant for the two named single-tenant policies
(arch-r4 #8).

Before this refactor, ``SINGLE_TENANT_NAME is not None`` was re-derived
inline at every call site that needed a single-tenant authorization
decision, driving two contradictory-looking behaviors:

  * ``agent_mcp.router.single_tenant.bypasses_operator_gate()`` — skip
    the operator-session/capability gate and admit the request.
  * ``agent_mcp.router.single_tenant.disables_write_endpoint()`` —
    refuse a router-admin write with 410 instead of performing it.

Nothing caught a new call site inlining a THIRD, unnamed variant, or
forgetting to wire either policy at all. This module is the guard:

  1. ``test_every_policy_site_uses_exactly_one_named_predicate`` walks
     an explicit table of every function that makes a single-tenant
     authorization decision and asserts its source calls exactly one
     of the two named predicates (never the raw comparison, never
     both).
  2. ``test_no_undocumented_raw_comparison`` scans every module under
     ``agent_mcp/router/`` for the raw ``SINGLE_TENANT_NAME is (not)
     None`` comparison and asserts every occurrence is either inside
     ``single_tenant.py`` (the one place that owns the comparison) or
     on the documented informational allow-list (uses of the name as
     DATA — a redirect target, a descriptor field — not as an
     authorization decision). A future call site that inlines the raw
     comparison anywhere else fails this test, whether or not it
     happens to reach the right behavior by accident.

Both are impossible to express meaningfully before this refactor: with
no named module owning the comparison, there is nothing to enumerate
against and no single place to scan.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.no_auth_seed_session

_ACCEPT = {"Accept": "application/vnd.agent-mcp.v1+json"}

_BYPASS = "bypasses_operator_gate"
_DISABLE = "disables_write_endpoint"

# ── 1. Every call site is registered here, exactly once ─────────────
#
# (module path, qualname, expected predicate). ``qualname`` is resolved
# via ``getattr`` chains (dotted for nested/attribute lookups is not
# needed here — every site is a module-level function).
_POLICY_SITES: tuple[tuple[str, str, str], ...] = (
    # bypass-gate-and-admit
    (
        "agent_mcp.router.auth_middleware",
        "require_operator_session_middleware",
        _BYPASS,
    ),
    ("agent_mcp.router.perm_gates", "require_capability", _BYPASS),
    (
        "agent_mcp.router.admin_api",
        "_require_project_operator_membership",
        _BYPASS,
    ),
    ("agent_mcp.router.admin_users_api", "_caller_is_sysadmin", _BYPASS),
    ("agent_mcp.router.app", "_visible_project_names", _BYPASS),
    # disable-endpoint (410)
    ("agent_mcp.router.admin_api", "create_project_handler", _DISABLE),
    ("agent_mcp.router.admin_api", "rename_project_handler", _DISABLE),
    ("agent_mcp.router.admin_api", "delete_project_handler", _DISABLE),
    ("agent_mcp.router.admin_api", "remove_alias_handler", _DISABLE),
)

_OTHER = {_BYPASS: _DISABLE, _DISABLE: _BYPASS}

_RAW_PATTERN = re.compile(r"SINGLE_TENANT_NAME\s+is\s+(not\s+)?None")


@pytest.mark.parametrize(
    "module_path,qualname,expected", _POLICY_SITES,
    ids=[f"{m}.{q}" for m, q, _ in _POLICY_SITES],
)
def test_every_policy_site_uses_exactly_one_named_predicate(
    module_path: str, qualname: str, expected: str, router_module,
) -> None:
    """``router_module`` (not a plain ``importlib.import_module``) is
    required here: ``agent_mcp.router.app`` reads several env vars at
    module import time, and only the fixture sets them up. The other
    modules in the table lazy-import ``app`` internally, so they're
    only genuinely import-clean once ``app`` itself has been loaded
    through the fixture first.
    """
    import importlib

    if module_path == "agent_mcp.router.app":
        module = router_module
    else:
        module = importlib.import_module(module_path)
    fn = getattr(module, qualname)
    source = inspect.getsource(fn)

    assert f"{expected}()" in source, (
        f"{module_path}.{qualname} must call single_tenant.{expected}() "
        f"— it no longer does (source may have drifted back to the raw "
        f"SINGLE_TENANT_NAME comparison, or the predicate got renamed)."
    )
    other = _OTHER[expected]
    assert f"{other}()" not in source, (
        f"{module_path}.{qualname} calls BOTH {expected}() and {other}() "
        f"— a single call site must express exactly one policy (XOR), "
        f"not both."
    )
    assert not _RAW_PATTERN.search(source), (
        f"{module_path}.{qualname} still contains the raw "
        f"SINGLE_TENANT_NAME comparison alongside the named predicate — "
        f"it should rely on {expected}() exclusively."
    )


# ── 2. No new raw comparison anywhere else in the router package ────
#
# Every remaining raw ``SINGLE_TENANT_NAME is (not) None`` outside
# ``single_tenant.py`` is DATA usage (the redirect target, a
# service-descriptor field, the health probe's ``mode`` string) —
# never an authorization decision. Each entry below is deliberate;
# adding a new one requires updating this list (and, if it's actually
# a gating decision, using one of the two named predicates instead).
_INFORMATIONAL_ALLOWLIST: frozenset[tuple[str, str]] = frozenset(
    {
        # health probe's public "mode" descriptor string — informational,
        # not a gate.
        (
            "admin_api.py",
            'if _app.SINGLE_TENANT_NAME is not None',
        ),
        # W1 redirect target selection (uses the configured name as
        # data, substituted into the wrong-project URL).
        (
            "app.py",
            "if SINGLE_TENANT_NAME is None or name == SINGLE_TENANT_NAME:",
        ),
        # overview envelope's informational ``multi_tenant`` /
        # ``single_tenant_name`` fields.
        ("app.py", '"multi_tenant": SINGLE_TENANT_NAME is None,'),
        ("app.py", "if SINGLE_TENANT_NAME is not None:"),
        # service descriptor's "mode" string (index_handler's sibling).
        (
            "app.py",
            (
                '"mode": "single-tenant" if SINGLE_TENANT_NAME is not None '
                'else "multi-tenant",'
            ),
        ),
    }
)


def test_no_undocumented_raw_comparison() -> None:
    router_dir = Path(__file__).resolve().parents[2] / "agent_mcp" / "router"
    assert router_dir.is_dir(), router_dir

    seen: set[tuple[str, str]] = set()
    unexpected: list[str] = []
    for path in sorted(router_dir.glob("*.py")):
        if path.name == "single_tenant.py":
            continue
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1,
        ):
            if not _RAW_PATTERN.search(line):
                continue
            key = (path.name, line.strip())
            if key in _INFORMATIONAL_ALLOWLIST:
                seen.add(key)
                continue
            unexpected.append(f"{path.name}:{lineno}: {line.strip()}")

    assert not unexpected, (
        "found a raw SINGLE_TENANT_NAME comparison outside "
        "single_tenant.py that isn't on the documented informational "
        "allow-list — route it through bypasses_operator_gate() / "
        "disables_write_endpoint(), or add it to "
        "_INFORMATIONAL_ALLOWLIST with a reason if it's genuinely just "
        "data:\n" + "\n".join(unexpected)
    )
    missing = _INFORMATIONAL_ALLOWLIST - seen
    assert not missing, (
        "stale entries in _INFORMATIONAL_ALLOWLIST no longer found in "
        f"the source (update the list to match): {sorted(missing)}"
    )


# ── 3. Behavioral: the bypass semantic actually admits, live ────────


@pytest.mark.asyncio
async def test_single_tenant_bypasses_wiring_membership_gate(
    aiohttp_client, router_module, register_project,
) -> None:
    """DiD-R7's ``_require_project_operator_membership`` (admin_api.py)
    is a ``bypasses_operator_gate()`` consumer distinct from the
    primary session middleware and ``require_capability``. Prove it
    independently: a caller with NO sysadmin bit, NO capability, and
    NO project membership — who ``test_sec_r7_wiring_gate.py`` shows
    would be 403'd by the capability gate alone in multi-tenant mode —
    is ADMITTED under single-tenant, since the whole gate stack
    (session, capability, membership) is a no-op there.
    """
    from agent_mcp.router import identity

    identity.run_router_migrations_upgrade()

    register_project("only-project")
    single_tenant_app = router_module.make_app(
        single_tenant_name="only-project", single_tenant_workspace=None,
    )

    # A brand-new operator: no sysadmin bit, no group, no capability,
    # no project membership. Under multi-tenant this caller is denied
    # by (in order) require_capability, then the membership wrapper.
    # Under single-tenant every one of those gates is a no-op.
    user_id = identity.create_user(
        username="nobody-in-particular", password="passwordpassword",
    )
    assert user_id

    client = await aiohttp_client(single_tenant_app)
    login = await client.post(
        "/agent-mcp/login",
        data={
            "username": "nobody-in-particular",
            "password": "passwordpassword",
        },
        allow_redirects=False,
    )
    assert login.status == 303, await login.text()
    set_cookie = login.headers.get("Set-Cookie")
    cookie = set_cookie.split(";", 1)[0].partition("=")[2].strip()

    resp = await client.get(
        "/agent-mcp/api/router/projects/only-project/client-config",
        headers=_ACCEPT,
        cookies={"agent_mcp_session": cookie},
        allow_redirects=False,
    )
    # Mirrors the ``test_operator_member_admits_wiring`` convention in
    # test_sec_r7_wiring_gate.py: the gate must not 403. The handler
    # itself may still 404 ("unknown agent") because the token map is
    # inert in the test env — that's a different concern from the
    # gate this test targets.
    assert resp.status != 403, await resp.text()
