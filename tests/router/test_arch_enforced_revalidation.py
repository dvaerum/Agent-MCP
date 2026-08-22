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
_COMBINED_HELPERS = {"read_body_and_revalidate", "revalidated_lock"}

# (module dotted path, route-registration function name) pairs to scan.
# Both are the two files OBS-R11-1 names explicitly as the recurring
# class's blast radius; a future third file getting the same shape
# should be added here rather than silently going unchecked.
_TARGET_MODULES = [
    ("agent_mcp.router.admin_api", "register_admin_routes"),
    ("agent_mcp.router.admin_users_api", "register_admin_users_routes"),
]


def _call_func_name(node: ast.AST) -> str | None:
    """Bare callable name for a simple ``Name`` or ``x.attr`` call target."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _find_gate_vars(register_fn: ast.FunctionDef) -> dict[str, str]:
    """``{var_name: capability_string}`` for every
    ``var = require_capability("cap")`` assignment inside the route-
    registration function (e.g. ``project_lifecycle_gate``,
    ``users_gate``). A handler is "capability-gated" iff one of these
    variable names appears anywhere in its route-registration call
    expression."""
    gates: dict[str, str] = {}
    for node in ast.walk(register_fn):
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
    register_fn: ast.FunctionDef,
    gate_vars: dict[str, str],
    handler_names: set[str],
) -> list[tuple[str, str, bool]]:
    """``[(http_method, handler_name, is_capability_gated), ...]`` for
    every ``app.router.add_<method>(path, <handler-expr>)`` call.

    ``<handler-expr>`` is usually a nested call chain — ``gated(cap_gate
    (handler))`` or ``gated(cap_gate(_require_project_operator_membership
    (handler)))`` — so both "which gate variables appear" and "which
    module-level handler function this ultimately wraps" are resolved by
    walking the whole expression subtree for ``Name`` references, not by
    assuming a fixed nesting depth.
    """
    out: list[tuple[str, str, bool]] = []
    for node in ast.walk(register_fn):
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


def _collect_gated_mutation_handlers() -> list[tuple[str, str, ast.AsyncFunctionDef]]:
    """``[(module_name, handler_name, function_node), ...]`` for every
    capability-gated handler registered under a mutation HTTP verb,
    across every module in ``_TARGET_MODULES``."""
    discovered: list[tuple[str, str, ast.AsyncFunctionDef]] = []
    for module_name, register_fn_name in _TARGET_MODULES:
        mod = importlib.import_module(module_name)
        tree = ast.parse(Path(mod.__file__).read_text())
        handler_defs = {
            node.name: node
            for node in tree.body
            if isinstance(node, ast.AsyncFunctionDef)
        }
        register_fn = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == register_fn_name
        )
        gate_vars = _find_gate_vars(register_fn)
        for method, handler_name, gated in _route_registrations(
            register_fn, gate_vars, set(handler_defs),
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
