# ── MOVED-UPSTREAM SOURCE ───────────────────────────────────────────
# Was: nixos-developer-system/users/dennis/agent-mcp/project_registry.py
# Now: agent_mcp/router/project_registry.py — moved upstream in Phase
# 1a of the router-upstream plan (prancy-napping-pie). Imported as a
# sibling module from agent_mcp.router.app.
# ────────────────────────────────────────────────────────────────────
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

On-disk shape (Phase 1b update). Two shapes are accepted on READ:

  Legacy (pre-Phase-1b)::

      {"<name>": "<workspace-string>", ...}

  Nested (current)::

      {"<name>": {"workspace": "<path>",
                  "aliases": [{"name": "<old>", "expires_at": "<iso>"}],
                  **extra},
       ...}

The legacy shape is upgraded lazily: a read returns synthesised
records (`aliases: []`) without touching the file, and any write
rewrites the whole file in the nested shape. This keeps a router
that boots-and-idles from mutating the operator's file out from
under them, while ensuring any registration / alias / rename touch
upgrades on the way out. The bash launcher
(`users/dennis/agent-mcp/default.nix`'s `agentMcpLauncher`) used
the legacy shape via `jq -er '.[$n]'`, expecting a plain string back;
since the launcher reads via the same Python registry now (the
router shells out to nothing), the nested shape is the steady state.

Any `extra` kwargs passed to `register()` are accepted at the API
boundary but are NOT persisted today — there's no in-tree consumer
for them yet, and we'd rather defer the on-disk-shape migration
(plus the matching launcher change) to a separate change with its
own commit + deploy + rollback story.

Public surface — see class docstrings for the contract:

    Project          : TypedDict {"name", "workspace", "aliases", **extra}
    Alias            : TypedDict {"name": str, "expires_at": str}
    ProjectRegistry  : list / get / register / unregister
                      add_alias / expire_alias / resolve_alias / rename
    REGISTRY_PATH    : module-level Path, overridable in tests
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TypedDict

__all__ = [
    "Alias",
    "AliasCollision",
    "InvalidName",
    "Project",
    "ProjectNameTaken",
    "ProjectRegistry",
    "RegistryError",
    "REGISTRY_PATH",
    "UnknownProject",
]


log = logging.getLogger(__name__)


# ── Typed mutation failures ─────────────────────────────────────────
#
# PF-R37-1 (500-hygiene / error-mapping class): the registry's write
# methods (``register`` / ``add_alias`` / ``rename``) previously signalled
# every semantically-distinct failure with a bare ``ValueError`` /
# ``KeyError`` whose meaning was carried ONLY in the message string. The
# REST handlers then had to string-match (fragile) or, worse, collapse the
# whole group to a single HTTP status — ``rename_project_handler`` mapped
# EVERY ``(ValueError, KeyError)`` to a 500, so a concurrent rename to an
# already-taken name (the atomic "already registered" guard) surfaced a
# 500 instead of a 409.
#
# These typed exceptions give each failure mode a stable identity a handler
# can map to the correct 404 / 409 / 400. They SUBCLASS the built-ins they
# replace (``UnknownProject`` is a ``KeyError``; the rest are ``ValueError``)
# so every existing ``except ValueError`` / ``except KeyError`` / bare
# ``pytest.raises(ValueError)`` keeps catching them unchanged — the typing
# is purely additive. A handler catches the specific subclass for a precise
# status and reserves a bare-``ValueError``/``KeyError`` backstop for a
# genuine internal error → 500.
class RegistryError(Exception):
    """Base for a registry mutation refused for a *semantic* reason
    (name taken, unknown project, alias collision, invalid name) — as
    opposed to a genuine internal fault. Handlers map these to 4xx and
    reserve 500 for everything else."""


class UnknownProject(RegistryError, KeyError):
    """The named project isn't registered → HTTP 404. Subclasses
    ``KeyError`` so callers doing ``except KeyError`` still catch it."""


class InvalidName(RegistryError, ValueError):
    """A project / alias name failed slug validation → HTTP 400."""


class ProjectNameTaken(RegistryError, ValueError):
    """The target name is already a REGISTERED project → HTTP 409.
    Distinct from ``AliasCollision``: this is a real project, not a
    grace-period alias."""


class AliasCollision(RegistryError, ValueError):
    """The target name is a currently-ACTIVE alias of another project →
    HTTP 409. An expired alias is reclaimable and never raises this."""


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


# Slug regex used to validate project names AND alias names. Kept in
# sync with the router-side `_SLUG_RE` in agent_mcp/router/app.py —
# duplicated here so the registry can validate aliases without a
# circular import.
_SLUG_RE = re.compile(r"^[a-z](?:[a-z0-9-]*[a-z0-9])?$")

# Default grace period for `add_alias` when the caller doesn't pass
# `expires_at`. The plan (decision #4) locks this at 30 days; we
# expose it as a module constant so tests / operators can tune.
DEFAULT_ALIAS_GRACE_DAYS: int = 30


class Alias(TypedDict):
    """One alias entry — a name that resolves to the parent project
    until `expires_at` passes. Stored ISO-8601 UTC with a trailing Z."""

    name: str
    expires_at: str


class Project(TypedDict, total=False):
    """One project record. `name`, `workspace`, and `aliases` are
    always populated by the registry's reader; any other keys are
    passed through opaque so the dashboard can stash e.g. `created_at`
    without this module needing a schema bump."""

    name: str
    workspace: str
    aliases: list[Alias]


def _now_iso() -> str:
    """ISO-8601 UTC seconds-precision, with a trailing Z. The format
    aliases are stored in on disk."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s: str) -> datetime:
    """Parse the format produced by `_now_iso()` plus any tz-aware
    ISO string Python accepts. Used by `resolve_alias` to filter out
    past-due aliases."""
    # `datetime.fromisoformat` understands a trailing `+00:00` on
    # 3.10+, but the `Z` shorthand requires 3.11. Strip it.
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


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
                    raise ProjectNameTaken(
                        f"project {name!r} is already registered at "
                        f"{existing_row['workspace']!r}; refusing to "
                        f"re-point at {workspace!r}"
                    )
                # Idempotent path — return the existing row untouched
                # rather than rewriting the file.
                return existing_row

            # BL-R33-1 (chokepoint defense-in-depth): refuse to claim a
            # name that is a currently-active grace-period alias of
            # ANOTHER project. ``resolve()`` returns a real project before
            # falling back to an alias, so registering a real project on an
            # active-alias name would silently SHADOW the alias and redirect
            # legacy clients into a different project/DB. The create HANDLER
            # returns a proper 409 ALIAS_COLLISION envelope before reaching
            # here; this is the single chokepoint every create funnels
            # through, so guarding it makes any future create path inherit
            # the invariant. Scanned INLINE (mirroring ``rename()``'s
            # active-alias scan below) rather than via ``resolve_alias`` —
            # that method re-opens the sidecar for LOCK_SH and would
            # self-deadlock against the LOCK_EX we already hold here. Only
            # fires for a NEW registration (the idempotent re-register above
            # already returned), and an EXPIRED alias is skipped, so a
            # past-due name stays reclaimable.
            now = datetime.now(timezone.utc)
            for other_name, payload in data.items():
                for entry in self._aliases_of(payload):
                    if entry.get("name") != name:
                        continue
                    try:
                        exp = _parse_iso(entry["expires_at"])
                    except (KeyError, ValueError):
                        continue
                    if exp > now:
                        raise AliasCollision(
                            f"name {name!r} is already an active alias "
                            f"for project {other_name!r}"
                        )

            data[name] = self._make_record(workspace, aliases=[])
            self._write_locked(fd, self._normalise_for_write(data))
            return self._materialise(name, data[name])

    def unregister(self, name: str) -> None:
        """Drop `name` from the registry. No-op if not present."""
        with self._lock_for_write() as (fd, data):
            if name not in data:
                return
            data.pop(name)
            self._write_locked(fd, self._normalise_for_write(data))

    # ── Alias API (Phase 1b) ───────────────────────────────────────

    def add_alias(
        self,
        name: str,
        alias: str,
        expires_at: str | None = None,
        *,
        grace_days: int | None = None,
    ) -> None:
        """Append `alias` to `name`'s alias list with the given expiry.

        Default expiry is `now + DEFAULT_ALIAS_GRACE_DAYS` (30 days).
        Callers can pass either ``expires_at`` (an ISO-8601 string) or
        ``grace_days`` (an int); ``grace_days`` wins if both are
        supplied. ``grace_days`` mirrors ``rename()``'s knob so the
        orchestrator can express "alias is dead on arrival, let the
        sweeper clean it up" by passing ``grace_days=0``.

        Validation under LOCK_EX:

          * `alias` must match the project-name slug regex.
          * `alias` must not collide with any existing project name
            *if* the resulting alias would still be active when
            written. An already-expired alias (``grace_days=0`` or an
            ``expires_at`` in the past) is allowed even when the name
            collides, because the next ``alias_expiry_tick`` will
            evict it before it could ever shadow the real project.
          * `alias` must not collide with any *currently active*
            alias for some other project. (Expired aliases are
            allowed to be reclaimed — `resolve_alias` would never
            return them anyway, so reclamation is safe and useful
            after a long-overdue cleanup.)

        Raises (typed subclasses of the built-ins, so existing
        ``except (KeyError, ValueError)`` still catch them):
            UnknownProject (KeyError): `name` is not a registered project.
            InvalidName (ValueError): `alias` fails slug validation.
            ProjectNameTaken (ValueError): `alias` collides with a real
                project name (and is not dead-on-arrival expired).
            AliasCollision (ValueError): `alias` is a currently-active
                alias of some other project.
        """
        if not _SLUG_RE.match(alias):
            raise InvalidName(
                f"alias {alias!r} is not a valid slug — must match "
                f"{_SLUG_RE.pattern}"
            )

        if grace_days is not None:
            expires_at = (
                datetime.now(timezone.utc)
                + timedelta(days=grace_days)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
        if expires_at is None:
            expires_at = (
                datetime.now(timezone.utc)
                + timedelta(days=DEFAULT_ALIAS_GRACE_DAYS)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Whether the alias being added is already past its expiry —
        # if so, it's dead on arrival and the collision check below
        # can safely skip it (the next reaper tick evicts it).
        try:
            already_expired = (
                _parse_iso(expires_at) <= datetime.now(timezone.utc)
            )
        except (TypeError, ValueError):
            already_expired = False

        with self._lock_for_write() as (fd, data):
            if name not in data:
                raise UnknownProject(name)

            if alias in data and not already_expired:
                raise ProjectNameTaken(
                    f"alias {alias!r} collides with a real project name"
                )

            now = datetime.now(timezone.utc)
            for other_name, payload in data.items():
                if other_name == name:
                    continue
                for entry in self._aliases_of(payload):
                    if entry["name"] != alias:
                        continue
                    try:
                        entry_exp = _parse_iso(entry["expires_at"])
                    except (KeyError, ValueError):
                        # Malformed entry — treat as expired.
                        continue
                    if entry_exp > now:
                        raise AliasCollision(
                            f"alias {alias!r} is already an active alias "
                            f"for project {other_name!r}"
                        )

            record = self._coerce_to_record(data[name])
            record["aliases"].append(
                {"name": alias, "expires_at": expires_at}
            )
            data[name] = record
            self._write_locked(fd, self._normalise_for_write(data))

    def expire_alias(self, name: str, alias: str) -> None:
        """Remove `alias` from `name`'s alias list. No-op if absent."""
        with self._lock_for_write() as (fd, data):
            if name not in data:
                return
            record = self._coerce_to_record(data[name])
            record["aliases"] = [
                a for a in record["aliases"] if a.get("name") != alias
            ]
            data[name] = record
            self._write_locked(fd, self._normalise_for_write(data))

    def resolve_alias(self, maybe_alias: str) -> str | None:
        """If `maybe_alias` matches a non-expired alias of some
        project, return that project's real name. Otherwise None.

        O(N) over registered projects — N is small (per-host single-
        digit to low-double-digit), and the read happens behind the
        existing per-request snapshot the router already does, so
        the cost is invisible.
        """
        data = self._read_locked()
        now = datetime.now(timezone.utc)
        for real_name, payload in data.items():
            for entry in self._aliases_of(payload):
                if entry.get("name") != maybe_alias:
                    continue
                try:
                    exp = _parse_iso(entry["expires_at"])
                except (KeyError, ValueError):
                    continue
                if exp > now:
                    return real_name
        return None

    def rename(
        self,
        old_name: str,
        new_name: str,
        grace_days: int = DEFAULT_ALIAS_GRACE_DAYS,
    ) -> None:
        """Atomically rename `old_name` to `new_name`, parking
        `old_name` as a grace-period alias on the new record.

        Does **not** move the workspace directory on disk nor restart
        any systemd unit — that's the calling endpoint's job
        (`agent_mcp.router.app.rename_handler`). This method only
        rewrites the registry file.

        R9-F5: it DOES, however, keep the record's `"workspace"` field
        tracking the rename whenever that field still follows the
        `<parent>/<name>` naming convention every project created via
        `register()` satisfies (i.e. `Path(workspace).name == old_name`)
        — the same check the calling endpoint uses to decide whether to
        physically move the directory. Without this, `"workspace"`
        froze at whatever value was set at CREATE time and never
        tracked ANY rename, so a project's second rename would silently
        desync the registry from the filesystem forever: the endpoint's
        move-guard trusts this field to know whether `old_name` still
        names the directory on disk, and a stale field makes that guard
        false on every rename after the first, silently skipping the
        `os.rename()` while still reporting success. Updating the field
        here — using the exact same naming-convention test — keeps it
        correct across an arbitrary number of renames, which matters
        beyond this module: the `agent-mcp-launcher` shell script
        (`nix/packages.nix`) resolves a running project's workspace
        directory straight out of this same registry file's
        `"workspace"` field, not from any Python-side cache. When the
        field doesn't follow the naming convention (a workspace
        registered at a custom, non-conventional path), it's left
        untouched, exactly mirroring the endpoint's own decision not to
        move such a directory.

        Raises (all subclass the built-in they replace, so existing
        ``except KeyError`` / ``except ValueError`` catch them unchanged):
            UnknownProject (KeyError): `old_name` isn't registered → 404.
            ProjectNameTaken (ValueError): `new_name` is a registered
                project → 409.
            AliasCollision (ValueError): `new_name` is a currently-active
                alias of another project → 409.
            InvalidName (ValueError): `new_name` fails slug validation → 400.
        """
        if not _SLUG_RE.match(new_name):
            raise InvalidName(
                f"new name {new_name!r} is not a valid slug — must match "
                f"{_SLUG_RE.pattern}"
            )

        expires_at = (
            datetime.now(timezone.utc) + timedelta(days=grace_days)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

        with self._lock_for_write() as (fd, data):
            if old_name not in data:
                raise UnknownProject(old_name)
            if new_name in data:
                raise ProjectNameTaken(
                    f"project {new_name!r} is already registered"
                )

            now = datetime.now(timezone.utc)
            for other_name, payload in data.items():
                if other_name == old_name:
                    continue
                for entry in self._aliases_of(payload):
                    if entry.get("name") != new_name:
                        continue
                    try:
                        exp = _parse_iso(entry["expires_at"])
                    except (KeyError, ValueError):
                        continue
                    if exp > now:
                        raise AliasCollision(
                            f"name {new_name!r} is already an active "
                            f"alias for project {other_name!r}"
                        )

            record = self._coerce_to_record(data[old_name])
            # R9-F5: re-derive the workspace path from the CURRENT
            # value using the same naming-convention test the calling
            # endpoint applies before physically moving the directory
            # (`Path(workspace).name == old_name`). This is what keeps
            # the field from freezing at creation time — every rename
            # re-applies the same transformation the previous rename
            # (if any) already applied, so it composes correctly across
            # an arbitrary number of renames instead of only the first.
            old_workspace = record.get("workspace", "")
            if old_workspace and Path(old_workspace).name == old_name:
                record["workspace"] = str(Path(old_workspace).with_name(new_name))
            record["aliases"].append(
                {"name": old_name, "expires_at": expires_at}
            )
            data[new_name] = record
            del data[old_name]
            self._write_locked(fd, self._normalise_for_write(data))

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

    # ── Shape normalisation ────────────────────────────────────────

    @staticmethod
    def _make_record(workspace: str, *, aliases: list[Alias]) -> dict[str, Any]:
        """Build a fresh nested-shape record for `register()`."""
        return {"workspace": workspace, "aliases": list(aliases)}

    @staticmethod
    def _coerce_to_record(payload: Any) -> dict[str, Any]:
        """Return a mutable nested-shape record for `payload`.

        Accepts either a legacy string payload OR an existing nested
        dict; returns a copy with `workspace` and `aliases` guaranteed
        present, ready for mutation.
        """
        if isinstance(payload, str):
            return {"workspace": payload, "aliases": []}
        if isinstance(payload, dict):
            workspace = payload.get("workspace", "")
            aliases = payload.get("aliases") or []
            if not isinstance(aliases, list):
                aliases = []
            # Shallow copy keeps any opaque extras the caller stuffed
            # in the dict, while presenting a tidy `aliases` list.
            out: dict[str, Any] = {
                k: v for k, v in payload.items()
                if k not in ("workspace", "aliases")
            }
            out["workspace"] = workspace
            out["aliases"] = list(aliases)
            return out
        return {"workspace": "", "aliases": []}

    @staticmethod
    def _aliases_of(payload: Any) -> list[dict[str, Any]]:
        """Best-effort: return the aliases list embedded in `payload`,
        accepting either the legacy string shape (→ no aliases) or
        the nested shape."""
        if isinstance(payload, dict):
            aliases = payload.get("aliases") or []
            if isinstance(aliases, list):
                return [a for a in aliases if isinstance(a, dict)]
        return []

    @classmethod
    def _normalise_for_write(cls, data: dict[str, Any]) -> dict[str, Any]:
        """Coerce every value in `data` to the nested-shape record so
        any in-memory legacy-string entries get upgraded on write."""
        return {
            name: cls._coerce_to_record(payload)
            for name, payload in data.items()
        }

    # ── Internals: row materialisation ─────────────────────────────

    @staticmethod
    def _materialise(name: str, payload: Any) -> Project:
        """Coerce a raw row into the Project TypedDict shape.

        Accepts both the new shape (`{"workspace": ..., "aliases":
        [...]}, **extra}`) and the legacy shape (`"<workspace-string>"`).
        Always returns a dict carrying `name` AND `aliases` so callers
        don't need to handle the legacy-flat case downstream.
        """
        if isinstance(payload, str):
            row: dict[str, Any] = {
                "name": name,
                "workspace": payload,
                "aliases": [],
            }
            return row  # type: ignore[return-value]
        if isinstance(payload, dict):
            aliases = payload.get("aliases") or []
            if not isinstance(aliases, list):
                aliases = []
            row = {
                k: v for k, v in payload.items()
                if k not in ("workspace", "aliases")
            }
            row["name"] = name
            row["workspace"] = payload.get("workspace", "")
            row["aliases"] = [
                {"name": a.get("name", ""), "expires_at": a.get("expires_at", "")}
                for a in aliases
                if isinstance(a, dict)
            ]
            return row  # type: ignore[return-value]
        # Defensive: unexpected payload type → treat as empty workspace.
        # Don't raise: the registry is read on hot paths and we'd rather
        # the operator see a broken-looking row than a 500.
        return {  # type: ignore[return-value]
            "name": name, "workspace": "", "aliases": [],
        }
