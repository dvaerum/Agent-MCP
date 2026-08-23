"""Per-tool access classification used by ``tools/list`` filtering.

The MCP framework's ``tools/list`` returns every registered tool by
default. That worked while a router-side rewrite hid admin-only tools
from worker bearers; Phase 7f deleted that rewrite (per Q7.1: routers
do not manipulate the MCP protocol), so workers started seeing the
full upstream catalogue. They then attempt to call admin-only tools,
the tool's own ``verify_token(.., "admin")`` short-circuit returns
isError=True (PR #15) — but the worker still wasted a tool call and,
worse, the model often loops trying alternatives based on what it can
see in tools/list.

The fix is to filter ``tools/list`` at the backend itself, based on
the calling bearer's role:

* ``"admin"``  — admin sees, worker does not, unauthenticated does
  not.
* ``"any"``    — everyone sees (admin + worker + unauthenticated
  alike).
* ``"worker-if-toggled:<config_key>[,<config_key>...]"`` — admin
  always sees; worker sees iff at least one listed key resolves
  truthy in ``project_context`` (using the helper's own default,
  which is set per key in :data:`_TOGGLE_DEFAULTS` below).

PR-W1c (2026-06-05) refactor — derivation, not hand-maintenance
---------------------------------------------------------------

Before this PR the classification was a hand-maintained dict here.
The two-source-of-truth (decorator + kwarg) had to stay in sync;
the invariant test caught *omissions* but not *contradictions* (a
tool gated one way but classified another would leak into worker
tools/list).

arch-r3 #1+5 PR-A (2026-07-11) — couple visibility to the LIVE cap
------------------------------------------------------------------

PR-W1c derived visibility from the impl's ``_required_role``
attribute. Wave 9 deleted the ``@requires_role`` decorator that set
it; ``_required_role`` is now set NOWHERE, so the derivation silently
fell through to the ``visibility=`` kwarg — a hand-synced label that
could disagree with the real authorization gate. A capability-gated
tool shipped without the kwarg leaked into every worker's / anonymous
caller's ``tools/list`` even though its cap gate would reject them.

The live authority is now :func:`agent_mcp.core.authorize.requires_capability`,
which stamps ``impl._required_capability`` (a member of
:data:`agent_mcp.core.capabilities.KNOWN_CAPABILITIES`). This module
now derives visibility from that cap, mapping cap → tier via the
capability bundles (:func:`_visibility_for_capability`):

* a cap in the **worker** bundle → ``"worker"`` tier (visible to
  worker + manager + admin; hidden from anonymous);
* a cap only in the **manager** bundle → ``"manager"`` tier;
* a cap in **neither** agent bundle (operator/sysadmin-only) →
  ``"operator"`` tier.

The ``visibility=`` kwarg survives ONLY as an explicit override, and
ONLY in the tighten direction — it may hide a tool from a role the cap
would admit (a UX choice), never advertise a tool the cap gate rejects
(that was the leak).

Phase 2 / Finding A (security-architecture hardening) — the fallback
SHRANK, it did not vanish
------------------------------------------------------------------

The first override class used to be "in-body cap checks": tools whose
capability test lived inside the tool body set no
``_required_capability`` on the wrapper, so the derivation could not see
their cap and ``visibility="operator"`` was the only signal. **That
class no longer exists** — every registered tool now declares its
requirement on the impl (``@requires_capability`` /
``@requires_policy`` / ``@requires_predicate``) AND restates it at
``register_tool(requires=...)``, which verifies the two agree at import
time. 19 hand-synced ``visibility=`` kwargs that merely echoed a
derivable tier were deleted with that migration; the invariant test
``tests/test_arch_enforced_tool_capability_registration.py::
test_visibility_kwarg_only_survives_where_it_does_real_work`` keeps
them from creeping back.

Two legitimate override classes remain (N4: the fallback cannot go to
zero):

1. **predicate-gated tools** — ``@requires_predicate`` wraps an
   arbitrary boolean over the Principal (an OR of two caps, a
   ``kind``-AND-cap compound, an operator-tier helper). There is no
   capability to map to a tier, so ``core/authorize.requires_predicate``
   deliberately does NOT expose the predicate to this derivation and the
   kwarg is the tool's only ``tools/list`` signal (``view_agents``,
   ``send_agent_message``, ``broadcast_admin_message``). A
   predicate-gated tool whose tier is ``"any"`` simply omits it.
2. **deliberate tighten** — a worker-callable cap the maintainer
   still keeps out of a worker's tools/list (create_task,
   bulk_task_operations, update_task carry ``tasks.create`` /
   ``tasks.update`` / ``tasks.assign`` but are admin-orchestration
   surfaces). The kwarg tightens ``"worker"`` → ``"operator"``; it is
   honored because it only restricts.

Per-entry the derivation reads:

* the impl's ``_required_capability`` (from ``@requires_capability``)
  → cap tier, with a tightening kwarg override;
* the impl's ``_required_policy_keys`` (from ``@requires_policy``)
  → ``"worker-if-toggled:<keys>"``;
* otherwise the registry entry's ``meta.declared_visibility`` (kwarg) —
  post-Phase-2 that means a predicate-gated or PUBLIC tool.
"""
from __future__ import annotations

import json
from typing import Dict, Iterator, Optional

from ..core.config import logger
from ..core.settings_schema import SETTINGS_SCHEMA, default_for


# Restrictiveness rank of a visibility level, lowest = fewest roles
# admitted. Used to enforce that a ``visibility=`` kwarg override on a
# cap-gated tool may only TIGHTEN (hide from more roles), never loosen
# (advertise a tool the cap gate rejects). ``worker-if-toggled:...`` is
# ranked with ``worker`` for this comparison.
_LEVEL_RANK: Dict[str, int] = {
    "operator": 0,
    "admin": 0,  # legacy synonym for operator
    "manager": 1,
    "worker": 2,
    "any": 3,
}


def _visibility_for_capability(cap: str) -> str:
    """Map a required capability to its ``tools/list`` visibility tier.

    The bundles in :mod:`agent_mcp.core.capabilities` are the single
    source of truth for which agent role carries which cap:

    * cap in the ``worker`` bundle → visible to worker (and manager /
      admin) → ``"worker"``;
    * cap only in the ``manager`` bundle → ``"manager"``;
    * cap in neither agent bundle (operator / sysadmin-only) →
      ``"operator"``.

    A cap-gated tool is never visible to anonymous callers — they hold
    no capabilities, so the cap gate rejects every one of them.
    """
    # Lazy import: keep module import cheap and avoid any import cycle.
    from ..core.capabilities import AGENT_ROLE_BUNDLES

    if cap in AGENT_ROLE_BUNDLES.get("worker", frozenset()):
        return "worker"
    if cap in AGENT_ROLE_BUNDLES.get("manager", frozenset()):
        return "manager"
    return "operator"


# Default truthiness for each toggle when the project_settings row is
# absent. ADR-0018: DERIVED from the single-source schema registry
# (``core/settings_schema.SETTINGS_SCHEMA``) rather than hand-maintained
# here — every bool setting's default is owned in exactly one place, so
# the tools/list filter and the call-time gate can never drift from it.
# Covers all bool keys (the old hand-list omitted 2, e.g.
# ``config_auto_event_loop_global``).
_TOGGLE_DEFAULTS: Dict[str, bool] = {
    s.key: bool(s.default) for s in SETTINGS_SCHEMA if s.type == "bool"
}


def _derive_access_level(entry) -> str:
    """Compute the access level string for one registry entry.

    Reads — in priority order:

    1. The impl's ``_required_capability`` (from
       ``@requires_capability``) — the LIVE authorization gate. Maps
       to a visibility tier via :func:`_visibility_for_capability`. A
       ``visibility=`` kwarg may only TIGHTEN the derived tier (hide
       from a role the cap admits); it may never loosen it (advertise
       a tool the cap gate rejects — the original tools/list leak).
    2. The impl's ``_required_policy_keys`` (from ``@requires_policy``)
       → renders to ``"worker-if-toggled:<comma-joined-keys>"``.
    3. The registry entry's ``meta.declared_visibility`` (the
       ``visibility=`` kwarg). Post-Phase-2 this is the sole signal for
       ``@requires_predicate``-gated tools (an arbitrary predicate maps
       to no tier) and for ``PUBLIC`` tools — see the module docstring.
       Recognised values: ``"operator"``, ``"manager"``, ``"worker"``,
       ``"any"``, or ``"worker-if-toggled:<keys>"``.
    4. Fallback: ``"any"`` (matches the pre-PR-W1c implicit default).
    """
    impl = entry.meta.implementation
    declared = getattr(entry.meta, "declared_visibility", "any") or "any"

    cap = getattr(impl, "_required_capability", None)
    policy_keys = getattr(impl, "_required_policy_keys", None)

    # 1. Live capability gate (@requires_capability) — the authority.
    #    Derive the tier from the cap; let a kwarg override only when it
    #    is strictly MORE restrictive (a deliberate tighten, e.g.
    #    create_task / bulk_task_operations carry a worker-tier cap but
    #    are kept out of a worker's tools/list). A kwarg that would
    #    LOOSEN the tier is ignored with a loud log — it would advertise
    #    a tool the cap gate rejects.
    if cap is not None:
        cap_tier = _visibility_for_capability(cap)
        override_rank = _LEVEL_RANK.get(declared)
        if override_rank is not None and override_rank < _LEVEL_RANK[cap_tier]:
            return "operator" if declared == "admin" else declared
        if (
            declared not in ("any", "worker", cap_tier)
            and not declared.startswith("worker-if-toggled:")
            and override_rank is None
        ):
            logger.warning(
                "access.py: tool %r declares visibility=%r but its cap %r "
                "implies tier %r; using the cap tier.",
                entry.name,
                declared,
                cap,
                cap_tier,
            )
        return cap_tier

    # 2. Toggle-gated worker tool (@requires_policy) → render the
    #    canonical "worker-if-toggled:<keys>" string from the decorator.
    if policy_keys:
        joined = ",".join(policy_keys)
        return f"worker-if-toggled:{joined}"

    # 3. No decorator gate → the kwarg is the only visibility signal.
    #    Legitimate for tools whose cap check lives IN-BODY (the
    #    derivation can't see an in-body cap) — the `visibility=`
    #    override is authoritative for these.
    if declared in ("operator", "admin"):
        # "admin" is a legacy synonym for "operator" (admin/operator
        # session only). Both hide the tool from worker + manager.
        return "operator" if declared == "admin" else declared
    if declared == "manager":
        return "manager"
    if declared == "worker":
        return "worker"
    if declared == "any":
        return "any"
    if declared.startswith("worker-if-toggled:"):
        return declared

    logger.warning(
        "access.py: registry entry %r has unrecognised declared_visibility "
        "%r; defaulting to 'any'.",
        entry.name,
        declared,
    )
    return "any"


def _build_access_map() -> Dict[str, str]:
    """Derive ``{tool_name: access_level}`` from the live registry.

    Re-evaluated on each access (cheap — the registry is at most a
    few dozen entries; no test or runtime caller loops this).
    """
    # Lazy import to avoid a module-import cycle (`registry` ↔
    # `access` via the `_tool_visibility_policy` callable's lazy
    # `from .access import is_visible_to_role`).
    from .registry import tool_registry

    result: Dict[str, str] = {}
    for name in tool_registry.names():
        entry = tool_registry.get(name)
        if entry is None:  # pragma: no cover — names() and get() agree
            continue
        result[name] = _derive_access_level(entry)
    return result


class _DerivedAccessMap:
    """Read-only dict-like view of the derived access classification.

    Backs the module-level :data:`TOOL_ACCESS` so existing call
    sites keep working without code changes:

    * ``TOOL_ACCESS["create_agent"]`` → ``"admin"``
    * ``TOOL_ACCESS.get("missing")`` → ``None``
    * ``TOOL_ACCESS.keys()`` → derived set of every registered name
    * ``"create_agent" in TOOL_ACCESS`` → ``True``
    * ``for name, level in TOOL_ACCESS.items(): ...``

    Every access re-derives from the registry. That's fine: the
    registry is a few dozen entries, and the only hot path that
    reads this (``is_visible_to_role`` per ``tools/list``) was
    already doing a per-tool dict lookup. The cost difference is
    negligible next to the JSON serialization for ``tools/list``
    itself.

    Also remains callable (``TOOL_ACCESS()``) for tests that want
    a plain-dict snapshot.
    """

    def __getitem__(self, key: str) -> str:
        return _build_access_map()[key]

    def __contains__(self, key: object) -> bool:
        return key in _build_access_map()

    def __iter__(self) -> Iterator[str]:
        return iter(_build_access_map())

    def __len__(self) -> int:
        return len(_build_access_map())

    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        return _build_access_map().get(key, default)

    def keys(self):
        return _build_access_map().keys()

    def values(self):
        return _build_access_map().values()

    def items(self):
        return _build_access_map().items()

    def __call__(self) -> Dict[str, str]:
        """Snapshot the derived map as a plain dict.

        Tests use this when they want to assert on a stable
        dictionary; the live view is fine for runtime callers.
        """
        return _build_access_map()

    def __repr__(self) -> str:  # pragma: no cover — debugging only
        return f"<_DerivedAccessMap entries={len(self)}>"


#: Tool name → access level string. Derived from the live
#: ``tool_registry`` (PR-W1c, 2026-06-05). See module docstring.
TOOL_ACCESS = _DerivedAccessMap()


def _get_config_bool(key: str, default: Optional[bool] = None) -> bool:
    """Read a boolean toggle from the ``project_settings`` store.

    Identical semantics to the per-module helpers in
    ``agent_communication_tools.py`` / ``task_tools.py``; kept here
    so the filter doesn't reach across modules into private helpers.
    If the settings store is unreachable (e.g. during very early
    bootstrap before the DB exists), defaults are returned.

    ADR-0018: ``default`` is optional. When the caller omits it, the
    default is resolved from the single-source schema registry
    (``core/settings_schema.default_for``) — no reader carries its own
    hardcoded default literal.

    Wave 11 (ADR-0016): ``config_*`` rows moved from ``project_context``
    to the dedicated ``project_settings`` table — this is one of the two
    canonical read seams the cutover repointed.
    """
    if default is None:
        default = bool(default_for(key))
    try:
        from ..db.connection import get_db_connection

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT value FROM project_settings WHERE context_key = ?",
            (key,),
        )
        row = cursor.fetchone()
        conn.close()
    except Exception:
        return default
    if not row:
        return default
    raw = row["value"]
    if isinstance(raw, str):
        s = raw.strip().strip('"').lower()
        if s in ("true", "1", "yes", "on"):
            return True
        if s in ("false", "0", "no", "off"):
            return False
    return default


def _get_config_int(key: str, default: Optional[int] = None) -> int:
    """Read an integer knob from the ``project_settings`` store.

    Numeric companion to :func:`_get_config_bool`; this is the single
    canonical config-read seam for int-typed toggles (message
    retention, AoE timeouts, …) so the ``SELECT value FROM
    project_settings`` + coercion isn't re-typed per feature module.

    ``project_settings.value`` is JSON-encoded on write, but tests /
    external tools may push a raw string, so parse liberally: JSON
    first, then ``int()``. Returns ``default`` when the key is absent,
    the store is unreachable (early bootstrap before the DB exists),
    or the value is not coercible to an int. Callers own any further
    policy (``<= 0`` handling, upper clamps).

    ADR-0018: ``default`` is optional — omit it and the default is
    resolved from the single-source schema registry
    (``core/settings_schema.default_for``).

    Wave 11 (ADR-0016): repointed from ``project_context`` alongside
    :func:`_get_config_bool`.
    """
    if default is None:
        default = int(default_for(key))
    try:
        from ..db.connection import get_db_connection

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT value FROM project_settings WHERE context_key = ?",
            (key,),
        )
        row = cursor.fetchone()
        conn.close()
    except Exception:
        return default
    if not row:
        return default
    raw = row["value"]
    # JSON is the canonical write format; fall back to the raw value so
    # a bare string push (tests / external tools) still coerces.
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        parsed = raw
    try:
        return int(parsed)
    except (TypeError, ValueError):
        return default


def is_visible_to_role(tool_name: str, role: str) -> bool:
    """Return True if ``tool_name`` should appear in ``tools/list``
    for the given ``role`` (``"admin"`` | ``"manager"`` | ``"worker"``
    | ``"anonymous"``).

    Phase 2 Wave 2a adds the ``"manager"`` role tier between worker
    and admin. The role-to-level visibility matrix:

    +-------------+-----+---------+---------+----------+-----------------+
    | level\\role | admin | manager | worker | anonymous              |
    +=============+=======+=========+========+========================+
    | operator   |  yes  |  no     |  no    |  no                    |
    | admin (legacy) | yes | no    |  no    |  no                    |
    | manager    |  yes  |  yes    |  no    |  no                    |
    | worker     |  yes  |  yes    |  yes   |  no                    |
    | any        |  yes  |  yes    |  yes   |  yes                   |
    | worker-if-toggled:... | yes | yes  | toggle-dependent | no    |
    +------------+-------+---------+--------+-------------------------+

    The ``"worker"`` level is the derived tier for a tool gated on a
    capability the worker bundle carries (arch-r3 #1+5): every active
    agent can call it, but anonymous callers (no caps) cannot, so it is
    hidden from them — unlike ``"any"``.

    Unknown tool names default to visible — registry callers should
    not silently hide tools the policy file forgot to classify; the
    invariant test catches the omission instead.
    """
    level = TOOL_ACCESS.get(tool_name)
    if level is None:
        # Defensive: see test_every_registered_tool_has_access_classification.
        # We log so the gap is loud in dev/CI but stay permissive at
        # runtime so a forgotten classification doesn't break a worker.
        logger.warning(
            "tools/list filter: tool %r has no access classification; "
            "defaulting to visible. Declare its gate on the impl "
            "(`@requires_capability(...)` / `@requires_policy(...)` / "
            "`@requires_predicate(...)`) and restate it at "
            "register_tool(requires=...); add an explicit `visibility=` "
            "kwarg only if the tool is predicate-gated or PUBLIC.",
            tool_name,
        )
        return True

    if role == "admin":
        # Admin/operator sees everything.
        return True

    if level in ("operator", "admin"):
        # Operator-only tools: hidden from manager + worker +
        # anonymous. Only the "admin" role above sees them.
        return False
    if level == "manager":
        # Manager-tier tools: visible to manager agents (and the
        # admin role handled above). Hidden from workers and
        # anonymous callers.
        return role == "manager"
    if level == "worker":
        # Worker-tier tools: gated on a cap the worker bundle carries,
        # so every active agent can call them. Visible to worker and
        # manager (admin handled above); hidden from anonymous callers
        # (no caps → the gate rejects them).
        return role in ("worker", "manager")
    if level == "any":
        return True
    if level.startswith("worker-if-toggled:"):
        if role == "manager":
            # Managers can see everything a worker can see, plus
            # everything they alone can. The toggle gate is a
            # worker-side constraint that doesn't apply to manager
            # callers.
            return True
        if role != "worker":
            # Anonymous: only "any" tools.
            return False
        keys = [
            k.strip()
            for k in level[len("worker-if-toggled:"):].split(",")
            if k.strip()
        ]
        # Any-truthy semantics: if the worker can do *anything* with
        # the tool under the current toggles, surface it.
        return any(
            _get_config_bool(k, _TOGGLE_DEFAULTS.get(k, False)) for k in keys
        )

    # Unrecognised level string — log and stay visible (same rationale
    # as the unknown-tool default).
    logger.warning(
        "tools/list filter: tool %r has unrecognised access level %r; "
        "defaulting to visible.",
        tool_name,
        level,
    )
    return True
