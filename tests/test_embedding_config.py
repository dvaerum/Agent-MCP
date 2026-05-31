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
