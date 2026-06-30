# Agent-MCP/agent_mcp/repositories/group_capability_repository.py
"""GroupCapabilityRepository — read/write the ``group_capability`` table.

Wave 9 PR 0 of 7 in ``prancy-napping-pie.md``. The new
``group_capability`` table (migration ``0004_group_capability.py`` in
``agent_mcp/router/migrations/versions/``) holds the
``(group_id, capability)`` grants a sysadmin sets via the Wave 9 PR 5
dashboard UI. This module is the single seam through which the
authorisation layer reads those rows and through which the (future)
dashboard PUT handler writes them.

Why a free-function module rather than the lifespan-owned class
pattern used for :class:`MessageRepository` /
:class:`TaskRepository` / :class:`AgentRepository`: the group-
capability data lives in the **router** DB (``router.db``), not the
per-project agent DB. The router accesses its DB through a different
seam (``agent_mcp.router.identity._connect``) and has no Repository
singleton lifecycle to plug into. The router-side pattern is
module-level functions (see :mod:`agent_mcp.router.group_resolver`),
so this module follows that shape — it co-exists in the
``agent_mcp.repositories`` package per the Wave 9 plan path, but its
implementation is a thin functional wrapper over the router's
sqlite connection.

Public surface:

* :func:`fetch` — return the frozenset of capabilities granted to a
  single ``group_id``. Single SELECT, hot read path called once per
  resolved group during :func:`resolve_capabilities`.
* :func:`replace` — atomic "set the cap list for this group" used by
  the Wave 9 PR 5 dashboard PUT handler. Wraps DELETE + INSERTs in a
  single transaction.

Defensive against router-DB-not-initialised: callers (e.g.
``core.capabilities.resolve_capabilities``) wrap the import + call in
try/except so a fresh test environment without a router DB still
produces a valid Principal. The functions themselves do NOT swallow
failures — that's the caller's responsibility, because the dashboard
write path WANTS exceptions to bubble.
"""
from __future__ import annotations

from typing import Iterable


def fetch(group_id: str) -> frozenset[str]:
    """Return every capability granted to ``group_id``.

    Hot read path — invoked once per resolved group during
    :func:`agent_mcp.core.capabilities.resolve_capabilities` (one
    operator request → N group ids → N fetch calls). The composite
    primary key (group_id, capability) covers the WHERE clause
    natively, so no secondary index is needed.

    Returns an empty frozenset when no rows match. Raises on a real
    DB error (FK violation, missing table) so the caller can decide
    whether to swallow (resolve-capabilities path) or surface
    (dashboard PUT path).
    """
    from ..router import identity as _identity

    with _identity._connect() as conn:
        cur = conn.execute(
            "SELECT capability FROM group_capability WHERE group_id = ?",
            (group_id,),
        )
        return frozenset(row["capability"] for row in cur.fetchall())


def replace(group_id: str, capabilities: Iterable[str]) -> None:
    """Atomically replace the capability set for ``group_id``.

    The dashboard PUT handler hands in the COMPLETE new cap set the
    operator ticked / unticked; we mirror that semantic (the function
    is "set the cap list to exactly these") rather than offering
    additive grant/revoke primitives. The single-statement DELETE +
    multi-row INSERT runs inside one transaction (the
    ``_connect()`` context manager commits on clean exit, rolls back
    on exception), so a mid-write failure leaves the prior cap set
    intact.

    Idempotent on cap content: calling twice with the same set is a
    no-op as far as the resolved cap surface is concerned, but the
    rows are deleted and re-inserted both times (the implementation
    is "set" not "diff" — the dashboard always has the full ticked
    state in hand).

    Does NOT validate caps against
    :data:`agent_mcp.core.capabilities.KNOWN_CAPABILITIES` —
    validation lives at the API seam (the PUT handler shipped in
    Wave 9 PR 5 will reject unknown cap strings with 422). Keeping
    the validation out of the repository lets the dashboard pre-flight
    the list before the network round-trip and keeps this module a
    thin DB seam.
    """
    from ..router import identity as _identity

    caps_list = list(dict.fromkeys(capabilities))  # de-dup, preserve order
    with _identity._connect() as conn:
        conn.execute(
            "DELETE FROM group_capability WHERE group_id = ?",
            (group_id,),
        )
        if caps_list:
            conn.executemany(
                "INSERT INTO group_capability (group_id, capability) "
                "VALUES (?, ?)",
                [(group_id, cap) for cap in caps_list],
            )


__all__ = ["fetch", "replace"]
