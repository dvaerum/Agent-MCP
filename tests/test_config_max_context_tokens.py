"""Guard: the RAG context-token budget must be env-overridable.

``MAX_CONTEXT_TOKENS`` / ``TASK_ANALYSIS_MAX_TOKENS`` default to
1,000,000 (GPT-4.1's window). On a small-context local model
(llama-cpp / Ollama, ~8k window) the RAG assembler's truncation guard
(`_append_within_budget`) never fires against a 1M budget, so the model
rejects the oversized prompt (`exceed_context_size_error`). A single env
var, ``AGENT_MCP_MAX_CONTEXT_TOKENS``, lets such a deployment cap the
budget to its own window while cloud deployments keep the 1M default.

Both constants are bound at module import, so each case runs in a FRESH
subprocess with a controlled env (same pattern as
``test_config_embedding_default_order``). ``importlib.reload`` is
unreliable here because a prior import can leave module state behind; a
subprocess imports the module exactly once against the env we set.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]

_PROBE = (
    "import json;"
    "import agent_mcp.core.config as c;"
    "print(json.dumps({"
    "'max_context':c.MAX_CONTEXT_TOKENS,"
    "'task_analysis':c.TASK_ANALYSIS_MAX_TOKENS}))"
)


def _resolve_config(overrides: dict[str, str]) -> dict:
    """Import core.config in a fresh subprocess under a controlled env
    and return its resolved context-token budget."""
    env = {k: v for k, v in os.environ.items() if k != "AGENT_MCP_MAX_CONTEXT_TOKENS"}
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
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    result["_stderr"] = proc.stderr
    return result


def test_default_budget_is_one_million_when_unset() -> None:
    """No env var → both constants keep the 1M GPT-4.1 default so cloud
    deployments are unchanged."""
    result = _resolve_config(overrides={})

    assert result["max_context"] == 1000000
    assert result["task_analysis"] == 1000000


def test_env_override_caps_both_budgets() -> None:
    """AGENT_MCP_MAX_CONTEXT_TOKENS caps both the RAG budget and the
    task-analysis budget to the same model-window value."""
    result = _resolve_config(overrides={"AGENT_MCP_MAX_CONTEXT_TOKENS": "6000"})

    assert result["max_context"] == 6000
    assert result["task_analysis"] == 6000


def test_non_integer_falls_back_to_default_without_crashing() -> None:
    """A typo'd (non-integer) value must not crash the server: fall back
    to the 1M default and warn."""
    result = _resolve_config(
        overrides={"AGENT_MCP_MAX_CONTEXT_TOKENS": "not-an-int"}
    )

    assert result["max_context"] == 1000000
    assert result["task_analysis"] == 1000000
    assert "AGENT_MCP_MAX_CONTEXT_TOKENS" in result["_stderr"]


def test_non_positive_falls_back_to_default() -> None:
    """A zero / negative value is meaningless as a budget → fall back to
    the 1M default and warn."""
    for bad in ("0", "-100"):
        result = _resolve_config(
            overrides={"AGENT_MCP_MAX_CONTEXT_TOKENS": bad}
        )
        assert result["max_context"] == 1000000, bad
        assert result["task_analysis"] == 1000000, bad
        assert "AGENT_MCP_MAX_CONTEXT_TOKENS" in result["_stderr"], bad
