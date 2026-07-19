"""Unit tests for the per-client hold-strategy resolver.

Covers the hybrid identity-first / feature-detect model (plan §2):
known clients resolve by ``clientInfo.name`` (case/spacing normalized),
Cursor is the false-positive guard (in-table no-heartbeat DESPITE a
progressToken), and unknown clients feature-detect on the token.
"""

from __future__ import annotations

import pytest

from agent_mcp.core.client_hold_strategy import (
    CLAUDE_CODE_HOLD_CAP_SECONDS,
    NO_HEARTBEAT_HOLD_SECONDS,
    normalize_client_name,
    resolve_hold_strategy,
)


@pytest.mark.parametrize(
    "name,expected_heartbeat,expected_cap",
    [
        ("claude-code", True, CLAUDE_CODE_HOLD_CAP_SECONDS),
        ("opencode", True, None),
        ("cursor", False, NO_HEARTBEAT_HOLD_SECONDS),
        ("cline", False, NO_HEARTBEAT_HOLD_SECONDS),
        ("zed", False, NO_HEARTBEAT_HOLD_SECONDS),
        ("continue", False, NO_HEARTBEAT_HOLD_SECONDS),
    ],
)
def test_known_clients_resolve_by_identity(name, expected_heartbeat, expected_cap):
    # Even with a progressToken present, the identity table wins.
    strat = resolve_hold_strategy(name, has_progress_token=True)
    assert strat.heartbeat is expected_heartbeat
    assert strat.hold_cap == expected_cap


def test_cursor_false_positive_guard():
    """Cursor sends a progressToken but never resets on it — identity
    pinning must give it no-heartbeat regardless of the token."""
    strat = resolve_hold_strategy("cursor", has_progress_token=True)
    assert strat.heartbeat is False
    assert strat.hold_cap == NO_HEARTBEAT_HOLD_SECONDS


def test_unknown_client_with_token_gets_heartbeat_no_cap():
    strat = resolve_hold_strategy("some-future-ide", has_progress_token=True)
    assert strat.heartbeat is True
    assert strat.hold_cap is None


def test_unknown_client_without_token_gets_silent_default():
    strat = resolve_hold_strategy("some-future-ide", has_progress_token=False)
    assert strat.heartbeat is False
    assert strat.hold_cap == NO_HEARTBEAT_HOLD_SECONDS


def test_no_client_name_without_token_is_silent_default():
    strat = resolve_hold_strategy(None, has_progress_token=False)
    assert strat.heartbeat is False
    assert strat.hold_cap == NO_HEARTBEAT_HOLD_SECONDS


def test_no_client_name_with_token_feature_detects():
    strat = resolve_hold_strategy(None, has_progress_token=True)
    assert strat.heartbeat is True
    assert strat.hold_cap is None


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("claude-code", "claude-code"),
        ("Claude-Code", "claude-code"),
        ("  claude-code  ", "claude-code"),
        ("Claude   Code", "claude code"),
        ("OpenCode", "opencode"),
        ("", None),
        (None, None),
    ],
)
def test_normalize_client_name(raw, expected):
    assert normalize_client_name(raw) == expected


def test_normalization_applies_in_resolution():
    """A client that sends mixed-case identity still hits the table."""
    strat = resolve_hold_strategy("Claude-Code", has_progress_token=False)
    assert strat.heartbeat is True
    assert strat.hold_cap == CLAUDE_CODE_HOLD_CAP_SECONDS
