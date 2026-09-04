"""Unit tests for the vendored ProjectRegistry.

Ported from the deploy repo's
``users/dennis/agent-mcp/tests/test_project_registry.py``. The
original used stdlib ``unittest`` so it could be exercised without a
pytest install; this fork's test suite already requires pytest, so we
take the opportunity to use plain functions + monkeypatch.

Coverage matches the original Candidate B contract pinned on
2026-06-01:

  1. concurrent register()s never lose entries (LOCK_EX serialises)
  2. concurrent reads while a write is in flight never see torn JSON
     (LOCK_SH blocks until the writer releases)
  3. corrupt JSON triggers a .corrupt-<ISO> backup and the registry
     starts fresh, returning an empty mapping
  4. unregister removes only the named entry; register is idempotent
  5. atomic publish via os.replace (no torn writes mid-rename)
  6. sidecar lockfile inode stable across rewrites (so flock keeps
     serialising even after the registry file gets atomically
     replaced)
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


def _iso(dt: datetime) -> str:
    """ISO-8601 UTC with a trailing Z — the storage format aliases use."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def registry_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Repoint the registry's global REGISTRY_PATH at a tmp file."""
    from agent_mcp.router import project_registry

    path = tmp_path / "projects.local.json"
    monkeypatch.setattr(project_registry, "REGISTRY_PATH", path)
    return path


@pytest.fixture
def reg(registry_path: Path):
    from agent_mcp.router import project_registry

    return project_registry.ProjectRegistry()


# ── 1. Concurrent writes never lose entries ─────────────────────────


def test_ten_concurrent_registers_keep_all_entries(reg, tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()

    threads: list[threading.Thread] = []
    errors: list[BaseException] = []

    def go(i: int) -> None:
        try:
            reg.register(f"p{i}", str(workspace_root / f"p{i}"))
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    for i in range(10):
        threads.append(threading.Thread(target=go, args=(i,)))
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == [], f"register raised: {errors!r}"
    names = {p["name"] for p in reg.list()}
    assert names == {f"p{i}" for i in range(10)}, (
        "concurrent register() lost entries — LOCK_EX missing?"
    )


# ── 2. Reads during writes never see torn JSON ──────────────────────


def test_reads_during_writes_see_either_old_or_new(reg) -> None:
    for i in range(5):
        reg.register(f"old{i}", f"/tmp/old{i}")

    stop = threading.Event()
    errors: list[BaseException] = []

    def writer() -> None:
        try:
            while not stop.is_set():
                for i in range(5):
                    reg.register(f"new{i}", f"/tmp/new{i}")
                for i in range(5):
                    reg.unregister(f"new{i}")
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    t = threading.Thread(target=writer)
    t.start()
    try:
        deadline = time.time() + 1.5
        observations = 0
        while time.time() < deadline:
            rows = reg.list()
            # Every row must have the canonical shape; torn JSON would
            # either raise json.JSONDecodeError out of `list()` or
            # return rows missing one of the required keys.
            for row in rows:
                assert "name" in row
                assert "workspace" in row
            observations += 1
        assert observations > 10
    finally:
        stop.set()
        t.join(timeout=5)
    assert errors == [], f"writer raised: {errors!r}"


# ── 3. Corrupt JSON recovery ────────────────────────────────────────


def test_corrupt_file_backed_up_and_registry_starts_fresh(
    reg, registry_path: Path,
) -> None:
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text("{this is not valid json")

    rows = reg.list()
    assert rows == [], "corrupt file must read as empty"

    siblings = list(registry_path.parent.iterdir())
    backups = [
        p for p in siblings
        if p != registry_path and ".corrupt-" in p.name
    ]
    assert len(backups) == 1, (
        f"expected exactly one corrupt-backup, got {siblings!r}"
    )

    # And the registry recovers — the next register() works.
    reg.register("recovered", "/tmp/recovered")
    assert {p["name"] for p in reg.list()} == {"recovered"}


# ── 4. Unregister ───────────────────────────────────────────────────


def test_unregister_removes_only_named(reg) -> None:
    reg.register("alpha", "/tmp/alpha")
    reg.register("beta", "/tmp/beta")
    reg.register("gamma", "/tmp/gamma")

    reg.unregister("beta")

    names = {p["name"] for p in reg.list()}
    assert names == {"alpha", "gamma"}
    assert reg.get("beta") is None
    assert reg.get("alpha")["workspace"] == "/tmp/alpha"


def test_unregister_unknown_is_noop(reg) -> None:
    reg.register("alpha", "/tmp/alpha")
    # Must not raise — same semantics as `dict.pop(k, None)`.
    reg.unregister("never-existed")
    assert {p["name"] for p in reg.list()} == {"alpha"}


# ── 4b. Idempotent register ─────────────────────────────────────────


def test_reregister_same_workspace_is_noop(reg) -> None:
    first = reg.register("alpha", "/tmp/alpha")
    second = reg.register("alpha", "/tmp/alpha")
    assert first == second
    assert {p["name"] for p in reg.list()} == {"alpha"}


def test_reregister_different_workspace_raises(reg) -> None:
    reg.register("alpha", "/tmp/alpha")
    with pytest.raises(ValueError):
        reg.register("alpha", "/tmp/different")


def test_register_on_active_alias_raises(reg) -> None:
    """BL-R33-1 (chokepoint defense-in-depth): registering a name that is
    a currently-active alias of ANOTHER project must raise — otherwise the
    new real project silently shadows the alias (``resolve()`` returns the
    real project before the alias fallback). Mirrors ``rename``'s inline
    active-alias scan."""
    reg.register("alpha", "/tmp/alpha")
    reg.add_alias("alpha", "old-a")  # active (default 30-day grace)
    assert reg.resolve_alias("old-a") == "alpha"

    with pytest.raises(ValueError):
        reg.register("old-a", "/tmp/old-a")

    # The alias was not clobbered and no shadowing project was created.
    assert reg.get("old-a") is None
    assert reg.resolve_alias("old-a") == "alpha"


def test_register_on_expired_alias_succeeds(reg) -> None:
    """BL-R33-1 boundary: an EXPIRED alias is reclaimable — the chokepoint
    guard only blocks ACTIVE aliases (``resolve_alias`` returns None for a
    past-due alias)."""
    reg.register("alpha", "/tmp/alpha")
    reg.add_alias("alpha", "old-a", grace_days=0)  # dead on arrival
    assert reg.resolve_alias("old-a") is None

    row = reg.register("old-a", "/tmp/old-a")
    assert row["name"] == "old-a"
    assert reg.get("old-a") is not None


# ── 5. Atomic publish via os.replace ────────────────────────────────


def test_write_path_is_atomic_via_os_replace(
    reg, registry_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The write path must use ``os.replace`` (atomic on POSIX) — a
    naive ``open(...).write`` would expose a half-written file to
    concurrent readers and to the systemd-launcher's ``jq -er`` parse.
    """
    reg.register("alpha", "/tmp/alpha")

    real_replace = os.replace
    seen: list[tuple] = []

    def spy_replace(src, dst, *a, **kw):
        seen.append((str(src), str(dst)))
        return real_replace(src, dst, *a, **kw)

    monkeypatch.setattr("os.replace", spy_replace)

    reg.register("beta", "/tmp/beta")

    assert any(
        Path(dst) == registry_path for _src, dst in seen
    ), (
        "register() did not publish via os.replace(tmp, registry_path) "
        "— writes are not atomic"
    )


# ── 6. Sidecar lockfile inode stability ─────────────────────────────


def test_lockfile_inode_stable_across_rewrites(reg, registry_path: Path) -> None:
    """The sidecar lockfile's inode MUST NOT change across writes —
    if it did, two writers entering concurrently would each flock a
    different inode and the serialisation guarantee would silently
    break. The registry file itself is replaced via ``os.replace``
    (new inode every time), which is why the lock lives on a sidecar.
    """
    reg.register("alpha", "/tmp/alpha")
    lockfile = registry_path.with_name(registry_path.name + ".lock")
    inode_before = lockfile.stat().st_ino

    for i in range(5):
        reg.register(f"p{i}", f"/tmp/p{i}")
        reg.unregister(f"p{i}")

    inode_after = lockfile.stat().st_ino
    assert inode_before == inode_after, (
        "sidecar lockfile inode changed — flock serialisation broken"
    )

    # And the registry file's inode DOES change (proves it's being
    # atomically replaced rather than truncated-in-place).
    # We need at least one mutating call after the first stat to
    # make the comparison meaningful.
    registry_inode_before = registry_path.stat().st_ino
    reg.register("late", "/tmp/late")
    registry_inode_after = registry_path.stat().st_ino
    assert registry_inode_before != registry_inode_after, (
        "registry file inode unchanged after a write — register() "
        "may be truncating in place instead of using os.replace"
    )


# ── 7. Nested-shape data model + aliases (Phase 1b) ─────────────────


def test_legacy_flat_shape_reads_with_empty_aliases(
    reg, registry_path: Path,
) -> None:
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(
        json.dumps({"alpha": "/tmp/alpha", "beta": "/tmp/beta"})
    )
    rows = reg.list()
    assert {r["name"] for r in rows} == {"alpha", "beta"}
    for row in rows:
        assert row["aliases"] == []


def test_nested_shape_reads_with_populated_aliases(
    reg, registry_path: Path,
) -> None:
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    future = _iso(datetime.now(UTC) + timedelta(days=10))
    registry_path.write_text(
        json.dumps(
            {
                "alpha": {
                    "workspace": "/tmp/alpha",
                    "aliases": [{"name": "older", "expires_at": future}],
                }
            }
        )
    )
    row = reg.get("alpha")
    assert row["workspace"] == "/tmp/alpha"
    assert row["aliases"] == [{"name": "older", "expires_at": future}]


def test_read_only_does_not_rewrite_legacy_file(
    reg, registry_path: Path,
) -> None:
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    original = json.dumps({"alpha": "/tmp/alpha"})
    registry_path.write_text(original)

    reg.list()
    reg.get("alpha")
    reg.resolve_alias("nope")

    # File contents must be byte-identical (no auto-upgrade on read).
    assert registry_path.read_text() == original


def test_write_upgrades_legacy_file_to_nested_shape(
    reg, registry_path: Path,
) -> None:
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(json.dumps({"alpha": "/tmp/alpha"}))

    reg.register("beta", "/tmp/beta")  # any write triggers upgrade

    data = json.loads(registry_path.read_text())
    # The legacy "alpha" entry is upgraded to the nested shape but NOT
    # stamped with backend_impl -- only a fresh _make_record() write
    # (a real register(), like "beta" below) persists that field; a
    # bare shape-upgrade via _coerce_to_record leaves absent keys
    # absent (read-time defaulting is _materialise()'s job, per that
    # method's docstring).
    assert data["alpha"] == {"workspace": "/tmp/alpha", "aliases": []}
    assert data["beta"] == {
        "workspace": "/tmp/beta", "aliases": [], "backend_impl": "python",
    }


def test_add_alias_default_expiry_is_30_days(reg) -> None:
    reg.register("alpha", "/tmp/alpha")
    before = datetime.now(UTC)
    reg.add_alias("alpha", "ancient")
    after = datetime.now(UTC)
    row = reg.get("alpha")
    assert len(row["aliases"]) == 1
    expires = datetime.fromisoformat(row["aliases"][0]["expires_at"])
    assert before + timedelta(days=30) - timedelta(minutes=1) <= expires
    assert expires <= after + timedelta(days=30) + timedelta(minutes=1)


def test_add_alias_rejects_collision_with_real_name(reg) -> None:
    reg.register("alpha", "/tmp/alpha")
    reg.register("beta", "/tmp/beta")
    with pytest.raises(ValueError):
        reg.add_alias("alpha", "beta")


def test_add_alias_rejects_collision_with_active_alias(reg) -> None:
    reg.register("alpha", "/tmp/alpha")
    reg.register("beta", "/tmp/beta")
    reg.add_alias("alpha", "shared")
    with pytest.raises(ValueError):
        reg.add_alias("beta", "shared")


def test_add_alias_rejects_bad_slug(reg) -> None:
    reg.register("alpha", "/tmp/alpha")
    with pytest.raises(ValueError):
        reg.add_alias("alpha", "Bad_Slug")


def test_resolve_alias_returns_name_for_matching_alias(reg) -> None:
    reg.register("real", "/tmp/real")
    reg.add_alias("real", "old")
    assert reg.resolve_alias("old") == "real"
    assert reg.resolve_alias("not-an-alias") is None


def test_resolve_alias_skips_expired(reg) -> None:
    reg.register("real", "/tmp/real")
    past = _iso(datetime.now(UTC) - timedelta(seconds=1))
    reg.add_alias("real", "stale", expires_at=past)
    assert reg.resolve_alias("stale") is None


def test_expire_alias_removes_entry(reg) -> None:
    reg.register("real", "/tmp/real")
    reg.add_alias("real", "old")
    reg.expire_alias("real", "old")
    assert reg.get("real")["aliases"] == []


def test_rename_keeps_old_name_as_alias(reg) -> None:
    reg.register("old", "/tmp/work")
    reg.rename("old", "new", grace_days=14)

    assert reg.get("old") is None
    row = reg.get("new")
    # ``rename()`` never physically moves anything on disk — that's
    # still the caller's (router endpoint's) job. But R9-F5: the field
    # DOES follow the naming convention `<parent>/<name>` when the
    # CURRENT value already matches it (see
    # test_rename_updates_workspace_field_when_it_follows_naming_convention
    # below). Here it deliberately does NOT: "/tmp/work"'s basename is
    # "work", not "old", so this is the "custom, non-conventional
    # workspace" case, and the field is left untouched — exactly
    # mirroring the router endpoint's own decision not to move such a
    # directory.
    assert row["workspace"] == "/tmp/work"
    assert len(row["aliases"]) == 1
    assert row["aliases"][0]["name"] == "old"
    assert reg.resolve_alias("old") == "new"


def test_rename_updates_workspace_field_when_it_follows_naming_convention(
    reg,
) -> None:
    """R9-F5: when the registered workspace basename matches the
    project's CURRENT name (the invariant every project created via
    the create-project endpoint satisfies — workspace is always
    ``DEFAULT_WORKSPACE_PARENT / <name>``), ``rename()`` must keep the
    ``"workspace"`` field's basename in sync too, not freeze it at
    creation time.

    Without this, a SECOND rename desyncs the registry from the
    filesystem forever: the router endpoint's directory-move guard
    trusts this field to know whether ``old_name`` still names the
    directory on disk, and a stale field makes that guard silently
    skip the move on every rename after the first while still
    reporting success (R9-F5, HIGH, live-reproduced against vm-dev).
    """
    reg.register("alpha", "/srv/projects/alpha")

    # grace_days=0 so each parked alias expires immediately — renaming
    # back to a still-active alias's name is a separate, intentional
    # 409 (AliasCollision) unrelated to the workspace-tracking bug
    # under test here.
    reg.rename("alpha", "beta", grace_days=0)
    assert reg.get("beta")["workspace"] == "/srv/projects/beta"

    # The natural generalization: a SECOND (and third) rename must
    # keep composing correctly, not just the first.
    reg.rename("beta", "gamma", grace_days=0)
    assert reg.get("gamma")["workspace"] == "/srv/projects/gamma"

    reg.rename("gamma", "alpha", grace_days=0)
    assert reg.get("alpha")["workspace"] == "/srv/projects/alpha"


def test_rename_rejects_when_new_name_exists(reg) -> None:
    reg.register("alpha", "/tmp/alpha")
    reg.register("beta", "/tmp/beta")
    with pytest.raises(ValueError):
        reg.rename("alpha", "beta")


def test_rename_rejects_unknown_old_name(reg) -> None:
    with pytest.raises(KeyError):
        reg.rename("nope", "yep")


# ── PF-R37-1: registry mutation failures carry a typed identity ──────
#
# Each signal subclasses the built-in it replaces, so every existing
# ``except (ValueError, KeyError)`` / ``pytest.raises(ValueError)`` keeps
# catching it — the typing is purely additive and lets a handler map each
# failure mode to its correct 404 / 409 / 400 instead of collapsing the
# whole group to a 500.


def test_rename_new_name_taken_raises_project_name_taken(reg) -> None:
    from agent_mcp.router import project_registry as pr

    reg.register("alpha", "/tmp/alpha")
    reg.register("beta", "/tmp/beta")
    with pytest.raises(pr.ProjectNameTaken):
        reg.rename("alpha", "beta")
    assert issubclass(pr.ProjectNameTaken, ValueError)


def test_rename_unknown_old_name_raises_unknown_project(reg) -> None:
    from agent_mcp.router import project_registry as pr

    with pytest.raises(pr.UnknownProject):
        reg.rename("nope", "yep")
    assert issubclass(pr.UnknownProject, KeyError)


def test_rename_new_name_active_alias_raises_alias_collision(reg) -> None:
    from agent_mcp.router import project_registry as pr

    reg.register("alpha", "/tmp/alpha")
    reg.register("beta", "/tmp/beta")
    reg.add_alias("beta", "shared")
    with pytest.raises(pr.AliasCollision):
        reg.rename("alpha", "shared")


def test_rename_invalid_new_name_raises_invalid_name(reg) -> None:
    from agent_mcp.router import project_registry as pr

    reg.register("alpha", "/tmp/alpha")
    with pytest.raises(pr.InvalidName):
        reg.rename("alpha", "Bad_Slug")


def test_register_conflict_and_alias_raise_typed(reg) -> None:
    from agent_mcp.router import project_registry as pr

    reg.register("alpha", "/tmp/alpha")
    # Re-register at a different workspace → ProjectNameTaken.
    with pytest.raises(pr.ProjectNameTaken):
        reg.register("alpha", "/tmp/other")
    # Claiming a name that is an active alias of another project.
    reg.register("beta", "/tmp/beta")
    reg.add_alias("beta", "aliased")
    with pytest.raises(pr.AliasCollision):
        reg.register("aliased", "/tmp/aliased")


def test_add_alias_typed_failures(reg) -> None:
    from agent_mcp.router import project_registry as pr

    reg.register("alpha", "/tmp/alpha")
    reg.register("beta", "/tmp/beta")
    with pytest.raises(pr.UnknownProject):
        reg.add_alias("ghost", "x")
    with pytest.raises(pr.InvalidName):
        reg.add_alias("alpha", "Bad_Slug")
    with pytest.raises(pr.ProjectNameTaken):
        reg.add_alias("alpha", "beta")  # collides with a real project name
    reg.add_alias("beta", "shared")
    with pytest.raises(pr.AliasCollision):
        reg.add_alias("alpha", "shared")  # active alias of beta


# ── backend_impl (Phase D1 canary-cutover flag) ──────────────────────
#
# `register()` accepts a `backend_impl` keyword (defaulting to
# "python") that CREATE persists into the record; `set_backend_impl()`
# is the separate mutator for flipping an EXISTING project (mirroring
# add_alias/rename's "register() is create/reconfirm, mutation gets
# its own method" split — register()'s idempotent-reregister path
# returns the existing row untouched, so it must not silently apply a
# new backend_impl on re-registration either).


def test_register_defaults_backend_impl_to_python(reg) -> None:
    row = reg.register("alpha", "/tmp/alpha")
    assert row["backend_impl"] == "python"


def test_register_persists_an_explicit_backend_impl(reg) -> None:
    row = reg.register("alpha", "/tmp/alpha", backend_impl="rust")
    assert row["backend_impl"] == "rust"
    # Round-trips through a fresh read, not just the in-memory return.
    assert reg.get("alpha")["backend_impl"] == "rust"


def test_register_rejects_an_unrecognized_backend_impl(reg) -> None:
    with pytest.raises(ValueError):
        reg.register("alpha", "/tmp/alpha", backend_impl="cobol")


def test_reregistering_at_the_same_workspace_does_not_change_backend_impl(reg) -> None:
    reg.register("alpha", "/tmp/alpha", backend_impl="rust")
    # Idempotent re-register with NO backend_impl kwarg — must not
    # silently reset the project back to the "python" default.
    row = reg.register("alpha", "/tmp/alpha")
    assert row["backend_impl"] == "rust"


def test_pre_existing_records_with_no_backend_impl_key_read_as_python(reg, registry_path: Path) -> None:
    # Simulates a registry file written before this field existed —
    # the on-disk record has no "backend_impl" key at all.
    registry_path.write_text(
        json.dumps({"legacy": {"workspace": "/tmp/legacy", "aliases": []}})
    )
    assert reg.get("legacy")["backend_impl"] == "python"
    assert reg.list()[0]["backend_impl"] == "python"


def test_set_backend_impl_flips_an_existing_project(reg) -> None:
    reg.register("alpha", "/tmp/alpha")
    updated = reg.set_backend_impl("alpha", "rust")
    assert updated["backend_impl"] == "rust"
    assert reg.get("alpha")["backend_impl"] == "rust"


def test_set_backend_impl_preserves_workspace_and_aliases(reg) -> None:
    reg.register("alpha", "/tmp/alpha")
    reg.add_alias("alpha", "old-name")
    reg.set_backend_impl("alpha", "rust")
    row = reg.get("alpha")
    assert row["workspace"] == "/tmp/alpha"
    assert [a["name"] for a in row["aliases"]] == ["old-name"]


def test_set_backend_impl_unknown_project_raises_unknown_project(reg) -> None:
    from agent_mcp.router import project_registry as pr

    with pytest.raises(pr.UnknownProject):
        reg.set_backend_impl("ghost", "rust")


def test_set_backend_impl_rejects_an_unrecognized_value(reg) -> None:
    reg.register("alpha", "/tmp/alpha")
    with pytest.raises(ValueError):
        reg.set_backend_impl("alpha", "cobol")
