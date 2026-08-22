"""R9-F5 — a project renamed TWICE permanently desyncs its on-disk
workspace directory from the registry, even with no race involved.

Root cause: ``Registry.rename()`` (``agent_mcp/router/project_registry.py``)
copied the record verbatim into the new key and never updated the
record's ``"workspace"`` field. That field stayed frozen at whatever
value was set at project CREATION time, forever.

``admin_api.py``'s rename handler gates the actual ``os.rename()`` of
the on-disk directory on ``workspace.name == old_name`` (only move the
directory if the registry's stored workspace still looks like it lives
under the CURRENT name). That's true for a project's first-ever rename
(workspace.name still matches the live name at that point), but false
for every rename after the first — ``old_name`` has already changed,
while ``workspace`` in the registry never followed it. The directory
move gets silently skipped, the HTTP call still reports 200, and the
registry now permanently disagrees with the filesystem.

Concretely: create "a" (workspace=.../a) -> rename to "b" (dir moves
a -> b, genuinely correct) -> rename BACK to "a" (old_name is "b", but
registry workspace field still says ".../a" from creation time, so
workspace.name ("a") != old_name ("b") is FALSE... wait: the guard
compares workspace.name to old_name — for the second rename old_name
is "b", and the STALE workspace field is ".../a", so "a" != "b" and
the move is skipped). The registry ends up saying project "a" lives at
".../a", but the real directory is still named ".../b" — a live
project whose backend can never find its own workspace again.

RED against the pre-fix tree: after the second rename, the directory
that should be at the new name doesn't exist there (it's still parked
under the intermediate name), and/or the registry's own workspace
field doesn't match a real, existing directory. GREEN: the workspace
field tracks every rename (not just the first) and always points at
the directory that actually exists on disk.

Fix (agent_mcp/router/project_registry.py, ``Registry.rename()``):
whenever the CURRENT workspace field's basename matches ``old_name``
(the naming-convention invariant every project created via
``POST /api/router/projects`` satisfies — workspace is always
``DEFAULT_WORKSPACE_PARENT / <name>``), rewrite the field's basename
to ``new_name`` too, so it keeps tracking the project across an
arbitrary number of renames instead of freezing at creation time. This
is the same computation ``admin_api.py``'s move-guard already performs
independently — teaching the registry to apply it too keeps both sides
permanently in agreement, including for the external
``agent-mcp-launcher`` shell script, which resolves a running
project's workspace directory straight out of this same registry
file's ``workspace`` field (not from any Python-side cache).
"""

from __future__ import annotations

import itertools
import json

import pytest

pytestmark = [pytest.mark.asyncio]

_ACCEPT = {
    "Accept": "application/vnd.agent-mcp.v1+json",
    "Content-Type": "application/json",
}


async def _rename(client, old_name: str, new_name: str, grace_days: int = 7):
    return await client.patch(
        f"/agent-mcp/api/router/projects/{old_name}",
        data=json.dumps({"name": new_name, "grace_days": grace_days}),
        headers=_ACCEPT,
        allow_redirects=False,
    )


async def test_double_rename_keeps_workspace_dir_in_sync(
    aiohttp_client, router_app, router_module, register_project,
) -> None:
    """Two sequential renames (no race) must leave the workspace
    directory, and the registry's ``workspace`` field, both correctly
    pointing at the SAME real, existing directory."""
    ws_a = register_project("proj-a")
    client = await aiohttp_client(router_app)

    # First rename: a -> b. This one "works" even pre-fix. grace_days=0
    # so the "proj-a" alias it parks expires immediately — otherwise
    # renaming straight back to "proj-a" a moment later would 409 on
    # the (unrelated, intentional) active-alias-collision guard instead
    # of exercising the workspace-desync bug this test targets.
    resp = await _rename(client, "proj-a", "proj-b", grace_days=0)
    assert resp.status == 200, f"got {resp.status}: {await resp.text()}"

    ws_b = ws_a.with_name("proj-b")
    assert ws_b.is_dir(), "first rename should have moved the directory"
    assert not ws_a.exists()

    row_b = router_module._REGISTRY.get("proj-b")
    assert row_b is not None
    assert row_b["workspace"] == str(ws_b), (
        "registry workspace field must track the directory the FIRST "
        "rename actually moved it to, not the creation-time path"
    )

    # Second rename: b -> a (back to the original name). This is the
    # one that silently no-ops the directory move pre-fix.
    resp = await _rename(client, "proj-b", "proj-a")
    assert resp.status == 200, f"got {resp.status}: {await resp.text()}"

    row_a = router_module._REGISTRY.get("proj-a")
    assert row_a is not None
    workspace_after = router_module.Path(row_a["workspace"])

    # The registry's own workspace field must point at a directory
    # that actually exists — this is the assertion that catches the
    # silent-skip specifically: pre-fix, the field stays stuck at
    # ``ws_a`` (never updated by the first rename either), which
    # happens to be the ORIGINAL path — but the physical directory
    # was moved away to ``ws_b`` and, because the second rename's
    # move-guard silently no-ops, it never comes back.
    assert workspace_after.is_dir(), (
        f"registry says workspace is {workspace_after}, but that "
        f"directory does not exist on disk — desynced by the second "
        f"rename"
    )
    assert workspace_after == ws_a, (
        "after renaming back to the original name, the workspace dir "
        "should genuinely be back at the original path"
    )
    assert ws_a.is_dir()
    assert not ws_b.exists(), (
        "the intermediate directory should have been moved away, not "
        "left behind as an orphan"
    )


async def test_triple_rename_keeps_workspace_dir_in_sync(
    aiohttp_client, router_app, router_module, register_project,
) -> None:
    """The natural generalization: 3+ renames in a row must each keep
    the workspace directory and registry field correctly synced, not
    just the reported 2-rename case."""
    ws_1 = register_project("stage-one")
    client = await aiohttp_client(router_app)

    names = ["stage-one", "stage-two", "stage-three", "stage-four"]
    for old_name, new_name in itertools.pairwise(names):
        resp = await _rename(client, old_name, new_name)
        assert resp.status == 200, (
            f"rename {old_name!r} -> {new_name!r} failed: "
            f"{resp.status}: {await resp.text()}"
        )

        row = router_module._REGISTRY.get(new_name)
        assert row is not None
        workspace = router_module.Path(row["workspace"])
        expected = ws_1.with_name(new_name)

        assert workspace == expected, (
            f"after renaming to {new_name!r}, registry workspace field "
            f"is {workspace}, expected {expected}"
        )
        assert workspace.is_dir(), (
            f"registry workspace field for {new_name!r} points at a "
            f"directory that doesn't exist: {workspace}"
        )

    # Only the final directory should remain; every intermediate name
    # should have been fully vacated, not left behind as an orphan.
    for stale_name in names[:-1]:
        assert not ws_1.with_name(stale_name).exists(), (
            f"orphaned directory left behind for old name {stale_name!r}"
        )
