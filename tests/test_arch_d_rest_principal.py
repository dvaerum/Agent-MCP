"""Finding D (Phase 5) — the REST door's identity travels on the value,
never on a module-level side-channel.

Background
----------
``app/deps.require_operator_session`` used to return an untyped
three-shape dict. One field genuinely did not fit — the forwarding
caller's HMAC-signed ``(project_role, sysadmin)`` — because, in the
comment's own words, "the dispatch helper has no Request/auth-dict
handle, and the dict's shape is contract-pinned elsewhere". So it
travelled out of band on ``deps._forwarding_route_role``, a module-level
``contextvars.ContextVar``, read back by
``_dispatch_helpers._build_route_principal``.

That carrier was task-local and therefore *correct*, but it was the same
structural shape as the bug ``test_sec_r4_operator_identity_race.py``
exists to prevent (identity reconstructed from module state rather than
read off the request's own value), and it was load-bearing for a
security property: AC-R5-1, a forwarding VIEWER must get a viewer-role
Principal the tool's capability gate denies, not the full operator
bundle. A future edit that dropped the ``.set()`` would have silently
re-opened that escalation with every test still green, because the
consumer's fallback is ``("operator", False)``.

Finding D made ``project_role``/``sysadmin`` ordinary fields on
:class:`agent_mcp.app.rest_principal.RestPrincipal` and threaded the
value itself into the dispatch helper. This module keeps that closed:

* the identifiers are gone from the whole package, checked by AST rather
  than by a comment asking nicely;
* the detector that proves it is itself proven, against a synthetic
  module that DOES read the carrier — so the rule cannot decay into one
  that detects nothing (same idiom as
  ``test_arch_enforced_stream_revalidation.py``);
* ``deps.py`` declares no ``ContextVar`` at all, so the next field that
  "doesn't fit" has to go on the value too.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

import agent_mcp
from agent_mcp.app.rest_principal import RestPrincipal

#: The retired side-channel. Both the private carrier and its public
#: reader — either name reappearing means the value stopped being the
#: single carrier of the forwarding caller's role.
_RETIRED_NAMES = frozenset({"_forwarding_route_role", "forwarding_route_role"})

_PACKAGE_ROOT = pathlib.Path(agent_mcp.__file__).resolve().parent


def _references(source: str, filename: str) -> list[str]:
    """Return ``file:line`` for every reference to a retired name.

    Catches the bare name (``_forwarding_route_role.get()``), the
    attribute form (``deps.forwarding_route_role()``), and the import
    (``from .deps import forwarding_route_role``) — the three ways the
    carrier was ever reached.
    """
    hits: list[str] = []
    tree = ast.parse(source, filename=filename)
    for node in ast.walk(tree):
        name = None
        if isinstance(node, ast.Name):
            name = node.id
        elif isinstance(node, ast.Attribute):
            name = node.attr
        elif isinstance(node, ast.alias):
            name = node.asname or node.name
        if name in _RETIRED_NAMES:
            hits.append(f"{filename}:{getattr(node, 'lineno', '?')}")
    return hits


def _package_modules() -> list[pathlib.Path]:
    return sorted(_PACKAGE_ROOT.rglob("*.py"))


# ── the invariant ─────────────────────────────────────────────────


def test_forwarding_route_role_contextvar_is_read_nowhere() -> None:
    """No module in ``agent_mcp`` references the retired carrier.

    RED before Finding D step 2: ``app/deps.py`` declared and set it and
    ``app/_dispatch_helpers.py`` read it.
    """
    offenders: list[str] = []
    for path in _package_modules():
        offenders.extend(
            _references(path.read_text(encoding="utf-8"),
                        str(path.relative_to(_PACKAGE_ROOT.parent)))
        )

    assert offenders == [], (
        "the forwarding-role ContextVar side-channel is back: "
        + ", ".join(offenders)
        + ". The forwarding caller's signed (project_role, sysadmin) "
        "belongs on RestPrincipal.route_role(), which the dispatch seam "
        "reads off the value it was handed — see Finding D, Phase 5."
    )


def test_deps_declares_no_contextvar() -> None:
    """``app/deps.py`` — the backend REST admission seam — holds no
    module-level ``ContextVar``.

    Not a style rule: this is where the pressure to add one comes from.
    A field that "doesn't fit the return shape" must go on
    :class:`RestPrincipal`, because a module global is invisible to the
    handler that consumes it and its absence is indistinguishable from a
    legitimate default.
    """
    path = _PACKAGE_ROOT / "app" / "deps.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    declarations = [
        f"line {node.lineno}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == "ContextVar")
            or (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "ContextVar"
            )
        )
    ]

    assert declarations == [], (
        "app/deps.py declares a ContextVar ("
        + ", ".join(declarations)
        + "); per-request identity belongs on the returned RestPrincipal."
    )


# ── the detector is itself detected ───────────────────────────────


_SYNTHETIC_OFFENDER = '''
import contextvars

_forwarding_route_role = contextvars.ContextVar("x", default=None)


def build():
    from .deps import forwarding_route_role

    threaded = forwarding_route_role()
    return threaded or ("operator", False)
'''


def test_detector_flags_a_hand_rolled_side_channel() -> None:
    """The RED half, kept permanently: a module that reintroduces the
    carrier must be flagged. Without this, deleting the carrier would
    also silently retire the rule that keeps it deleted."""
    hits = _references(_SYNTHETIC_OFFENDER, "synthetic.py")

    assert hits, (
        "the detector no longer flags a hand-rolled forwarding-role "
        "ContextVar — the invariant above has decayed into a no-op"
    )


# ── what replaced it ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "principal,expected",
    [
        # Forwarding: the REAL signed role, straight off the value.
        (
            RestPrincipal(
                kind="forwarding", operator_id="v", project_role="viewer",
            ),
            ("viewer", False),
        ),
        (
            RestPrincipal(
                kind="forwarding", operator_id="o", project_role="operator",
            ),
            ("operator", False),
        ),
        (
            RestPrincipal(
                kind="forwarding", operator_id="s", project_role=None,
                sysadmin=True,
            ),
            (None, True),
        ),
        # Cookie / bearer: None, so the dispatch seam keeps its historical
        # operator-tier default. NOT an oversight — see route_role's
        # docstring. Threading a cookie path's legitimately-None
        # project_role (backend can't name its own project) onto the
        # Principal would deny every non-``system.*`` capability.
        (
            RestPrincipal(kind="session", user={"username": "a"}), None,
        ),
        (
            RestPrincipal(
                kind="session", user={"username": "a"},
                project_role="operator",
            ),
            None,
        ),
        (RestPrincipal(kind="operator_bearer"), None),
    ],
)
def test_route_role_matches_the_carriers_semantics(
    principal: RestPrincipal, expected
) -> None:
    """``RestPrincipal.route_role()`` reproduces exactly what the
    ContextVar reported per door — the forwarding role, and ``None``
    everywhere else."""
    assert principal.route_role() == expected
