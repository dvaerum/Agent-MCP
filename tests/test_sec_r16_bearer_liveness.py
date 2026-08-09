"""R16-DiD-1 — bearer-liveness predicate consistency on the REST dep.

``app.deps._is_operator_tier_bearer`` is the operator-tier admit gate on
the per-project backend's ``Authorization: Bearer`` path. Historically it
rejected only ``status == 'terminated'`` while every sibling
bearer-liveness gate uses the canonical ``LIVE_AGENT_SQL`` predicate,
``status NOT IN ('terminated', 'tombstone')`` (see
``repositories/agent_repository.py`` :data:`LIVE_AGENT_SQL`; siblings:
``app/main_app.py`` cache-only gate excludes both, ``app/routers/delivery.py``
uses ``LIVE_AGENT_SQL``).

A purged agent leaves a ``tombstone`` row bound to a predictable
``__tombstone_<agent_id>`` token (``admin_tools.insert_tombstone``). Today
tombstone rows carry a NULL ``agent_role`` so the gap is NON-EXPLOITABLE —
``_is_operator_tier_bearer`` also requires ``agent_role`` in
``{manager, admin}``. This module pins the predicate itself: a
``tombstone`` row constructed with a NON-NULL operator role (the state a
future change that gives tombstones a role would create) must be REJECTED,
so the weak ``!= 'terminated'`` variant cannot silently reopen the hole.

These are pure predicate unit tests: a fake repo is installed via
:func:`set_agent_repo` so the gate resolves against a hand-built row and
no DB is required.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_mcp.app.deps import _is_operator_tier_bearer
from agent_mcp.repositories import clear_agent_repo, set_agent_repo


class _FakeAgentRepo:
    """Minimal stand-in exposing only ``get_by_token`` — the one method
    :func:`_is_operator_tier_bearer` calls. Returns ``_row`` for any
    non-empty token, mirroring ``AgentRepository.get_by_token`` which
    returns the row for ANY status (terminated/tombstone included) for
    audit/attribution."""

    def __init__(self, row: dict[str, Any] | None) -> None:
        self._row = row

    def get_by_token(self, token: str) -> dict[str, Any] | None:
        return self._row


@pytest.fixture
def install_repo():
    """Install a fake repo returning a caller-supplied row, tearing it
    back down so the singleton doesn't leak across tests."""

    def _install(row: dict[str, Any] | None) -> None:
        set_agent_repo(_FakeAgentRepo(row))  # type: ignore[arg-type]

    yield _install
    clear_agent_repo()


# ── RED: the tombstone-with-role gap ────────────────────────────────


@pytest.mark.parametrize("role", ["manager", "admin"])
def test_tombstone_row_with_operator_role_is_rejected(install_repo, role):
    """A ``tombstone`` row carrying a NON-NULL operator ``agent_role``
    must NOT be admitted as an operator-tier bearer.

    Constructed directly (role non-NULL) so it proves the predicate gap
    rather than relying on the incidental NULL-role of real tombstones.
    Before the fix (``status == 'terminated'`` only) this returned True.
    """
    install_repo({
        "agent_id": "ghost",
        "token": "__tombstone_ghost",
        "status": "tombstone",
        "agent_role": role,
    })
    assert _is_operator_tier_bearer("__tombstone_ghost") is False


# ── terminated still rejected (unchanged behaviour) ─────────────────


@pytest.mark.parametrize("role", ["manager", "admin"])
def test_terminated_operator_bearer_is_rejected(install_repo, role):
    """A terminated manager/admin bearer must stay rejected."""
    install_repo({
        "agent_id": "alice",
        "token": "tok-alice",
        "status": "terminated",
        "agent_role": role,
    })
    assert _is_operator_tier_bearer("tok-alice") is False


# ── live operator bearers still admitted (happy path) ───────────────


@pytest.mark.parametrize("role", ["manager", "admin"])
def test_live_operator_bearer_is_admitted(install_repo, role):
    """A LIVE manager/admin bearer must still be admitted."""
    install_repo({
        "agent_id": "boss",
        "token": "tok-boss",
        "status": "active",
        "agent_role": role,
    })
    assert _is_operator_tier_bearer("tok-boss") is True


# ── negatives that must stay negative ───────────────────────────────


def test_live_worker_bearer_is_rejected(install_repo):
    """A live worker bearer is not operator-tier (no escalation)."""
    install_repo({
        "agent_id": "w1",
        "token": "tok-w1",
        "status": "active",
        "agent_role": "worker",
    })
    assert _is_operator_tier_bearer("tok-w1") is False


def test_empty_token_is_rejected(install_repo):
    """An empty token short-circuits to False without a lookup."""
    install_repo({"status": "active", "agent_role": "manager"})
    assert _is_operator_tier_bearer("") is False


def test_unknown_token_is_rejected(install_repo):
    """A token that resolves to no row is rejected."""
    install_repo(None)
    assert _is_operator_tier_bearer("nope") is False
