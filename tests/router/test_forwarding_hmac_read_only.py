"""Forwarding-HMAC key reader is strictly read-only (F015 v4).

Root cause that drove the v3 → v4 inversion:

  PRs #208-#213 had ``ensure_forwarding_hmac_key`` generate the
  per-project HMAC key, write it to disk, then invoke ``systemctl
  start agent-mcp@<name>.service``. This worked when the router
  triggered the start — but systemd's ``Restart=on-failure``
  reactivates the unit autonomously after a crash, bypassing the
  router entirely. The live VM hit a 9569-deep restart loop because
  the backend crashed once with the key missing, systemd kept
  restarting it, and the router-side self-heal (PR #213) never ran
  on any of those autonomous restarts.

  F015 v4 inverts ownership: the systemd unit's ExecStartPre
  generates the file (see ``nix/module.nix``), guaranteeing the
  invariant "file is present whenever the unit is starting"
  regardless of who triggered the start. The router becomes a
  reader — fast in-memory cache backed by a one-time disk read.

This test pins the read-only contract. Generating, writing, or
self-healing logic returning to the router is a regression we want
to catch at the test level, not in a 9569-deep restart loop.

The tests deliberately do NOT mock ``Path.exists()`` or the file
system. The bug the v3 test ``test_forwarding_hmac_self_heal.py``
hid was exactly the kind a mocked filesystem would never surface:
the live VM didn't care whether the router thought the file should
exist, only whether the file was actually on disk when the backend
ran. We write real files to a real ``tmp_path`` and observe them.
"""

from __future__ import annotations

import ast
import inspect
import os
from pathlib import Path

import pytest


# Async tests opt in individually with ``@pytest.mark.asyncio``; the
# AST/structural tests at the bottom of the file are plain sync
# functions and must NOT inherit an asyncio mark.


# ── Behavioural contract ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cache_hit_returns_cached_without_disk_read(
    router_module, router_env, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cache hit returns the cached bytes and does NOT touch the disk.

    The HMAC sign path runs on every cookie-authenticated request,
    so a disk read on the hot path would be a regression. We assert
    by monkeypatching ``Path.read_bytes`` to raise and confirming
    the cache hit short-circuits before it's called.
    """
    from agent_mcp.router import project_orchestrator as _po

    name = "cache-hit"
    expected = b"x" * 32
    _po.forwarding_hmac_keys[name] = expected

    def _explode(self: Path) -> bytes:  # pragma: no cover - guard
        raise AssertionError(
            f"cache hit must not read disk; got read_bytes({self})"
        )

    monkeypatch.setattr(Path, "read_bytes", _explode)

    assert _po.ensure_forwarding_hmac_key(name) == expected
    assert _po.get_forwarding_hmac_key(name) == expected


@pytest.mark.asyncio
async def test_cache_miss_reads_existing_file_and_populates_cache(
    router_module, router_env,
) -> None:
    """Cache miss + file present on disk → return bytes, warm cache.

    This is the path the cookie handler hits after a fresh router
    process boot when the systemd unit's ExecStartPre wrote the file
    earlier (or on a different process).
    """
    from agent_mcp.router import project_orchestrator as _po

    name = "cache-miss-file-exists"
    expected = b"abcdefgh" * 4  # 32 bytes
    path = _po._forwarding_hmac_path(name)
    path.write_bytes(expected)
    os.chmod(path, 0o600)

    # Pre-condition: cache empty.
    assert name not in _po.forwarding_hmac_keys

    got = _po.ensure_forwarding_hmac_key(name)
    assert got == expected
    # Post-condition: cache warmed for next call.
    assert _po.forwarding_hmac_keys[name] == expected


@pytest.mark.asyncio
async def test_cache_miss_and_no_file_returns_none(
    router_module, router_env,
) -> None:
    """Cache miss + file missing → ``None``. Router NEVER writes.

    This is the "systemd hasn't run ExecStartPre yet" state.
    Returning None lets ``_ensure`` proceed to invoke ``systemctl
    start`` (which triggers the ExecStartPre that writes the file).
    The cookie path that calls ``get_forwarding_hmac_key`` separately
    will pick up the bytes off disk on the next call.
    """
    from agent_mcp.router import project_orchestrator as _po

    name = "no-file-no-cache"
    path = _po._forwarding_hmac_path(name)
    # Sanity: directory exists (created by _forwarding_hmac_path) but
    # the file does not.
    assert path.parent.exists()
    assert not path.exists()
    assert name not in _po.forwarding_hmac_keys

    assert _po.ensure_forwarding_hmac_key(name) is None
    assert _po.get_forwarding_hmac_key(name) is None

    # And the call did NOT create the file — that's the systemd
    # unit's job (see ``nix/module.nix`` ExecStartPre).
    assert not path.exists(), (
        "router-side reader created the HMAC key file — F015 v4 "
        "inverted ownership; the systemd unit's ExecStartPre is the "
        "ONLY writer. A router-side write here re-introduces the "
        "v3 race that crashed the VM in a 9569-deep restart loop."
    )


@pytest.mark.asyncio
async def test_orchestrator_stop_pops_cache_but_does_not_touch_file(
    router_module, router_env, systemctl_stub,
) -> None:
    """``ProjectOrchestrator.stop()`` pops the cache, leaves the file
    alone. The systemd unit's ExecStartPre + RuntimeDirectoryPreserve
    decide the on-disk lifecycle, not the router."""
    from agent_mcp.router import project_orchestrator as _po

    name = "stop-pops-cache"
    router_module._REGISTRY.register(name, str(router_env.root / "ws"))
    unit = f"agent-mcp@{name}.service"
    systemctl_stub.active_units.add(unit)

    # Pre-seed: simulate what a previous successful spawn left behind
    # (systemd ExecStartPre wrote the file; router cached it).
    expected = b"k" * 32
    path = _po._forwarding_hmac_path(name)
    path.write_bytes(expected)
    _po.forwarding_hmac_keys[name] = expected

    orch = _po.ProjectOrchestrator(router_module._REGISTRY)
    result = orch.stop(name)
    assert result == {"stopped": True}

    # Cache popped so the next spawn re-reads from the source of truth.
    assert name not in _po.forwarding_hmac_keys
    # File untouched: ownership is the systemd unit's.
    assert path.exists() and path.read_bytes() == expected


# ── Structural contract: no write logic in the reader path ──────────


def _read_function_source(func) -> str:
    """Return the source text for ``func`` (handles closures)."""
    return inspect.getsource(func)


def _reader_module_node() -> ast.Module:
    """Parse ``project_orchestrator.py`` to AST."""
    from agent_mcp.router import project_orchestrator as _po

    return ast.parse(Path(_po.__file__).read_text())


# Token-based regression guards: a future refactor that quietly
# re-introduces a write path inside the reader functions will trip
# one of these. We check the function source rather than the module
# globally so unrelated helpers (e.g. tests, the rotation API if it
# ever lands) don't trigger false positives.

_WRITE_TOKENS = (
    "write_bytes",
    "os.write",
    "secrets.token_bytes",
    "os.urandom",
    "O_CREAT",
)


def test_ensure_forwarding_hmac_key_has_no_write_tokens(router_module) -> None:
    from agent_mcp.router import project_orchestrator as _po

    src = _read_function_source(_po.ensure_forwarding_hmac_key)
    offenders = [tok for tok in _WRITE_TOKENS if tok in src]
    assert not offenders, (
        f"ensure_forwarding_hmac_key contains write-side tokens "
        f"{offenders!r} — F015 v4 demoted this function to read-only. "
        f"Generating or writing the HMAC key from the router re-"
        f"introduces the inverted-ownership bug (systemd auto-restarts "
        f"bypass the router; file goes missing on the live VM). The "
        f"systemd unit's ExecStartPre is the only writer."
    )


def test_get_forwarding_hmac_key_has_no_write_tokens(router_module) -> None:
    from agent_mcp.router import project_orchestrator as _po

    src = _read_function_source(_po.get_forwarding_hmac_key)
    offenders = [tok for tok in _WRITE_TOKENS if tok in src]
    assert not offenders, (
        f"get_forwarding_hmac_key contains write-side tokens "
        f"{offenders!r} — see ensure_forwarding_hmac_key contract."
    )


def test_secrets_import_dropped_from_orchestrator(router_module) -> None:
    """``secrets`` was only used to generate the HMAC key. After F015
    v4 (router never writes), the import should be gone. If a future
    change needs ``secrets`` for an unrelated reason, update the
    assertion message and the import-graph map at the same time so
    the next maintainer knows what the new symbol is for."""
    tree = _reader_module_node()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "secrets", (
                    "project_orchestrator imports ``secrets`` again — "
                    "F015 v4 removed it along with the key-generation "
                    "logic. Re-introducing the import suggests writer "
                    "logic creeping back into the router; if it's for "
                    "a legitimate non-HMAC reason, update this test."
                )
        elif isinstance(node, ast.ImportFrom):
            assert node.module != "secrets", (
                "project_orchestrator imports from ``secrets`` again — "
                "see above."
            )
