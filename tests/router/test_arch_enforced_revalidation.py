"""OBS-R11-1 architecture invariant: no bare yield point ahead of
revalidation on a capability-gated MUTATION handler.

Seven separate pentest rounds (R6-F2 -> R7-F1 -> R7-F3 -> R8-F3 ->
R9-F2 -> R9-F3 -> R9-F4) rediscovered the SAME shape: a handler
snapshots the caller's Principal at request entry, does a genuine
``await`` (a body-read, a lock-acquire, a downstream call), and then
trusts the stale pre-await snapshot for the privileged write that
follows. ``perm_gates.revalidate_capability_or_403`` /
``admin_api._revalidate_capability_and_membership_or_403`` already
re-derive every axis correctly (capability, membership, role-tier,
session-liveness) — the recurrence was never about the CHECK being
wrong, it was that calling it was OPT-IN: a handler author had to
remember to call it, with the right arguments, immediately after the
right await, and not before. ``perm_gates.read_body_and_revalidate`` /
``perm_gates.revalidated_lock`` (added for this refactor) fuse the
await and the re-check into ONE call so there's no second step to
forget.

This test is the backstop for that fusion staying universal: it
statically discovers every handler in ``admin_api.py`` /
``admin_users_api.py`` that is (a) capability-gated via
``require_capability`` and (b) registered under a mutation HTTP verb
(POST/PUT/PATCH/DELETE — a GET route has nothing to write, so the
recurring class structurally can't apply to it; this mirrors the
round-12 completeness-critic's scoping refinement that a handler with
NO genuine yield point between check-and-act, like
``agent_profile_tools.py``'s synchronous writer, is immune BY
CONSTRUCTION and out of scope). For each discovered handler it either:

  * asserts there is no ``await`` / ``async with`` in the function body
    at all (immune by construction — e.g. ``delete_user_handler``,
    whose entire body is synchronous sqlite3 calls), or
  * asserts the FIRST yield point in the function IS a call to one of
    the combined helpers (``read_body_and_revalidate`` /
    ``revalidated_lock``) — i.e. nothing awaits ahead of the re-check.

RED/GREEN self-verification (per OBS-R11-1's TDD mandate): reverting
any ONE of the Part-1 migrations locally (re-inlining the old
"read/lock, THEN separately call the revalidator" two-step) makes the
affected parametrized case in this file fail, because the manually
re-introduced raw ``await``/``async with`` now precedes the (now
absent, or now-later) combined-helper call. Re-applying the migration
restores the pass. This was verified by hand while writing this test
(see the PR description for the exact revert/confirm-fail/reapply/
confirm-pass transcript) — it is not re-run automatically here because
doing so would require mutating source files at test time, which is
worse than the one-time manual verification for a static-analysis
test like this one.

R13-F1 (HIGH, live-exploited): the check above only ever verified the
START of a handler's yield-point chain — that nothing awaits AHEAD of
the FIRST combined-helper call. It said nothing about the END of that
chain: ``rename_project_handler`` had a SECOND, later, independent
yield point (``_ensure_lock`` acquisition, up to a full backend
cold-boot's worth of lock CONTENTION) that this test would happily
pass right through, because its first yield point (the body-read) WAS
correctly wired to ``read_body_and_revalidate`` — the bare
``async with _app._ensure_lock(...):`` that followed, unwrapped, was
invisible to a check that only ever looked at index 0.
``test_no_bare_yield_point_after_last_revalidation`` below closes that:
for every discovered handler, it walks the AST for any bare (non-
marker) ``await``/``async with`` occurring, in source order, with no
combined-helper marker call anywhere AFTER it.

R14-F2 (HIGH, live-exploited): the FIRST version of this check
(shipped for R13-F1) excluded yield points lexically nested INSIDE the
last marker's own ``async with`` body outright, reasoning that they ran
"under the still-held lock the marker just freshly revalidated inside
... that's the protected region the marker exists to guard, not a
gap." That reasoning was unsound: a held ``asyncio.Lock`` only blocks
OTHER coroutines racing for the SAME lock; it does nothing to stop an
unrelated capability/membership DELETE from committing to the DB while
THIS coroutine is suspended mid-``await`` inside the "protected" block.
``rename_project_handler`` / ``delete_project_handler`` /
``stop_project_handler`` each held a bare ``asyncio.to_thread``
systemctl-stop (or, for stop, ``_is_active`` too) await immediately
inside their ``revalidated_lock`` block, with the destructive write
straight after — completely unrevalidated. The check below no longer
carves out an exemption for anything nested inside a marker's body: a
bare yield point is "covered" only when *some* marker call — including
``perm_gates.revalidate_after``, the new helper this fix introduced —
occurs anywhere LATER in the function, at any nesting depth. A handler
with a bare yield point and no marker after it anywhere is exactly the
R14-F2 shape.

Finding G (security-authz-architecture-hardening.md, Phase 0): this
file itself used to hardcode the 2-module allowlist below as
``_TARGET_MODULES = [("agent_mcp.router.admin_api", ...), (...
admin_users_api", ...)]``, with a comment admitting "a future third
file getting the same shape should be added here rather than silently
going unchecked" — exactly the opt-in-and-forget shape this whole test
exists to catch, just recurring in the test's own plumbing. The module
list is now discovered dynamically by walking ``agent_mcp/router/``
and AST-parsing each file for a real
``from .perm_gates import require_capability`` statement (module-level
or lazy, both occur in this codebase) — the same "does this module use
the pattern" question a human reviewer would ask, answered
mechanically instead of by someone remembering to update a list. This
immediately discovered a third module, ``admin_sso_api.py``
(``register_admin_sso_routes``), which the old hardcoded list silently
never covered — see ``test_dynamic_discovery_is_superset_of_history``
below, which pins that the new mechanism can never regress to covering
FEWER modules than the list it replaces.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

# Async tests in this repo's router suite opt in individually with
# ``@pytest.mark.asyncio``; every function below is a plain sync AST
# walk and must NOT inherit an asyncio mark.

_MUTATION_METHODS = {"post", "put", "patch", "delete"}
_COMBINED_HELPERS = {
    "read_body_and_revalidate", "revalidated_lock", "revalidate_after",
}

_ROUTER_PACKAGE = "agent_mcp.router"

# Module stems that can never be a route-registration module using this
# pattern: the package/entry-point shims, and ``perm_gates`` itself
# (which DEFINES ``require_capability``, so it always fails the "does
# this module IMPORT it" test below anyway — excluded explicitly so a
# stray self-referential import inside perm_gates.py's own tests-style
# code could never make it discover itself).
_EXCLUDED_MODULE_STEMS = {"__init__", "__main__", "perm_gates"}

#: The 2-module allowlist this file hardcoded before Finding G's
#: dynamic discovery replaced it. Kept only so
#: ``test_dynamic_discovery_is_superset_of_history`` can assert the
#: new mechanism never silently regresses to covering FEWER modules
#: than the hand-maintained list it replaces.
_HISTORICAL_TARGET_MODULES = [
    "agent_mcp.router.admin_api",
    "agent_mcp.router.admin_users_api",
]


def _router_package_dir() -> Path:
    """Filesystem directory backing ``agent_mcp.router``, resolved via
    the package's own ``__file__`` (not a path relative to this test
    file) so discovery is correct regardless of where pytest runs
    from."""
    import agent_mcp.router as _pkg

    return Path(_pkg.__file__).parent


def _imports_require_capability(tree: ast.Module) -> bool:
    """True iff ``tree`` contains a real
    ``from .perm_gates import ... require_capability`` statement —
    module-level or lazily inside a function (both idioms are common
    in this codebase's route-registration handlers) — as opposed to a
    mere textual mention of the name in a comment or docstring (e.g.
    ``single_tenant.py``, which references ``perm_gates.require_capability``
    in prose but never imports or calls it, and must NOT be discovered
    as a target module on that basis)."""
    return any(
        isinstance(node, ast.ImportFrom)
        and node.module == "perm_gates"
        and any(alias.name == "require_capability" for alias in node.names)
        for node in ast.walk(tree)
    )


def _discover_target_modules() -> list[str]:
    """Every ``agent_mcp.router`` module that actually imports
    ``perm_gates.require_capability``, found by walking the package
    directory and AST-parsing each file — replaces the hand-maintained
    ``_HISTORICAL_TARGET_MODULES`` allowlist (Finding G)."""
    discovered = []
    for path in sorted(_router_package_dir().glob("*.py")):
        if path.stem in _EXCLUDED_MODULE_STEMS:
            continue
        tree = ast.parse(path.read_text())
        if _imports_require_capability(tree):
            discovered.append(f"{_ROUTER_PACKAGE}.{path.stem}")
    return discovered


# Dotted module paths to scan. Previously a hardcoded 2-entry allowlist
# (see ``_HISTORICAL_TARGET_MODULES``); now discovered dynamically —
# see the module docstring's "Finding G" section.
_TARGET_MODULES = _discover_target_modules()


def test_dynamic_discovery_is_superset_of_history() -> None:
    """Finding G's own regression guard: the dynamically-discovered
    module set must never cover FEWER modules than the hardcoded list
    it replaced — that would be a silent coverage regression in the
    detector itself, worse than not changing it at all.

    Also logs (via the assertion message / a print, visible with
    ``pytest -s`` or on failure) exactly which modules the dynamic
    discovery found beyond the historical list, so a reviewer can see
    the mechanism is doing real work rather than degenerating back to
    the same 2 entries."""
    discovered = set(_TARGET_MODULES)
    historical = set(_HISTORICAL_TARGET_MODULES)
    missing = historical - discovered
    assert not missing, (
        f"dynamic discovery via _discover_target_modules() no longer "
        f"finds {missing!r}, which the old hardcoded "
        f"_HISTORICAL_TARGET_MODULES list covered — this is a coverage "
        f"regression in the detector itself."
    )
    added = discovered - historical
    print(
        f"arch-enforced-revalidation dynamic discovery: "
        f"{len(discovered)} module(s) found, "
        f"{len(added)} beyond the historical 2-module allowlist: "
        f"{sorted(added)!r}"
    )


def _call_func_name(node: ast.AST) -> str | None:
    """Bare callable name for a simple ``Name`` or ``x.attr`` call target."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _find_gate_vars(module_tree: ast.Module) -> dict[str, str]:
    """``{var_name: capability_string}`` for every
    ``var = require_capability("cap")`` assignment anywhere in
    ``module_tree`` (e.g. ``project_lifecycle_gate``, ``users_gate``,
    ``sso_gate``) — module-wide rather than scoped to one named
    route-registration function, so this works regardless of how many
    registration functions a module defines or what they're called. A
    handler is "capability-gated" iff one of these variable names
    appears anywhere in its route-registration call expression."""
    gates: dict[str, str] = {}
    for node in ast.walk(module_tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Call)
            and _call_func_name(node.value.func) == "require_capability"
            and node.value.args
            and isinstance(node.value.args[0], ast.Constant)
        ):
            gates[node.targets[0].id] = node.value.args[0].value
    return gates


def _route_registrations(
    module_tree: ast.Module,
    gate_vars: dict[str, str],
    handler_names: set[str],
) -> list[tuple[str, str, bool]]:
    """``[(http_method, handler_name, is_capability_gated), ...]`` for
    every ``app.router.add_<method>(path, <handler-expr>)`` call
    anywhere in ``module_tree`` — module-wide rather than scoped to one
    named route-registration function (see ``_find_gate_vars``).

    ``<handler-expr>`` is usually a nested call chain — ``gated(cap_gate
    (handler))`` or ``gated(cap_gate(_require_project_operator_membership
    (handler)))`` — so both "which gate variables appear" and "which
    module-level handler function this ultimately wraps" are resolved by
    walking the whole expression subtree for ``Name`` references, not by
    assuming a fixed nesting depth.
    """
    out: list[tuple[str, str, bool]] = []
    for node in ast.walk(module_tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr.startswith("add_")
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "router"
        ):
            continue
        if len(node.args) < 2:
            continue
        method = node.func.attr[len("add_"):]
        handler_expr = node.args[1]
        names_in_expr = {
            n.id for n in ast.walk(handler_expr) if isinstance(n, ast.Name)
        }
        handler_matches = names_in_expr & handler_names
        if len(handler_matches) != 1:
            # Not a recognisable "wraps exactly one module-level handler"
            # shape (e.g. a lambda) — nothing for this test to check.
            continue
        gated = bool(names_in_expr & gate_vars.keys())
        out.append((method, next(iter(handler_matches)), gated))
    return out


def _is_combined_helper_call(call: ast.Call) -> bool:
    return _call_func_name(call.func) in _COMBINED_HELPERS


def _yield_points_and_markers(
    func: ast.AsyncFunctionDef,
) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """Source-order positions of every yield point in ``func``, and the
    subset of those that ARE a combined-helper call.

    A "yield point" is any ``await <expr>`` or ``async with <expr>:`` —
    both are genuine places where a concurrent request can run and
    mutate live state before this coroutine resumes. A yield point is a
    "marker" when the awaited/with expression is a call to
    ``read_body_and_revalidate`` or ``revalidated_lock`` — the ONLY
    combined helpers from ``perm_gates.py`` this refactor introduced.
    ``ast.walk`` naturally descends into an ``async with revalidated_lock
    (...):`` body too, so awaits INSIDE that block are correctly counted
    as later yield points (they run after the marker, under the lock —
    not a violation), not confused with the marker itself.
    """
    yield_points: list[tuple[int, int]] = []
    markers: list[tuple[int, int]] = []
    for node in ast.walk(func):
        if isinstance(node, ast.Await):
            pos = (node.lineno, node.col_offset)
            yield_points.append(pos)
            if isinstance(node.value, ast.Call) and _is_combined_helper_call(
                node.value,
            ):
                markers.append(pos)
        elif isinstance(node, ast.AsyncWith):
            pos = (node.lineno, node.col_offset)
            yield_points.append(pos)
            for item in node.items:
                ctx = item.context_expr
                if isinstance(ctx, ast.Call) and _is_combined_helper_call(ctx):
                    markers.append(pos)
                    break
    yield_points.sort()
    markers.sort()
    return yield_points, markers


def _unprotected_yield_points_after_last_marker(
    func: ast.AsyncFunctionDef,
) -> list[tuple[int, int]]:
    """R13-F1 / R14-F2: positions of every bare (non-marker)
    ``await``/``async with`` in ``func`` that has NO combined-helper
    marker call anywhere AFTER it in source order — at ANY nesting
    depth, including yield points lexically NESTED inside another
    marker's own ``async with`` body.

    R14-F2 (HIGH, live-exploited) found that the ORIGINAL version of
    this check (written for R13-F1) exempted everything nested inside
    the LAST marker's own body outright, reasoning it ran "under the
    still-held lock the marker just freshly revalidated inside" —
    unsound: a held ``asyncio.Lock`` blocks only OTHER coroutines
    racing for the SAME lock, not a concurrent, unrelated capability/
    membership DELETE committing to the DB while THIS coroutine is
    suspended mid-await still inside that block.
    ``rename_project_handler`` / ``delete_project_handler`` /
    ``stop_project_handler`` all held a bare ``asyncio.to_thread``
    systemctl-stop (or ``_is_active``) await INSIDE their
    ``revalidated_lock`` block, straight after its one-shot entry
    revalidation, with the destructive write immediately after — the
    exact shape this now catches. The fix routes that inner await
    through ``perm_gates.revalidate_after`` (a NEW combined-helper
    marker fusing the await with a fresh re-check the instant it
    resolves — mirrors ``read_body_and_revalidate``'s existing fusion
    idiom, see its docstring in ``perm_gates.py``).

    A bare yield point is "covered" whenever *some* marker call occurs
    anywhere LATER in the function, at any nesting depth — not "the
    yield point must itself be a marker". This is intentionally a
    little more permissive than requiring every single yield point to
    be a marker: an earlier read-only probe (e.g. ``stop``'s
    ``_is_active`` check, ALSO wrapped in ``revalidate_after`` by this
    fix, but hypothetically even if it weren't) can stay bare as long
    as a later marker fires before the actual write, since that later
    revalidation is what protects the write regardless of whether the
    earlier probe's own result was already stale.

    Returns an empty list for a handler with no markers at all — that
    shape is covered by ``test_no_bare_yield_point_before_revalidation``
    above instead (either immune-by-construction, or already flagged
    there for having a yield point with NO revalidation whatsoever).
    """
    yield_points, markers = _yield_points_and_markers(func)
    if not markers:
        return []
    marker_set = set(markers)
    hits = [
        pos for pos in yield_points
        if pos not in marker_set and not any(m > pos for m in markers)
    ]
    hits.sort()
    return hits


def _parse_async_func(src: str) -> ast.AsyncFunctionDef:
    """Parse a standalone async-function source snippet for the
    synthetic-shape unit tests below — these exercise the AST checker
    directly, independent of any real handler in the codebase."""
    tree = ast.parse(src)
    fn = tree.body[0]
    assert isinstance(fn, ast.AsyncFunctionDef)
    return fn


def test_unprotected_yield_points_catches_bare_await_nested_in_revalidated_lock() -> None:
    """R14-F2 synthetic case: a bare ``await`` INSIDE a
    ``revalidated_lock`` block, with the destructive write immediately
    after and NOTHING revalidating in between, must be flagged — this
    is exactly the shape ``rename``/``delete``/``stop`` had pre-fix,
    and exactly what the ORIGINAL (R13-F1-only) version of this checker
    blanket-exempted as "protected by the still-held lock"."""
    src = (
        "async def handler(req):\n"
        "    async with revalidated_lock(req, 'cap', name) as denied:\n"
        "        if denied is not None:\n"
        "            return denied\n"
        "        await asyncio.to_thread(systemctl, 'stop', unit)\n"
        "        registry.destroy(name)\n"
    )
    hits = _unprotected_yield_points_after_last_marker(_parse_async_func(src))
    assert hits, "expected the bare in-lock await to be flagged"


def test_unprotected_yield_points_clears_when_wrapped_in_revalidate_after() -> None:
    """Same synthetic shape, fixed: the in-lock await is routed through
    ``revalidate_after`` — no violation, proving the checker doesn't
    just blanket-flag everything inside a ``revalidated_lock`` block
    either."""
    src = (
        "async def handler(req):\n"
        "    async with revalidated_lock(req, 'cap', name) as denied:\n"
        "        if denied is not None:\n"
        "            return denied\n"
        "        _, denied = await revalidate_after(\n"
        "            asyncio.to_thread(systemctl, 'stop', unit),\n"
        "            req, 'cap', name,\n"
        "        )\n"
        "        if denied is not None:\n"
        "            return denied\n"
        "        registry.destroy(name)\n"
    )
    hits = _unprotected_yield_points_after_last_marker(_parse_async_func(src))
    assert not hits, f"unexpected hits: {hits}"


def test_unprotected_yield_points_no_false_positive_on_single_marker_handler() -> None:
    """Legitimate protected code, no regression: a handler whose ONLY
    yield point is the combined-helper marker itself (no further
    awaits at all — the ``create_project_handler`` shape) must not be
    flagged."""
    src = (
        "async def handler(req):\n"
        "    body, denied = await read_body_and_revalidate(req, parse, 'cap')\n"
        "    if denied is not None:\n"
        "        return denied\n"
        "    do_write(body)\n"
    )
    hits = _unprotected_yield_points_after_last_marker(_parse_async_func(src))
    assert not hits, f"unexpected hits: {hits}"


def test_unprotected_yield_points_still_catches_r13f1_second_lock_shape() -> None:
    """Non-regression on the ORIGINAL R13-F1 shape: a whole SECOND,
    independent bare ``async with`` lock acquisition after the first
    marker, with no revalidation ever following it, must still be
    flagged exactly as before this file's R14-F2 rewrite."""
    src = (
        "async def handler(req):\n"
        "    body, denied = await read_body_and_revalidate(req, parse, 'cap')\n"
        "    if denied is not None:\n"
        "        return denied\n"
        "    async with _app._ensure_lock(name, 'backend'):\n"
        "        registry.destroy(name)\n"
    )
    hits = _unprotected_yield_points_after_last_marker(_parse_async_func(src))
    assert hits, "expected the bare second-lock async-with to be flagged"


def _collect_gated_mutation_handlers() -> list[tuple[str, str, ast.AsyncFunctionDef]]:
    """``[(module_name, handler_name, function_node), ...]`` for every
    capability-gated handler registered under a mutation HTTP verb,
    across every module in ``_TARGET_MODULES``.

    Scans each module's WHOLE tree (not a single named
    route-registration function) for gate-variable assignments and
    route-registration calls — see ``_find_gate_vars`` /
    ``_route_registrations`` — so this works uniformly across modules
    with different route-registration function names (e.g.
    ``register_admin_routes`` vs. ``register_admin_sso_routes``)
    without this collector needing to know any of those names."""
    discovered: list[tuple[str, str, ast.AsyncFunctionDef]] = []
    for module_name in _TARGET_MODULES:
        mod = importlib.import_module(module_name)
        tree = ast.parse(Path(mod.__file__).read_text())
        handler_defs = {
            node.name: node
            for node in tree.body
            if isinstance(node, ast.AsyncFunctionDef)
        }
        gate_vars = _find_gate_vars(tree)
        for method, handler_name, gated in _route_registrations(
            tree, gate_vars, set(handler_defs),
        ):
            if gated and method in _MUTATION_METHODS:
                discovered.append(
                    (module_name, handler_name, handler_defs[handler_name]),
                )
    return discovered


_DISCOVERED = _collect_gated_mutation_handlers()
_DISCOVERED_IDS = [f"{mod}:{name}" for mod, name, _ in _DISCOVERED]


def test_discovery_found_the_known_call_sites() -> None:
    """Sanity guard on the detector itself: if a future change to the
    route-registration syntax (new wrapper shape, different AST pattern)
    silently makes ``_collect_gated_mutation_handlers`` find nothing,
    every parametrized case below would vacuously "pass" by not
    existing — worse than not having this test at all. Pin the known
    Part-1 migration set (OBS-R11-1's ~5 admin_api.py + ~12
    admin_users_api.py capability-gated mutation handlers) so a
    detection regression fails loudly here instead.
    """
    assert len(_DISCOVERED) >= 16, (
        f"expected at least 16 capability-gated mutation handlers across "
        f"admin_api.py + admin_users_api.py, found {len(_DISCOVERED)}: "
        f"{_DISCOVERED_IDS!r} — the AST route-registration detector may "
        f"have silently stopped matching (e.g. a route-wiring syntax "
        f"change), which would make every other test in this file "
        f"vacuously pass without checking anything."
    )
    names = {name for _, name, _ in _DISCOVERED}
    for expected in (
        "create_project_handler",
        "rename_project_handler",
        "delete_project_handler",
        "stop_project_handler",
        "create_user_handler",
        "edit_user_handler",
        "add_project_membership_handler",
        "change_project_membership_role_handler",
        "replace_group_capabilities_handler",
    ):
        assert expected in names, (
            f"{expected!r} was not discovered as a capability-gated "
            f"mutation handler — check the route registration still "
            f"wires it through a ``require_capability(...)`` gate "
            f"variable under a POST/PUT/PATCH/DELETE verb."
        )


@pytest.mark.parametrize(
    "module_name, handler_name, func", _DISCOVERED, ids=_DISCOVERED_IDS,
)
def test_no_bare_yield_point_before_revalidation(
    module_name: str, handler_name: str, func: ast.AsyncFunctionDef,
) -> None:
    yield_points, markers = _yield_points_and_markers(func)

    if not yield_points:
        # Immune by construction (OBS-R11-1's scoping refinement): no
        # genuine async yield point exists between this handler's entry
        # check and its write, so there is nothing a concurrent request
        # could interleave into. Applying either combined helper here
        # would be a no-op, exactly like ``agent_profile_tools.py``'s
        # handler in the round-12 counter-example.
        return

    assert markers, (
        f"{module_name}.{handler_name} has a genuine async yield point "
        f"(await/async-with) but never calls "
        f"perm_gates.read_body_and_revalidate / revalidated_lock. This "
        f"is the OBS-R11-1 recurring stale-Principal TOCTOU shape "
        f"(R6-F2/R7-F1/R7-F3/R8-F3/R9-F2/R9-F3/R9-F4): a capability/"
        f"membership check at request entry, a genuine await, then a "
        f"privileged write trusting the stale pre-await snapshot. Route "
        f"this handler's yield point through one of the combined "
        f"helpers in perm_gates.py instead of awaiting it bare."
    )
    first_yield = yield_points[0]
    first_marker = markers[0]
    assert first_yield == first_marker, (
        f"{module_name}.{handler_name} awaits/holds something at line "
        f"{first_yield[0]} BEFORE its first call to the combined "
        f"revalidation helper (line {first_marker[0]}) — the exact "
        f"OBS-R11-1 shape: a bare yield point ahead of the re-check "
        f"gives a concurrent privilege revocation a window to land "
        f"before the (stale) re-check runs. Make "
        f"read_body_and_revalidate / revalidated_lock the FIRST yield "
        f"point in this handler."
    )


@pytest.mark.parametrize(
    "module_name, handler_name, func", _DISCOVERED, ids=_DISCOVERED_IDS,
)
def test_no_bare_yield_point_after_last_revalidation(
    module_name: str, handler_name: str, func: ast.AsyncFunctionDef,
) -> None:
    """R13-F1 / R14-F2: nothing may await/hold bare with no combined-
    helper marker call anywhere AFTER it, at ANY nesting depth —
    complementing ``test_no_bare_yield_point_before_revalidation``'s
    check of the START of the yield-point chain. A handler with no
    marker at all is already flagged (or exempted) by that other test;
    this one only fires for a handler that revalidates SOMEWHERE but
    still leaves a bare yield point with nothing revalidating after it
    — whether that's a whole SECOND yield point outside the first
    marker's block (R13-F1: a separate ``_ensure_lock`` acquisition) or
    an in-lock await lexically NESTED inside a ``revalidated_lock``
    block with no ``revalidate_after`` following it (R14-F2)."""
    hits = _unprotected_yield_points_after_last_marker(func)
    assert not hits, (
        f"{module_name}.{handler_name} has a bare async yield point at "
        f"line(s) {[h[0] for h in hits]} with no combined revalidation "
        f"helper call (read_body_and_revalidate / revalidated_lock / "
        f"revalidate_after) anywhere AFTER it — the R13-F1/R14-F2 "
        f"shape: a yield point (a separate ``_ensure_lock`` acquisition, "
        f"lock CONTENTION worth a full backend cold-boot, or a bare "
        f"``asyncio.to_thread`` call INSIDE an already-held "
        f"``revalidated_lock`` block) that no revalidation call ever "
        f"runs after. A concurrent capability/membership revocation can "
        f"land in that window and the destructive write downstream "
        f"would still run on stale authority. Wrap this yield point in "
        f"perm_gates.revalidated_lock (for a fresh lock acquisition) or "
        f"perm_gates.revalidate_after (for an await already inside a "
        f"held revalidated_lock block) so a fresh revalidation always "
        f"follows it before anything downstream runs un-rechecked."
    )
