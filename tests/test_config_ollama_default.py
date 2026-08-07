"""When OPENAI_API_KEY is unset, core.config seeds Ollama defaults.

Replaces the old CRITICAL log. The bundled local Ollama default keeps
the server functional out of the box; users only need to set an env
var when they want a different endpoint.

Each test deletes OPENAI_API_KEY (and the Ollama-default keys it
seeds) before reloading the config module so the module-load side
effects re-run from a known state.
"""

from __future__ import annotations

import importlib
import os

import pytest

_OLLAMA_KEYS = (
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_MODEL",
    "AGENT_MCP_EMBEDDING_MODEL",
    "AGENT_MCP_EMBEDDING_DIMENSION",
)


@pytest.fixture
def _clean_ollama_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Delete every Ollama-related env var so the module-load code path
    that fills them with defaults gets exercised from scratch."""
    for key in _OLLAMA_KEYS:
        monkeypatch.delenv(key, raising=False)


def _reload_config():
    import agent_mcp.core.config as cfg
    return importlib.reload(cfg)


def test_no_openai_api_key_seeds_ollama_defaults(_clean_ollama_env) -> None:
    _reload_config()
    assert os.environ.get("OPENAI_API_KEY") == "ollama"
    assert os.environ.get("OPENAI_BASE_URL") == "http://127.0.0.1:11434/v1"
    assert os.environ.get("OPENAI_MODEL") == "qwen3:1.7b"
    assert os.environ.get("AGENT_MCP_EMBEDDING_MODEL") == "qwen3-embedding:0.6b"
    assert os.environ.get("AGENT_MCP_EMBEDDING_DIMENSION") == "1024"


def test_openai_api_key_env_module_attr_is_ollama_when_unset(
    _clean_ollama_env,
) -> None:
    cfg = _reload_config()
    # The module-level OPENAI_API_KEY_ENV must reflect the seeded
    # default, not stay None.
    assert cfg.OPENAI_API_KEY_ENV == "ollama"


def test_explicit_openai_api_key_is_not_overridden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-explicit-user-key")
    # Don't touch the Ollama-default keys — the user-supplied key
    # should win and the Ollama defaults stay as the user left them.
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)

    cfg = _reload_config()
    assert os.environ.get("OPENAI_API_KEY") == "sk-explicit-user-key"
    assert cfg.OPENAI_API_KEY_ENV == "sk-explicit-user-key"
    # When the user set OPENAI_API_KEY explicitly, we MUST NOT clobber
    # OPENAI_BASE_URL / OPENAI_MODEL with Ollama defaults — that would
    # silently break a user who legitimately wants the OpenAI cloud.
    assert "OPENAI_BASE_URL" not in os.environ
    assert "OPENAI_MODEL" not in os.environ
