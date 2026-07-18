"""Import-order guard: the Ollama setdefault block must run BEFORE the
``SIMPLE_EMBEDDING_MODEL`` / ``SIMPLE_EMBEDDING_DIMENSION`` reads.

Regression for a live bug: when ``OPENAI_API_KEY`` was unset the server
is meant to default to local Ollama, but the embedding constants were
bound from ``os.environ`` *earlier* in the module body than the block
that seeds ``AGENT_MCP_EMBEDDING_MODEL`` / ``AGENT_MCP_EMBEDDING_DIMENSION``.
So the constants froze to the OpenAI fallbacks (text-embedding-3-large /
1536), which Ollama does not serve — every RAG indexing cycle 404'd.

Each case runs in a FRESH subprocess with a controlled environment.
``importlib.reload`` can't reproduce the bug: the first in-process
import seeds ``os.environ`` via ``setdefault`` as a side effect, and
that seeded value survives the reload, masking the import-order defect.
A subprocess imports the module exactly once against the env we set, so
the ordering is exercised for real.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]

# Emit the resolved embedding config as JSON so the parent can assert on
# the constants AND on embedding_settings() (the RAG stack's read path).
_PROBE = (
    "import json;"
    "import agent_mcp.core.config as c;"
    "s=c.embedding_settings(advanced=False);"
    "print(json.dumps({"
    "'model':c.SIMPLE_EMBEDDING_MODEL,"
    "'dimension':c.SIMPLE_EMBEDDING_DIMENSION,"
    "'resolved_model':s.model,"
    "'resolved_dimension':s.dimension,"
    "'resolved_advanced':s.advanced}))"
)

_EMBEDDING_ENV_KEYS = (
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_MODEL",
    "AGENT_MCP_EMBEDDING_MODEL",
    "AGENT_MCP_EMBEDDING_DIMENSION",
)


def _resolve_config(overrides: dict[str, str]) -> dict:
    """Import core.config in a fresh subprocess under a controlled env
    and return its resolved embedding configuration."""
    env = {k: v for k, v in os.environ.items() if k not in _EMBEDDING_ENV_KEYS}
    env.update(overrides)
    env["PYTHONPATH"] = str(_REPO_ROOT)

    proc = subprocess.run(
        [sys.executable, "-c", _PROBE],
        env=env,
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (
        f"probe subprocess failed ({proc.returncode}):\n{proc.stderr}"
    )
    # config emits logging to stderr; the last stdout line is our JSON.
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_ollama_embedding_defaults_apply_when_openai_key_unset() -> None:
    """OPENAI_API_KEY unset + embedding vars unset → Ollama embedding
    defaults win, and embedding_settings() resolves to the same."""
    result = _resolve_config(overrides={})

    assert result["model"] == "qwen3-embedding:0.6b"
    assert result["dimension"] == 1024
    assert result["resolved_model"] == "qwen3-embedding:0.6b"
    assert result["resolved_dimension"] == 1024
    assert result["resolved_advanced"] is False


def test_cloud_embedding_defaults_preserved_when_openai_key_set() -> None:
    """OPENAI_API_KEY set → Ollama block skipped → embedding constants
    keep the OpenAI cloud defaults (text-embedding-3-large / 1536)."""
    result = _resolve_config(overrides={"OPENAI_API_KEY": "sk-real"})

    assert result["model"] == "text-embedding-3-large"
    assert result["dimension"] == 1536
    assert result["resolved_model"] == "text-embedding-3-large"
    assert result["resolved_dimension"] == 1536


def test_explicit_embedding_override_wins() -> None:
    """An explicit AGENT_MCP_EMBEDDING_MODEL / _DIMENSION wins even when
    OPENAI_API_KEY is unset — setdefault must not clobber it."""
    result = _resolve_config(
        overrides={
            "AGENT_MCP_EMBEDDING_MODEL": "foo",
            "AGENT_MCP_EMBEDDING_DIMENSION": "99",
        }
    )

    assert result["model"] == "foo"
    assert result["dimension"] == 99
    assert result["resolved_model"] == "foo"
    assert result["resolved_dimension"] == 99
