"""Env-var override for embedding model + dimension.

`agent_mcp.core.config` evaluates its module-level constants at import
time. Tests reload the module after setting env vars so the new
values get picked up. Other modules that already captured these
constants aren't affected — that's fine; tests only assert what
`config` itself resolves to.
"""

from __future__ import annotations

import importlib

import pytest

from agent_mcp.core import config as cfg


@pytest.fixture
def reload_config():
    """Reload agent_mcp.core.config after a test mutates env vars."""
    yield
    importlib.reload(cfg)


def test_simple_embedding_model_defaults_preserved(monkeypatch, reload_config) -> None:
    """No env var → upstream's text-embedding-3-large / 1536 stays."""
    monkeypatch.delenv("AGENT_MCP_EMBEDDING_MODEL", raising=False)
    monkeypatch.delenv("AGENT_MCP_EMBEDDING_DIMENSION", raising=False)

    importlib.reload(cfg)

    assert cfg.SIMPLE_EMBEDDING_MODEL == "text-embedding-3-large"
    assert cfg.SIMPLE_EMBEDDING_DIMENSION == 1536


def test_simple_embedding_model_overridable_via_env(monkeypatch, reload_config) -> None:
    """AGENT_MCP_EMBEDDING_MODEL overrides the model constant."""
    monkeypatch.setenv("AGENT_MCP_EMBEDDING_MODEL", "qwen3-embedding:0.6b")

    importlib.reload(cfg)

    assert cfg.SIMPLE_EMBEDDING_MODEL == "qwen3-embedding:0.6b"


def test_simple_embedding_dimension_overridable_via_env(
    monkeypatch, reload_config
) -> None:
    """AGENT_MCP_EMBEDDING_DIMENSION overrides the dimension constant."""
    monkeypatch.setenv("AGENT_MCP_EMBEDDING_DIMENSION", "1024")

    importlib.reload(cfg)

    assert cfg.SIMPLE_EMBEDDING_DIMENSION == 1024
    assert isinstance(cfg.SIMPLE_EMBEDDING_DIMENSION, int)


# --- EmbeddingSettings: resolved value object, not a mutated global ----
#
# arch-r4 #4 (HIGH, latent correctness bug): `EMBEDDING_DIMENSION` /
# `EMBEDDING_MODEL` / `ADVANCED_EMBEDDINGS` used to be module-level
# constants computed ONCE at import time, then MUTATED in place by
# `server_bootstrap.apply_runtime_flags` for `--advanced` mode.
# Attribute-access readers (`_config.EMBEDDING_DIMENSION`) saw the
# rebind; `from ...core.config import EMBEDDING_DIMENSION` readers (e.g.
# `db/schema.py`, `features/rag/indexing.py`) froze the pre-mutation
# (simple-mode) value at THEIR OWN import time — an unenforced
# import-order dependency that could build the sqlite-vec column at
# the wrong dimension. (A third reader, the hand-run
# `db/migrations/add_code_support.py` script, derived the dimension a
# 3rd way via hardcoded string matching; deleted in arch-r5 #9 as
# orphaned — nothing imported it, and its 3 effects are now owned by
# ORM `create_all`, `_DEFAULT_RAG_META_ENTRIES`, and
# `check_embedding_dimension_compatibility`.)
#
# `embedding_settings()` is a function: every call re-resolves from
# current state, so there is no name-binding to freeze. These are PURE
# unit tests — no subprocess, no import-order choreography, no module
# reload — because `embedding_settings(advanced=...)` resolves directly
# from an explicit argument (or, absent one, from the current
# `ADVANCED_EMBEDDINGS` module flag).


def test_embedding_settings_simple_mode_dimension() -> None:
    """Simple mode resolves to the SIMPLE_* constants (1536 by default)."""
    settings = cfg.embedding_settings(advanced=False)
    assert settings.model == cfg.SIMPLE_EMBEDDING_MODEL
    assert settings.dimension == cfg.SIMPLE_EMBEDDING_DIMENSION
    assert settings.advanced is False


def test_embedding_settings_advanced_mode_dimension() -> None:
    """Advanced mode resolves to text-embedding-3-large / 3072 — the
    values that used to require a correctly-ordered import to observe."""
    settings = cfg.embedding_settings(advanced=True)
    assert settings.model == cfg.ADVANCED_EMBEDDING_MODEL == "text-embedding-3-large"
    assert settings.dimension == cfg.ADVANCED_EMBEDDING_DIMENSION == 3072


def test_embedding_settings_from_server_config_advanced(tmp_path) -> None:
    """A caller holding a ``ServerConfig`` resolves settings PURELY from
    it — no reliance on module state, no import-order dependency.

    This is the RED test for arch-r4 #4: before the fix, the only way to
    get the advanced-mode dimension was to mutate
    ``core_config.EMBEDDING_DIMENSION`` via ``apply_runtime_flags`` and
    hope every reader observed the mutation. ``embedding_settings()``
    accepts the flag directly.
    """
    from agent_mcp.server_bootstrap import ServerConfig

    server_cfg = ServerConfig(
        transport="stdio", port=0, project_dir=str(tmp_path), advanced=True
    )

    settings = cfg.embedding_settings(server_cfg.advanced)

    assert settings.dimension == 3072
    assert settings.advanced is True


def test_embedding_settings_from_server_config_simple(tmp_path) -> None:
    from agent_mcp.server_bootstrap import ServerConfig

    server_cfg = ServerConfig(
        transport="stdio", port=0, project_dir=str(tmp_path), advanced=False
    )

    settings = cfg.embedding_settings(server_cfg.advanced)

    assert settings.dimension == cfg.SIMPLE_EMBEDDING_DIMENSION
    assert settings.advanced is False


def test_embedding_settings_omitted_falls_back_to_module_flag(
    monkeypatch,
) -> None:
    """Deep call sites with no ``ServerConfig`` in hand (RAG indexer,
    ``db/schema.py``) call ``embedding_settings()`` with no argument —
    it falls back to the ``ADVANCED_EMBEDDINGS`` flag that
    ``apply_runtime_flags`` sets exactly ONCE at boot."""
    original = cfg.ADVANCED_EMBEDDINGS
    try:
        cfg.ADVANCED_EMBEDDINGS = True
        assert cfg.embedding_settings().dimension == 3072

        cfg.ADVANCED_EMBEDDINGS = False
        assert cfg.embedding_settings().dimension == cfg.SIMPLE_EMBEDDING_DIMENSION
    finally:
        cfg.ADVANCED_EMBEDDINGS = original


def test_readers_no_longer_import_frozen_embedding_constants() -> None:
    """Structural guard: the modules that used to freeze
    ``EMBEDDING_DIMENSION`` / ``ADVANCED_EMBEDDINGS`` at their own import
    time must now resolve via ``embedding_settings()`` instead."""
    import inspect

    from agent_mcp.db import schema as schema_mod
    from agent_mcp.features.rag import indexing as indexing_mod

    for mod in (schema_mod, indexing_mod):
        src = inspect.getsource(mod)
        assert "embedding_settings" in src, (
            f"{mod.__name__} should resolve embedding config via "
            f"embedding_settings(), not a frozen import"
        )

    # These names must no longer exist as module attributes on
    # core.config — apply_runtime_flags only mutates ADVANCED_EMBEDDINGS.
    assert not hasattr(cfg, "EMBEDDING_MODEL")
    assert not hasattr(cfg, "EMBEDDING_DIMENSION")
