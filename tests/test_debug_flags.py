"""Per-project debug-logging switches (core/debug_flags.py)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from agent_mcp.core import debug_flags as df

_KEY = "config_debug_eventloop"
_ENV = "AGENT_MCP_EVENTLOOP_DEBUG"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    df.clear_cache()
    monkeypatch.delenv(_ENV, raising=False)
    yield
    df.clear_cache()


def test_env_var_is_the_fallback_when_setting_absent(monkeypatch):
    # _get_config_bool returns the `default` it's handed when the row is
    # absent — so the effective value is the env var.
    with patch(
        "agent_mcp.tools.access._get_config_bool", side_effect=lambda k, d: d
    ):
        monkeypatch.setenv(_ENV, "1")
        df.clear_cache()
        assert df.debug_enabled(_KEY, _ENV) is True

        monkeypatch.setenv(_ENV, "0")
        df.clear_cache()
        assert df.debug_enabled(_KEY, _ENV) is False


def test_project_setting_overrides_env(monkeypatch):
    monkeypatch.setenv(_ENV, "0")  # env says off
    with patch("agent_mcp.tools.access._get_config_bool", return_value=True):
        df.clear_cache()
        assert df.debug_enabled(_KEY, _ENV) is True  # the stored row wins


def test_read_failure_falls_back_to_env(monkeypatch):
    monkeypatch.setenv(_ENV, "1")
    with patch(
        "agent_mcp.tools.access._get_config_bool",
        side_effect=RuntimeError("no project db"),
    ):
        df.clear_cache()
        assert df.debug_enabled(_KEY, _ENV) is True  # never breaks a request


def test_result_is_ttl_cached():
    calls = []

    def fake(_k, _d):
        calls.append(1)
        return True

    with patch("agent_mcp.tools.access._get_config_bool", side_effect=fake):
        df.clear_cache()
        assert df.debug_enabled(_KEY, _ENV) is True
        assert df.debug_enabled(_KEY, _ENV) is True
        assert len(calls) == 1  # second call served from the TTL cache
