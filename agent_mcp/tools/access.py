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
Adding a new admin-only tool required: (1) the ``@requires("admin")``
decorator on the impl, (2) a matching entry in ``TOOL_ACCESS``. The
two had to stay in sync; the invariant test
``test_every_registered_tool_has_access_classification`` caught
*omissions* but not *contradictions* (a tool decorated admin but
classified "any" in the table would leak into worker tools/list).

PR-W1c flipped this to a double source of truth, both at the tool
declaration site:

1. ``@requires_role("admin")`` (or the equivalent ``@requires("admin")``
   from :mod:`agent_mcp.core.authorize`) on the impl — enforces the
   auth check at the call site and exposes
   ``func._required_role = "admin"`` for introspection.
2. ``visibility="admin"`` kwarg on ``register_tool()`` — surfaces
   the same fact as registry metadata.

This module now *derives* :data:`TOOL_ACCESS` from the live
:data:`agent_mcp.tools.registry.tool_registry`. The derived value
reads, per registered entry:

* the impl's ``_required_role`` attribute (set by ``@requires``
  / ``@requires_role``);
* the impl's ``_required_policy_keys`` + ``_required_policy_default``
  attributes (set by ``@requires_policy``);
* the registry entry's ``meta.declared_visibility`` (the kwarg).

When the decorator and kwarg disagree, the decorator wins — the
call-site enforcement is the real authority; the kwarg merely
surfaces it for ``tools/list``. The invariant test passes because
every registered tool has *some* derived classification (the
fallback is ``"any"``, matching ``is_visible_to_role``'s historical
default).
"""
from __future__ import annotations

from typing import Dict, Iterator, Optional

from ..core.config import logger


# Default truthiness for each toggle when the project_context row is
# absent. Mirrors what each tool's own impl passes to
# `_get_config_bool(..., default=...)` (and what `@requires_policy`'s
# `default=` kwarg sets), so the tools/list filter and the call-time
# gate agree on "is this on?" without crossing module boundaries.
_TOGGLE_DEFAULTS: Dict[str, bool] = {
    # Default-deny worker→worker (PR #16).
    "config_allow_worker_to_worker": False,
    # Default-allow worker self-assign / self-file / own-status updates.
    "config_allow_worker_self_assign": True,
    "config_allow_worker_create_unassigned": True,
    "config_allow_worker_update_own_status": True,
}


def _derive_access_level(entry) -> str:
    """Compute the access level string for one registry entry.

    Reads — in priority order:

    1. The impl's ``_required_role`` attribute (from ``@requires`` /
       ``@requires_role``). ``"admin"`` wins; ``"any"`` is a
       valid-token gate that still maps to ``"any"`` for tools/list
       (every active agent — worker or admin — can call it).
    2. The impl's ``_required_policy_keys`` (from ``@requires_policy``)
       → renders to ``"worker-if-toggled:<comma-joined-keys>"``.
    3. The registry entry's ``meta.declared_visibility`` (the
       ``visibility=`` kwarg). Used when no decorator was found, or
       when the decorator says ``"any"`` but the kwarg restricts
       further (rare; the kwarg can specify ``"admin"`` even without
       a matching decorator, in which case tools/list hides it but
       call-time enforcement would slip through — Test C in
       ``test_tool_access_kwarg_and_decorator.py``).
    4. Fallback: ``"any"`` (matches the pre-PR-W1c implicit default).

    Decorator wins on disagreement (admin in decorator + ``"any"``
    in kwarg → ``"admin"``). This pins the most-secure
    interpretation: if the call site rejects workers, the
    visibility filter must hide the tool from workers too.
    """
    impl = entry.meta.implementation
    declared = getattr(entry.meta, "declared_visibility", "any") or "any"

    role = getattr(impl, "_required_role", None)
    policy_keys = getattr(impl, "_required_policy_keys", None)

    # Decorator says admin → the call site rejects workers → hide.
    # (Overrides any softer kwarg.)
    if role == "admin":
        return "admin"

    # Decorator says worker-if-toggled → render the canonical string
    # from the decorator's keys. A kwarg-declared
    # "worker-if-toggled:..." string SHOULD match the decorator's
    # keys; we prefer the decorator (canonical) without comparing —
    # a mismatch would be developer error caught by review.
    if policy_keys:
        joined = ",".join(policy_keys)
        return f"worker-if-toggled:{joined}"

    # No decorator (or `@requires("any")`) → fall back to the kwarg.
    # Recognised values: "admin", "any", or
    # "worker-if-toggled:<keys>". Unknown values default to "any"
    # with a loud log.
    if declared == "admin":
        # `visibility="admin"` kwarg without the decorator → hide
        # from worker tools/list, but call-time enforcement would
        # slip through. The `@requires_role` decorator is the right
        # fix; this is the "kwarg-only" path from PR-W1c Test C.
        return "admin"
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


def _get_config_bool(key: str, default: bool) -> bool:
    """Read a boolean toggle from project_context.

    Identical semantics to the per-module helpers in
    ``agent_communication_tools.py`` / ``task_tools.py``; kept here
    so the filter doesn't reach across modules into private helpers.
    If the project_context store is unreachable (e.g. during very
    early bootstrap before the DB exists), defaults are returned.
    """
    try:
        from ..db.connection import get_db_connection

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT value FROM project_context WHERE context_key = ?",
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


def is_visible_to_role(tool_name: str, role: str) -> bool:
    """Return True if ``tool_name`` should appear in ``tools/list``
    for the given ``role`` (``"admin"`` | ``"worker"`` |
    ``"anonymous"``).

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
            "defaulting to visible. Add a `visibility=` kwarg to its "
            "register_tool() call (and `@requires_role(...)` if the "
            "policy is more restrictive than 'any').",
            tool_name,
        )
        return True

    if role == "admin":
        return True

    if level == "admin":
        return False
    if level == "any":
        return True
    if level.startswith("worker-if-toggled:"):
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
