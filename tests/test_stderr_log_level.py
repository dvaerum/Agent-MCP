"""The always-on stderr→journald handler floor is env-configurable.

Default WARNING keeps steady-state journald quiet; a burn-in / feature
watch flips ``AGENT_MCP_STDERR_LOG_LEVEL=INFO`` to surface INFO
(e.g. the operator_events stream OPEN/CLOSE lines) without a code change.
An unrecognised value must fall back to WARNING, never silence the
handler.
"""

from __future__ import annotations

import logging

from agent_mcp.core import config


def test_default_is_warning(monkeypatch):
    monkeypatch.delenv("AGENT_MCP_STDERR_LOG_LEVEL", raising=False)
    assert config._resolve_stderr_log_level() == logging.WARNING


def test_info_override(monkeypatch):
    monkeypatch.setenv("AGENT_MCP_STDERR_LOG_LEVEL", "INFO")
    assert config._resolve_stderr_log_level() == logging.INFO


def test_case_insensitive_and_whitespace(monkeypatch):
    monkeypatch.setenv("AGENT_MCP_STDERR_LOG_LEVEL", "  info ")
    assert config._resolve_stderr_log_level() == logging.INFO


def test_debug_override(monkeypatch):
    monkeypatch.setenv("AGENT_MCP_STDERR_LOG_LEVEL", "DEBUG")
    assert config._resolve_stderr_log_level() == logging.DEBUG


def test_unknown_value_falls_back_to_warning(monkeypatch):
    monkeypatch.setenv("AGENT_MCP_STDERR_LOG_LEVEL", "BOGUS")
    assert config._resolve_stderr_log_level() == logging.WARNING
