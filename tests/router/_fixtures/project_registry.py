# ── ONE-TIME VENDORED COPY ───────────────────────────────────────────
# This file is a verbatim snapshot of
# /home/dennis/nixos-developer-system/users/dennis/agent-mcp/project_registry.py
# (deploy repo), pulled in at Phase 0 of the router-upstream plan
# (prancy-napping-pie). Its sole purpose is to give the pytest suite
# something concrete to assert against while the source still lives in
# the deploy repo. It is INTENTIONALLY identical to the source — do
# NOT edit. If the deploy file changes, this snapshot stays stale until
# Phase 1, which deletes this entire `_fixtures/` directory and moves
# router.py + project_registry.py to their permanent in-tree home.
# ─────────────────────────────────────────────────────────────────────
"""Pure JSON project registry — the locking store backing the file
`~/.config/agent-mcp/projects.local.json` that the router consults on
every request and the dashboard mutates via __create / __unregister.

Candidate B of the 2026-06-01 architecture review locked this as the
*only* abstraction over that file. It does NOT own systemd, sockets,
or lifecycle — those stay in router.py. The router calls this for
read/list/register/unregister and nothing else.

Locking strategy: fcntl.LOCK_EX on every write, LOCK_SH on every
read. Concurrent reads are allowed; writers serialise behind each
other AND behind any in-flight reader. The lock is held on a
SIDECAR lockfile (`<path>.lock`) that is never renamed — because
the write path uses `os.replace(tmp, path)` to publish atomically,
locking the registry file itself would race: two writers opening
before the first replace each hold flock on a different inode, so
no serialisation would happen. The sidecar's inode is stable, so
every flock on `<path>.lock` is on the same kernel lock object.

Corrupt JSON recovery: if `json.loads` raises on the current file,
we rename it to `projects.local.json.corrupt-<ISO8601 UTC>` and start
fresh with an empty mapping. A loud `logging.warning` is emitted so
the operator can fish the carcass out of `~/.config/agent-mcp/` and
hand-merge if desired. This is the one place where data loss is
possible — but the alternative (refusing to start, or silently
returning whatever subset parses) leaves the dashboard wedged with
no recovery path.

File-on-disk shape: the historical `{name: "<workspace-string>"}`
flat mapping. We preserve this shape on disk because the bash
launcher (`users/dennis/agent-mcp/default.nix`'s `agentMcpLauncher`)
reads it via `jq -er '.[$n]'` and expects a plain string back; a
nested `{workspace: ...}` shape would break the launcher silently
and the systemd template would refuse to start any project.

Any `extra` kwargs passed to `register()` are accepted at the API
boundary but are NOT persisted today — there's no in-tree consumer
for them yet, and we'd rather defer the on-disk-shape migration
(plus the matching launcher change) to a separate change with its
own commit + deploy + rollback story.

Public surface — see class docstrings for the contract:

    Project          : TypedDict {"name", "workspace", **extra}
    ProjectRegistry  : list / get / register / unregister
    REGISTRY_PATH    : module-level Path, overridable in tests
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypedDict

__all__ = ["Project", "ProjectRegistry", "REGISTRY_PATH"]


log = logging.getLogger(__name__)


# Default location. Overridable per-instance (constructor takes
# `path=`) or globally by reassigning this module attribute (tests
# do this). The router reads it via `ProjectRegistry()` at the
# module top so a single shared default suffices in production.
REGISTRY_PATH: Path = Path(
    os.environ.get(
        "AGENT_MCP_PROJECTS_FILE",
        str(Path.home() / ".config" / "agent-mcp" / "projects.local.json"),
    )
)


class Project(TypedDict, total=False):
    """One project record. `name` and `workspace` are required; any
    other keys are passed through opaque so the dashboard can stash
    e.g. `created_at` without this module needing a schema bump."""

    name: str
    workspace: str


class ProjectRegistry:
    """Thread- and process-safe accessor for the projects JSON file.

    Cheap to construct (no I/O in __init__). Each method opens the
    file, takes the appropriate POSIX advisory lock via fcntl.flock,
    does its work, and releases.  Designed for callers that don't
    cache the instance — `ProjectRegistry().list()` per request is
    fine; the lock is fast, and fresh-read-per-call sidesteps cache
    coherency concerns when multiple processes (router +
    agent-mcp-launcher) share the file.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path  # None → resolve via the module global on use

    # ── Path resolution ────────────────────────────────────────────
    #
    # Done lazily so tests can monkeypatch REGISTRY_PATH after the
    # ProjectRegistry is already constructed (the router constructs
    # one at module import time).

    @property
    def path(self) -> Path:
        return self._path if self._path is not None else REGISTRY_PATH

    @property
    def lock_path(self) -> Path:
        """Sidecar lockfile — see module docstring for why a sidecar."""
        return self.path.with_name(self.path.name + ".lock")

    def _open_lock(self, mode: int) -> int:
        """Create-if-missing the sidecar, take `mode` (LOCK_SH/LOCK_EX),
        return the fd. Caller owns the fd and must unlock+close."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        fcntl.flock(fd, mode)
        return fd

    @staticmethod
    def _release(fd: int) -> None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    # ── Public API ─────────────────────────────────────────────────

    def list(self) -> list[Project]:
        """Return every registered project, sorted by name."""
        data = self._read_locked()
        return [
            self._materialise(name, payload)
            for name, payload in sorted(data.items())
        ]

    def get(self, name: str) -> Project | None:
        """Return the project record for `name`, or None if unknown."""
        data = self._read_locked()
        payload = data.get(name)
        if payload is None:
            return None
        return self._materialise(name, payload)

    def register(self, name: str, workspace: str, **extra: Any) -> Project:
        """Register (or re-confirm) a project. Returns the stored row.

        Idempotent: re-registering with the same `workspace` is a
        no-op and returns the existing row. Re-registering with a
        DIFFERENT workspace raises ValueError — silently relocating
        would invisibly move the project's SQLite state.

        `extra` is currently accepted but not persisted; see the
        module docstring for why we keep the on-disk shape flat.
        """
        del extra  # reserved for a future shape migration
        with self._lock_for_write() as (fd, data):
            existing = data.get(name)
            if existing is not None:
                existing_row = self._materialise(name, existing)
                if existing_row["workspace"] != workspace:
                    raise ValueError(
                        f"project {name!r} is already registered at "
                        f"{existing_row['workspace']!r}; refusing to "
                        f"re-point at {workspace!r}"
                    )
                # Idempotent path — return the existing row untouched
                # rather than rewriting the file.
                return existing_row

            # Flat string shape: matches the legacy on-disk format
            # the bash launcher's `jq -er '.[$n]'` expects.
            data[name] = workspace
            self._write_locked(fd, data)
            return self._materialise(name, workspace)

    def unregister(self, name: str) -> None:
        """Drop `name` from the registry. No-op if not present."""
        with self._lock_for_write() as (fd, data):
            if name not in data:
                return
            data.pop(name)
            self._write_locked(fd, data)

    # ── Internals: locking, parsing, corrupt-recovery ──────────────

    def _read_locked(self) -> dict[str, Any]:
        """LOCK_SH the sidecar, read the registry, return raw mapping.

        Reads under the shared lock are blocked by any in-flight
        writer (which holds LOCK_EX on the same sidecar) but multiple
        readers can proceed in parallel. Once we hold the lock, the
        registry file's contents are stable until we release —
        writers `os.replace(tmp, path)` AFTER they've acquired
        LOCK_EX, which can't happen while LOCK_SH is held.
        """
        lock_fd = self._open_lock(fcntl.LOCK_SH)
        try:
            path = self.path
            if not path.is_file():
                return {}
            try:
                with open(path, "rb") as f:
                    raw = f.read()
            except FileNotFoundError:
                return {}
        finally:
            self._release(lock_fd)
        return self._parse_or_recover(raw)

    def _parse_or_recover(self, raw: bytes) -> dict[str, Any]:
        if not raw.strip():
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            self._handle_corrupt(raw, str(e))
            return {}
        if not isinstance(data, dict):
            self._handle_corrupt(
                raw, f"top-level JSON is {type(data).__name__}, expected dict",
            )
            return {}
        return data

    def _handle_corrupt(self, raw: bytes, reason: str) -> None:
        """Rename the corrupt file to a timestamped backup, loud-warn."""
        path = self.path
        if not path.exists():
            return
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
        backup = path.with_name(f"{path.name}.corrupt-{ts}")
        try:
            path.rename(backup)
        except OSError as e:
            log.warning(
                "project registry: could not back up corrupt %s: %s; "
                "leaving original in place. Reason for recovery: %s",
                path, e, reason,
            )
            return
        log.warning(
            "project registry: %s was unparseable (%s); moved to %s and "
            "starting fresh. Hand-merge entries back in if you can salvage.",
            path, reason, backup,
        )

    # ── Internals: write path ──────────────────────────────────────

    class _WriteCtx:
        """Hand-rolled CM: take LOCK_EX on the sidecar, read the
        registry file, hand the parsed dict to the caller, and hold
        the lock until __exit__ so the read-modify-write cycle (plus
        the eventual os.replace) is atomic w.r.t. other writers AND
        any readers (which take LOCK_SH on the same sidecar)."""

        def __init__(self, registry: "ProjectRegistry") -> None:
            self.registry = registry
            self._fd: int | None = None
            self.data: dict[str, Any] = {}

        def __enter__(self) -> tuple[int, dict[str, Any]]:
            self._fd = self.registry._open_lock(fcntl.LOCK_EX)
            path = self.registry.path
            raw = b""
            if path.is_file():
                try:
                    with open(path, "rb") as f:
                        raw = f.read()
                except FileNotFoundError:
                    raw = b""
            self.data = self.registry._parse_or_recover(raw)
            return self._fd, self.data

        def __exit__(self, exc_type, exc, tb) -> None:
            if self._fd is not None:
                self.registry._release(self._fd)

    def _lock_for_write(self) -> "ProjectRegistry._WriteCtx":
        return ProjectRegistry._WriteCtx(self)

    def _write_locked(self, lock_fd: int, data: dict[str, Any]) -> None:
        """Atomic rewrite: write tmp + fsync + rename. Caller still
        holds LOCK_EX on `lock_fd`, so concurrent readers (LOCK_SH on
        the same path) will block until we exit the with-block."""
        path = self.path
        payload = (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8")
        # tempfile.NamedTemporaryFile gives us a unique name in the
        # same directory so the rename is on the same filesystem.
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=str(path.parent),
            prefix=path.name + ".",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            tmp_path = Path(tmp.name)
            try:
                tmp.write(payload)
                tmp.flush()
                os.fsync(tmp.fileno())
            except BaseException:
                tmp_path.unlink(missing_ok=True)
                raise
        try:
            os.replace(tmp_path, path)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise

    # ── Internals: row materialisation ─────────────────────────────

    @staticmethod
    def _materialise(name: str, payload: Any) -> Project:
        """Coerce a raw row into the Project TypedDict shape.

        Accepts both the new shape (`{"workspace": ..., **extra}`)
        and the legacy shape (`"<workspace-string>"`). Always
        returns a dict carrying `name` so callers don't have to
        round-trip through the registry dict's keys.
        """
        if isinstance(payload, str):
            row: dict[str, Any] = {"name": name, "workspace": payload}
            return row  # type: ignore[return-value]
        if isinstance(payload, dict):
            row = {"name": name, **payload}
            row.setdefault("workspace", "")
            return row  # type: ignore[return-value]
        # Defensive: unexpected payload type → treat as empty workspace.
        # Don't raise: the registry is read on hot paths and we'd rather
        # the operator see a broken-looking row than a 500.
        return {"name": name, "workspace": ""}  # type: ignore[return-value]
