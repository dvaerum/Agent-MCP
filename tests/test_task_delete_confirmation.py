"""DELETE /api/tasks/<id> must not silently subtree-delete (RED first).

Background
----------
``app/routers/tasks.py`` used to hardcode ``force_delete=True`` on the
dashboard's delete route. The MCP tool
(:func:`agent_mcp.tools.task_tools.delete_task_tool_impl`) HAS a cascade
safety guard — it returns ``Conflict`` listing the children when a task
has descendants and ``force_delete`` is falsy (task_tools.py ~:5659) —
but hardcoding ``True`` made that guard dead code on the dashboard
surface. Deleting a parent from the UI therefore destroyed the WHOLE
descendant subtree, cleared ``agents.current_task`` for every affected
agent, pruned ``depends_on_tasks`` across unrelated tasks (auto-advancing
blocked ones), and purged the RAG index for all of them — behind a dialog
that said only "This cannot be undone" and never named a count.

Contract pinned here
--------------------
* ``force_delete`` is client-supplied (JSON body, same shape as the
  memories DELETE route which already reads ``force_delete`` from the
  body — ``app/routers/memories.py`` ~:335), defaulting to ``False``.
* A parent with children and no explicit confirmation ⇒ 409 Conflict,
  nothing deleted.
* With ``force_delete: true`` the cascade still works (the operator typed
  DELETE in the tier-2 dialog).
* ``GET /api/tasks/<id>/delete-preview`` reports the blast radius so the
  dialog can name the count + titles. It mirrors
  ``/api/agents/<id>/purge-preview`` and reuses
  ``_collect_task_descendants`` rather than re-walking the tree.
"""

from __future__ import annotations

import pytest

from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio


def _live_task_ids(admin) -> set[str]:
    """Ids currently visible in ``GET /api/tasks``.

    There is no ``GET /api/tasks/<id>`` route, so the list endpoint is
    the existence oracle (same approach as
    ``test_rest_task_endpoints.py``).
    """
    listing = admin.client.get("/api/tasks").json()
    rows = listing.get("tasks", listing) if isinstance(listing, dict) else listing
    return {row["task_id"] for row in rows}


async def _make_parent_with_children(admin) -> tuple[str, list[str]]:
    """Create ``parent`` + two children + one grandchild; return ids."""
    parent = admin.post(
        "/api/tasks",
        json={"task_title": "parent", "task_description": "root"},
    ).json()["task_id"]

    child_ids = []
    for n in ("child-a", "child-b"):
        child_ids.append(
            admin.post(
                "/api/tasks",
                json={
                    "task_title": n,
                    "task_description": "child",
                    "parent_task": parent,
                },
            ).json()["task_id"]
        )
    grandchild = admin.post(
        "/api/tasks",
        json={
            "task_title": "grandchild",
            "task_description": "deep",
            "parent_task": child_ids[0],
        },
    ).json()["task_id"]
    child_ids.append(grandchild)
    return parent, child_ids


async def test_delete_parent_without_confirmation_is_refused(tmp_path) -> None:
    """RED: the dashboard's DELETE must NOT cascade without an explicit
    ``force_delete``. The tool's Conflict guard has to reach the wire."""
    async with mcp_session(tmp_path) as admin:
        parent, children = await _make_parent_with_children(admin)

        r = admin.request("DELETE", f"/api/tasks/{parent}", json={})
        assert r.status_code == 409, (
            "a parent task with children must be refused without an "
            f"explicit force_delete, got {r.status_code}: {r.text}"
        )

        # Nothing was deleted — parent AND the whole subtree survive.
        alive = _live_task_ids(admin)
        assert {parent, *children} <= alive, (
            "a refused delete must leave the whole subtree intact"
        )


async def test_delete_parent_with_force_delete_cascades(tmp_path) -> None:
    """With the explicit confirmation the cascade still works — the fix
    re-arms the guard, it does not remove the capability."""
    async with mcp_session(tmp_path) as admin:
        parent, children = await _make_parent_with_children(admin)

        r = admin.request(
            "DELETE", f"/api/tasks/{parent}", json={"force_delete": True}
        )
        assert r.status_code == 200, r.text
        assert r.json().get("success") is True, r.json()

        alive = _live_task_ids(admin)
        assert not ({parent, *children} & alive), (
            "an explicitly confirmed force delete must still cascade"
        )


async def test_delete_leaf_task_needs_no_confirmation(tmp_path) -> None:
    """Tier 1 stays tier 1: a childless task deletes on a plain confirm
    (no ``force_delete``), exactly as before."""
    async with mcp_session(tmp_path) as admin:
        leaf = admin.post(
            "/api/tasks",
            json={"task_title": "leaf", "task_description": "no kids"},
        ).json()["task_id"]

        r = admin.request("DELETE", f"/api/tasks/{leaf}", json={})
        assert r.status_code == 200, r.text
        assert leaf not in _live_task_ids(admin)


async def test_delete_preview_reports_the_blast_radius(tmp_path) -> None:
    """GET /api/tasks/<id>/delete-preview names the subtree."""
    async with mcp_session(tmp_path) as admin:
        parent, children = await _make_parent_with_children(admin)

        r = admin.get(f"/api/tasks/{parent}/delete-preview")
        assert r.status_code == 200, r.text
        preview = r.json()

        assert preview["task_id"] == parent
        assert preview["descendant_count"] == len(children), preview
        titles = {d["title"] for d in preview["descendants"]}
        assert {"child-a", "child-b", "grandchild"} <= titles, preview
        ids = {d["task_id"] for d in preview["descendants"]}
        assert ids == set(children), preview
        # The dialog uses this to pick tier 1 vs tier 2.
        assert preview["requires_force"] is True, preview


async def test_delete_preview_on_a_leaf_is_empty(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        leaf = admin.post(
            "/api/tasks",
            json={"task_title": "lonely", "task_description": "-"},
        ).json()["task_id"]

        preview = admin.get(f"/api/tasks/{leaf}/delete-preview").json()
        assert preview["descendant_count"] == 0, preview
        assert preview["descendants"] == [], preview
        assert preview["requires_force"] is False, preview


async def test_delete_preview_404s_on_unknown_task(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        r = admin.get("/api/tasks/task_does_not_exist/delete-preview")
        assert r.status_code == 404, r.text
