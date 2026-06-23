"""Forwarding-HMAC key file self-heals after out-of-band deletion (F015 v3).

Root cause (F015 v3, post retire-system-token Waves 1-5):

  ``ensure_forwarding_hmac_key(name)`` short-circuited on a cache
  hit and returned the cached bytes WITHOUT verifying the on-disk
  file still existed. The systemd unit
  ``agent-mcp@<project>.service`` declares
  ``RuntimeDirectory=agent-mcp/%i`` with the default
  ``RuntimeDirectoryPreserve=no``, so every ``systemctl stop`` wiped
  ``/run/agent-mcp/<name>/`` — including the ``forwarding_hmac``
  file. The idle reaper called ``_systemctl("stop", ...)`` but did
  NOT pop ``forwarding_hmac_keys[name]``, so the cache stayed
  populated through the systemd teardown. On the next request,
  ``_ensure`` called ``ensure_forwarding_hmac_key`` (cache hit, no
  re-write), then ``_systemctl("start", unit)`` → launcher exec'd
  the backend → click's ``--forwarding-hmac-in`` ``File()`` validator
  exited 2 with "File does not exist".

Two complementary fixes are pinned by the tests below:

  1. ``ensure_forwarding_hmac_key`` is now self-healing: on a cache
     hit it stats the on-disk path and, if missing, re-writes the
     cached key (mode 0600) before returning. The cache is still
     authoritative for the value — what changes is that the disk
     file is treated as a derived artefact that must be re-derived
     when it's gone.

  2. ``_reaper_tick`` now pops ``forwarding_hmac_keys[name]`` after
     ``_systemctl("stop", ...)`` so the cache symmetry with
     ``ProjectOrchestrator.stop()`` is restored. After a reaper
     stop, the next spawn rotates the key cleanly instead of
     re-using a key the backend will never see again on disk.

The tests deliberately do NOT mock ``Path.exists()`` — they delete
and recreate the real file via ``Path.unlink()`` and observe the
filesystem afterwards. Mocking ``exists()`` to return True would
defeat the purpose: the bug is exactly that the cache hit skipped
the filesystem check.
"""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.asyncio


async def test_ensure_forwarding_hmac_key_self_heals_when_file_deleted(
    router_module, router_env,
) -> None:
    """Cache hit + missing file → file is re-created with the cached key.

    Reproduces F015 v3: systemd RuntimeDirectory teardown deletes the
    on-disk key while the router's in-memory cache stays populated.
    The fix: ``ensure_forwarding_hmac_key`` stats the path on every
    cache hit and re-writes the cached key if the file is gone.
    """
    from agent_mcp.router import project_orchestrator as _po

    name = "self-heal"

    # First call: generates a new key, writes the file, caches.
    key1 = _po.ensure_forwarding_hmac_key(name)
    path = _po._forwarding_hmac_path(name)
    assert path.exists(), "first call must create the on-disk key file"
    assert path.read_bytes() == key1
    assert name in _po.forwarding_hmac_keys

    # Simulate systemd RuntimeDirectory teardown: the file is gone but
    # the in-memory cache still has the bytes.
    path.unlink()
    assert not path.exists()
    assert _po.forwarding_hmac_keys.get(name) == key1, (
        "precondition: cache must still have the entry — that's the bug"
    )

    # Second call: cache hit MUST re-create the file with the SAME
    # bytes (rotating here would break any backend instance that loaded
    # the key earlier in this router process's lifetime).
    key2 = _po.ensure_forwarding_hmac_key(name)
    assert key2 == key1, (
        "self-heal must NOT rotate the key — backends in-flight would "
        "fail HMAC verify with a new key while still holding the old one"
    )
    assert path.exists(), (
        "self-heal must re-create the on-disk file so the backend's "
        "--forwarding-hmac-in File() validator passes at spawn time"
    )
    assert path.read_bytes() == key1


async def test_self_healed_file_has_mode_0600(
    router_module, router_env,
) -> None:
    """The re-written file must keep the same 0600 permission as the
    first write — the router process owns it and nothing else should
    read it (matches the generate-branch invariant)."""
    from agent_mcp.router import project_orchestrator as _po

    name = "perm-check"
    _po.ensure_forwarding_hmac_key(name)
    path = _po._forwarding_hmac_path(name)
    path.unlink()
    _po.ensure_forwarding_hmac_key(name)

    mode = path.stat().st_mode & 0o777
    assert mode == 0o600, (
        f"self-healed key file has mode {oct(mode)}, expected 0o600 — "
        "router-only readability is part of the HMAC contract"
    )


async def test_reaper_tick_pops_forwarding_hmac_cache(
    router_module, router_env, systemctl_stub, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After ``_reaper_tick`` stops an idle backend, the in-memory HMAC
    cache for that project must be popped — symmetry with
    ``ProjectOrchestrator.stop()`` (which already pops at
    ``project_orchestrator.py:648``).

    Without this, the cache + on-disk file drift after a reaper-driven
    stop (file gets wiped by systemd RuntimeDirectory teardown, cache
    survives), and the self-heal branch above is the only thing
    keeping the next spawn alive. We want BOTH: the reaper to pop
    (so the next spawn rotates cleanly), AND the self-heal to defend
    against any other out-of-band file deletion we haven't anticipated.
    """
    from agent_mcp.router import project_orchestrator as _po

    name = "reaper-pop"
    router_module._REGISTRY.register(name, str(router_env.root / "ws"))
    unit = f"agent-mcp@{name}.service"
    systemctl_stub.active_units.add(unit)

    # Seed the cache as if a backend had been spawned.
    _po.ensure_forwarding_hmac_key(name)
    assert name in _po.forwarding_hmac_keys

    # Plant an idle last_active timestamp so the reaper acts on it.
    last = 1_000_000.0
    _po.last_active[(name, "backend")] = last
    monkeypatch.setattr(
        _po.time, "time",
        lambda: last + _po.IDLE_SEC + 60.0,
    )

    await _po._reaper_tick()

    assert systemctl_stub.counts[("stop", unit)] == 1, (
        "precondition: reaper must have invoked systemctl stop — "
        f"call log: {systemctl_stub.calls}"
    )
    assert name not in _po.forwarding_hmac_keys, (
        "reaper stopped the backend but did NOT pop the HMAC cache — "
        "the next spawn will reuse a key the backend won't see on disk "
        "(systemd RuntimeDirectory teardown wiped the file)"
    )
    assert (name, "backend") not in _po.last_active


async def test_reaper_pop_lets_next_ensure_rotate(
    router_module, router_env, systemctl_stub, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end of the reaper-symmetry fix: after a reaper stop +
    cache pop, the next ``ensure_forwarding_hmac_key`` MUST generate
    a fresh key (not reuse the previous one). This matches the
    behaviour of ``ProjectOrchestrator.stop()`` followed by an
    ``_ensure``."""
    from agent_mcp.router import project_orchestrator as _po

    name = "rotate-after-reap"
    router_module._REGISTRY.register(name, str(router_env.root / "ws"))
    unit = f"agent-mcp@{name}.service"
    systemctl_stub.active_units.add(unit)

    key_pre = _po.ensure_forwarding_hmac_key(name)

    # Plant idle timestamp + drive one reaper tick.
    last = 2_000_000.0
    _po.last_active[(name, "backend")] = last
    monkeypatch.setattr(
        _po.time, "time",
        lambda: last + _po.IDLE_SEC + 60.0,
    )
    await _po._reaper_tick()

    # Simulate systemd RuntimeDirectory teardown (which is what
    # systemd actually does on stop with RuntimeDirectoryPreserve=no).
    path = _po._forwarding_hmac_path(name)
    if path.exists():
        path.unlink()

    key_post = _po.ensure_forwarding_hmac_key(name)
    assert key_post != key_pre, (
        "after reaper stop + systemd teardown, the next ensure must "
        "generate a FRESH key — reuse would leave the backend with a "
        "key it can never produce again"
    )
    assert path.exists() and path.read_bytes() == key_post
