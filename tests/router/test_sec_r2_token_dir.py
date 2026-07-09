"""SEC round-2 FINDING 5 [LOW] — token-dir default must not be CWD.

Owner-authorised defensive review (2026-07-09). ``Path("")`` is TRUTHY
(``PosixPath('.')``), so the old
``Path(os.environ.get("AGENT_MCP_TOKENS_DIR", "")) or <default>`` idiom
resolved an UNSET env var to the process CWD, not the intended default —
making the ``<name>--*.token`` purge on delete / rename a silent no-op.
``admin_api._token_dir()`` now branches on the env var's presence.
"""

from __future__ import annotations

from pathlib import Path


def test_token_dir_default_resolves_to_config_not_cwd(monkeypatch) -> None:
    from agent_mcp.router import admin_api

    monkeypatch.delenv("AGENT_MCP_TOKENS_DIR", raising=False)
    got = admin_api._token_dir()

    assert got == Path.home() / ".config" / "agent-mcp" / "tokens"
    # The bug: an unset env var resolved to the CWD (``PosixPath('.')``).
    assert got != Path("")
    assert got != Path(".")
    assert got != Path.cwd()


def test_token_dir_empty_string_env_falls_back_to_default(monkeypatch) -> None:
    from agent_mcp.router import admin_api

    # An explicitly-empty env var is treated as "unset".
    monkeypatch.setenv("AGENT_MCP_TOKENS_DIR", "")
    got = admin_api._token_dir()

    assert got == Path.home() / ".config" / "agent-mcp" / "tokens"


def test_token_dir_honours_env_when_set(monkeypatch, tmp_path) -> None:
    from agent_mcp.router import admin_api

    monkeypatch.setenv("AGENT_MCP_TOKENS_DIR", str(tmp_path))
    assert admin_api._token_dir() == tmp_path
