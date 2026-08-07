"""Security (AZ-R28-1, round-28 hardening parity): the per-project
backend composition READ endpoints must require an operator session,
matching their already-gated in-file siblings.

FINDING (owner-authorized security review, 2026-07):

  * ``GET /api/status``          — system status
  * ``GET /api/graph-data``      — agents/tasks/files relationship graph
  * ``GET /api/task-tree-data``  — task tree

had NO backend auth dependency at all, while every sibling composition
READ in the same router — ``/api/node-details`` (#281), ``/api/all-data``
and ``/api/context-data`` (#280) — carries
``Depends(require_operator_session)``. ``AuthHeaderMiddleware`` gates
only ``/mcp``, not ``/api/*``, so on the backend's own (Unix-domain
socket) surface these three were unauthenticated — the exact
direct-UDS defense-in-depth tier PRs #280 / #281 were shipped to close.
The aiohttp router still fronts them with cookie+membership, so this is
NOT remotely exploitable; the fix restores parity for the direct-backend
tier.

Mirrors the assertion shape of
``test_sec_composition_secret_exposure.py``:
``admin.client.get`` drives the RAW (unauthenticated) wire → 401;
``admin.get`` attaches a signed forwarding header → 200.
"""

from __future__ import annotations

import pytest

from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio


_COMPOSITION_READS = ("/api/status", "/api/graph-data", "/api/task-tree-data")


@pytest.mark.parametrize("path", _COMPOSITION_READS)
async def test_composition_read_requires_operator_session(
    tmp_path, path: str
) -> None:
    """No auth at all → 401. Each route previously had NO auth dep."""
    async with mcp_session(tmp_path) as admin:
        r = admin.client.get(path)
        assert r.status_code == 401, (
            f"{path} must require an operator session (got "
            f"{r.status_code}): {r.text}"
        )


@pytest.mark.parametrize("path", _COMPOSITION_READS)
async def test_composition_read_admits_authenticated_operator(
    tmp_path, path: str
) -> None:
    """A signed forwarding-header operator still reads the endpoint —
    the legitimate dashboard path must not regress."""
    async with mcp_session(tmp_path) as admin:
        r = admin.get(path)  # signed forwarding header
        assert r.status_code == 200, (
            f"{path} must still admit an authenticated operator (got "
            f"{r.status_code}): {r.text}"
        )
