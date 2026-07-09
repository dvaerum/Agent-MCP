"""``_analyze_context_health`` must return a COMPLETE shape, even for an
empty context — the empty-context backup crash (round 3 follow-up).

Root cause: ``_analyze_context_health([])`` used to return only
``{"status": "no_data", "total": 0}``. Every caller
(``backup_project_context_tool_impl`` at line ~1503 and the
``view_project_context`` health block at line ~763) then reads
``health["health_score"]`` / ``["stale_entries"]`` / ``["json_errors"]``
/ ``["recommendations"]`` unconditionally, so backing up (or viewing
the health of) a project with ZERO context entries raised
``KeyError: 'health_score'`` and the backup tool returned ``Failed``.

That crash was invisible while returned error ``ToolResult`` variants
reached the MCP client with ``isError=False`` (the AS-1 bug fixed in the
same PR). Making ``isError`` truthful surfaced it; the real fix is to
make the health report always emit a complete shape so every caller is
safe — not just guard one call site.
"""

from __future__ import annotations

from agent_mcp.tools.project_context_tools import _analyze_context_health

# The fields every consumer of the health report reads. If any is
# missing the consumers KeyError.
_REQUIRED_FIELDS = {
    "status",
    "health_score",
    "total",
    "stale_entries",
    "json_errors",
    "large_entries",
    "issues",
    "warnings",
    "recommendations",
}


def test_empty_context_health_has_complete_shape() -> None:
    """An empty context yields a full health dict, not the truncated
    ``{"status": "no_data", "total": 0}`` that made every caller
    KeyError."""
    report = _analyze_context_health([])

    missing = _REQUIRED_FIELDS - report.keys()
    assert not missing, (
        f"empty-context health report is missing fields {sorted(missing)}; "
        f"got keys {sorted(report.keys())}"
    )
    # status stays 'no_data' — the dashboard (lib/api.ts) treats it as a
    # distinct, valid status; we add the numeric fields around it.
    assert report["status"] == "no_data"
    assert report["total"] == 0
    assert report["stale_entries"] == 0
    assert report["json_errors"] == 0
    assert report["large_entries"] == 0
    # A numeric score the f-string formatters can render.
    assert isinstance(report["health_score"], (int, float))
    # recommendations is a non-empty list (callers index [0] / len()).
    assert isinstance(report["recommendations"], list)
    assert report["recommendations"]


def test_populated_context_health_still_complete() -> None:
    """Regression: the populated path keeps the same complete shape."""
    report = _analyze_context_health(
        [{"context_key": "k", "value": "{}", "updated_at": None}]
    )
    missing = _REQUIRED_FIELDS - report.keys()
    assert not missing, f"missing {sorted(missing)}"
    assert report["total"] == 1
