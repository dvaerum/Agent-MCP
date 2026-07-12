"""Shared bounded-read clamp for the ``/api`` list/read endpoints.

Single source of truth for the ``?limit`` clamp that bounds every
dashboard/list read so a project with thousands of rows can't
materialise an unbounded payload on each request (2026-06-02 database
review, item 2; pentest R2-F2 / R3-F3).

Lives in its own module — NOT in ``composition.py`` — because
``composition`` already imports from ``agents`` (``_mcp_presence_for``),
so ``agents`` importing the clamp from ``composition`` would form an
import cycle. Both the composition ``*-data`` reads and the standalone
``/api/tasks`` + ``/api/agents`` list reads import the clamp from here,
so all list endpoints share ONE default + upper bound and can't drift.
``composition`` re-exports the three names for back-compat with callers
(and tests) that still import them from ``composition``.
"""

from __future__ import annotations

from starlette.requests import Request

# Default per-section LIMIT; bounded by the 2026-06-02 database review
# (item 2) so a project with thousands of rows no longer materialises an
# unbounded payload on every dashboard refresh. Callers that want more can
# pass ``?limit=N``, but ``_ALL_DATA_MAX_LIMIT`` clamps the upper bound to
# keep the JSON shape sane.
_ALL_DATA_DEFAULT_LIMIT = 500
_ALL_DATA_MAX_LIMIT = 5000


def _clamp_section_limit(request: Request) -> int:
    """Parse the optional ``?limit`` query param and clamp it to
    ``[1, _ALL_DATA_MAX_LIMIT]``, defaulting to
    ``_ALL_DATA_DEFAULT_LIMIT`` when absent or unparseable.

    Single source of truth for the bounded-read clamp. ``/api/all-data``
    grew this clamp first (db review item 2); pentest R2-F2 converged the
    sibling composition reads (``/api/graph-data``,
    ``/api/task-tree-data``, ``/api/context-data``) onto it, and pentest
    R3-F3 converged the remaining standalone list reads
    (``/api/tasks``, ``/api/agents``) onto it too — so every list read
    shares ONE default + upper bound and can't drift. Callers that want
    more rows pass ``?limit=N`` but never escape the upper bound.
    """
    try:
        requested_limit = int(
            request.query_params.get("limit", _ALL_DATA_DEFAULT_LIMIT)
        )
    except (TypeError, ValueError):
        requested_limit = _ALL_DATA_DEFAULT_LIMIT
    return max(1, min(requested_limit, _ALL_DATA_MAX_LIMIT))


__all__ = [
    "_ALL_DATA_DEFAULT_LIMIT",
    "_ALL_DATA_MAX_LIMIT",
    "_clamp_section_limit",
]
