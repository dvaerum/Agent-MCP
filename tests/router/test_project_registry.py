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

import os
import threading
import time
from pathlib import Path

import pytest


@pytest.fixture
def registry_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Repoint the registry's global REGISTRY_PATH at a tmp file."""
    import project_registry

    path = tmp_path / "projects.local.json"
    monkeypatch.setattr(project_registry, "REGISTRY_PATH", path)
    return path


@pytest.fixture
def reg(registry_path: Path):
    import project_registry

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
