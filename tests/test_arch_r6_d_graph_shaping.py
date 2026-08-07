"""arch-r6 #4: unit tests for the shared task node/edge shaping helpers
extracted from `features/dashboard/api.py`'s two graph builders
(`fetch_graph_data_logic` and `fetch_task_tree_data_logic`).

Before this extraction, `_task_node`/`_parent_edge`/`_dependency_edges`
didn't exist as standalone units — the same logic was duplicated inline
in each builder and only reachable through a full DB + HTTP round trip
(see `tests/test_sec_r31_tombstone_siblings.py`). These tests exercise
the extracted helpers directly, in particular the truncation boundaries
and malformed-JSON guard that a full-graph-assemble test can't isolate.

Note on scope: `_task_node`'s tooltip `title` text and `mass` are NOT
identical between the two builders (the full graph adds
'Assigned'/'Created by' lines and sets mass=2; the task tree omits both
and says 'Desc' instead of 'Description') — see `_task_node`'s
docstring. Those differences are threaded through as caller-supplied
parameters rather than forced together, so the "null/empty description"
boundary is exercised via `_truncate_field` (the shared helper both
`_task_node`'s label and each caller's description tooltip line use),
not inside `_task_node` itself.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from agent_mcp.features.dashboard.api import (
    _dependency_edges,
    _parent_edge,
    _task_node,
    _truncate_field,
)


def _task_row(**overrides: Any) -> sqlite3.Row:
    """Build a real sqlite3.Row for a `tasks`-shaped record so the
    helpers under test see exactly what production code sees (attribute
    access by column name, not a plain dict).
    """
    defaults: dict[str, Any] = {
        "task_id": "task-1",
        "title": "Some Task",
        "status": "pending",
        "assigned_to": None,
        "created_by": "admin",
        "parent_task": None,
        "depends_on_tasks": None,
        "description": "A description",
    }
    defaults.update(overrides)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    cols = list(defaults.keys())
    conn.execute(f"CREATE TABLE t ({', '.join(f'{c} TEXT' for c in cols)})")
    conn.execute(
        f"INSERT INTO t ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
        [defaults[c] for c in cols],
    )
    row = conn.execute(f"SELECT {', '.join(cols)} FROM t").fetchone()
    conn.close()
    return row


# --------------------------- _truncate_field ---------------------------


def test_truncate_field_title_exactly_limit_is_unchanged() -> None:
    text = "x" * 20
    assert _truncate_field(text, 20) == text


def test_truncate_field_title_one_over_limit_is_truncated() -> None:
    text = "x" * 21
    result = _truncate_field(text, 20)
    assert result == "x" * 20 + "..."


def test_truncate_field_description_none_falls_back_to_na() -> None:
    assert _truncate_field(None, 100, na_if_empty=True) == "N/A"


def test_truncate_field_description_empty_string_falls_back_to_na() -> None:
    assert _truncate_field("", 100, na_if_empty=True) == "N/A"


def test_truncate_field_description_exactly_limit_is_unchanged() -> None:
    text = "y" * 100
    assert _truncate_field(text, 100, na_if_empty=True) == text


def test_truncate_field_description_one_over_limit_is_truncated() -> None:
    text = "y" * 101
    result = _truncate_field(text, 100, na_if_empty=True)
    assert result == "y" * 100 + "..."


# ------------------------------ _task_node ------------------------------


def test_task_node_label_title_exactly_20_chars_unchanged() -> None:
    row = _task_row(title="x" * 20)
    node = _task_node(row, title="tooltip")
    assert node["label"] == "x" * 20


def test_task_node_label_title_21_chars_truncated() -> None:
    row = _task_row(title="x" * 21)
    node = _task_node(row, title="tooltip")
    assert node["label"] == "x" * 20 + "..."


def test_task_node_shared_fields() -> None:
    row = _task_row(task_id="task-42", status="completed")
    node = _task_node(row, title="tooltip text")
    assert node["id"] == "task_task-42"
    assert node["group"] == "task"
    assert node["title"] == "tooltip text"
    # get_node_style('task', 'completed') output is spread in.
    assert node["color"] == "#9E9E9E"
    assert node["shape"] == "square"
    assert "mass" not in node, "mass must be omitted unless explicitly passed"


def test_task_node_mass_passed_through_when_given() -> None:
    row = _task_row()
    node = _task_node(row, title="tooltip", mass=2)
    assert node["mass"] == 2


# ---------------------------- _dependency_edges ----------------------------


def test_dependency_edges_malformed_json_logged_and_returns_empty(caplog) -> None:
    row = _task_row(task_id="task-1", depends_on_tasks="not-json{{{")
    with caplog.at_level("WARNING"):
        edges = _dependency_edges(row, {"task_task-1"}, style={"color": "#fff"})
    assert edges == []
    assert any(
        "Could not parse depends_on_tasks JSON for task task-1" in rec.message
        for rec in caplog.records
    ), caplog.text


def test_dependency_edges_context_suffix_in_warning(caplog) -> None:
    row = _task_row(task_id="task-1", depends_on_tasks="{bad")
    with caplog.at_level("WARNING"):
        _dependency_edges(
            row, {"task_task-1"}, style={"color": "#fff"}, context="in task tree"
        )
    assert any(
        "for task task-1 in task tree:" in rec.message for rec in caplog.records
    ), caplog.text


def test_dependency_edges_empty_column_returns_empty() -> None:
    row = _task_row(depends_on_tasks=None)
    assert _dependency_edges(row, {"task_task-1"}, style={"color": "#fff"}) == []


def test_dependency_edges_filters_to_known_node_ids() -> None:
    row = _task_row(task_id="task-1", depends_on_tasks='["task-2", "task-missing"]')
    edges = _dependency_edges(
        row, {"task_task-1", "task_task-2"}, style={"color": "#E84393"}
    )
    assert len(edges) == 1
    assert edges[0]["from"] == "task_task-2"
    assert edges[0]["to"] == "task_task-1"
    assert edges[0]["color"] == "#E84393"


# ------------------------------ _parent_edge ------------------------------


def test_parent_edge_no_parent_task_returns_none() -> None:
    row = _task_row(task_id="task-1", parent_task=None)
    assert _parent_edge(row, {"task_task-1"}, title="t", style={}) is None


def test_parent_edge_parent_not_in_node_ids_returns_none() -> None:
    row = _task_row(task_id="task-1", parent_task="task-parent")
    # "task_task-parent" is deliberately absent from node_ids.
    assert _parent_edge(row, {"task_task-1"}, title="t", style={}) is None


def test_parent_edge_both_present_builds_edge() -> None:
    row = _task_row(task_id="task-1", parent_task="task-parent")
    edge = _parent_edge(
        row,
        {"task_task-1", "task_task-parent"},
        title="Parent of task-1",
        style={"color": {"color": "#6AB04C", "opacity": 0.9}, "width": 2},
    )
    assert edge == {
        "from": "task_task-parent",
        "to": "task_task-1",
        "title": "Parent of task-1",
        "color": {"color": "#6AB04C", "opacity": 0.9},
        "width": 2,
    }


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
