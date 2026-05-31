"""Regression guards for dashboard task action buttons.

Upstream's task-details-panel renders Start / Mark Complete / Add Note
buttons but with no onClick — they're inert chrome. This PR wires
them to the now-existing REST endpoints:
  - update_task_status / add_note → POST /api/update-task-dashboard
  - delete                        → DELETE /api/tasks/<id> (PR #12)

Tests confirm:
1. api.ts has a `deleteTask` method that DELETEs /api/tasks/<id>.
2. api.ts `updateTask` routes through `/update-task-dashboard` (the
   real upstream endpoint), not the imaginary `PUT /tasks/<id>`.
3. task-details-panel.tsx wires onClick handlers on action buttons.
"""

from __future__ import annotations

import re
from pathlib import Path

DASHBOARD = Path("agent_mcp/dashboard")


def _read(rel: str) -> str:
    return (DASHBOARD / rel).read_text()


def test_api_has_delete_task_method() -> None:
    src = _read("lib/api.ts")
    assert "deleteTask" in src, "expected api.ts to export deleteTask method"
    # It must use the real upstream endpoint shape.
    assert "/tasks/" in src and "method: 'DELETE'" in src, (
        "deleteTask should DELETE /api/tasks/<id>"
    )


def test_api_update_task_uses_update_task_dashboard_endpoint() -> None:
    src = _read("lib/api.ts")
    # find the updateTask body
    m = re.search(r"async updateTask\([^)]*\).*?\n  \}", src, re.DOTALL)
    assert m, "updateTask not found"
    body = m.group(0)
    # Real upstream endpoint, not the imaginary PUT /tasks/<id>.
    assert "update-task-dashboard" in body, (
        "updateTask must POST to /api/update-task-dashboard (the real "
        "upstream endpoint), not PUT /tasks/<id> (which 405s)"
    )


def test_task_details_panel_wires_button_handlers() -> None:
    src = _read("components/dashboard/task-details-panel.tsx")
    # Some click handler must reference apiClient.deleteTask / updateTask.
    assert (
        "apiClient.updateTask" in src or "apiClient.deleteTask" in src
    ), "expected at least one button to call apiClient.update/deleteTask"
    # The Start/Mark Complete/Add Note buttons need onClick.
    # Heuristic: count onClick occurrences after the "Action Buttons" comment.
    action_section = src.split("Action Buttons", 1)
    assert len(action_section) == 2, "no 'Action Buttons' section found"
    after = action_section[1]
    onclicks = after.count("onClick")
    assert onclicks >= 2, (
        f"expected ≥2 onClick handlers in the action-buttons section; "
        f"got {onclicks}"
    )
