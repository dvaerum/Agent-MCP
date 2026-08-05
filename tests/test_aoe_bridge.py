"""Unit tests for the AoE bridge's pure cores (ADR-0021): skinny frame
rendering + mode-aware AoE injection."""

from __future__ import annotations

import pytest

from aoe_bridge.inject import (
    STRUCTURED,
    TERMINAL,
    AoeInjector,
    injection_request,
    normalise_mode,
)
from aoe_bridge.render import render_frame


# ── render (skinny, never a body) ───────────────────────────────────


def test_render_unread_lists_ids_not_bodies():
    frame = {
        "type": "delivery",
        "reason": "unread_messages",
        "unread_count": 2,
        "unread_messages": [
            {"message_id": "m1", "sender_id": "manager", "subject": "deploy?"},
            {"message_id": "m2", "sender_id": "alice", "subject": "review"},
        ],
    }
    text = render_frame(frame)
    assert "2 unread messages" in text
    assert "get_agent_messages" in text
    assert "deploy?" in text and "m1" in text and "manager" in text
    # never carries a body field key
    assert "body" not in text.lower()


def test_render_singular_grammar():
    text = render_frame(
        {"reason": "unread_messages", "unread_count": 1, "unread_messages": []}
    )
    assert "1 unread message " in text and "messages" not in text.split("—")[0]


def test_render_unfinished_tasks():
    text = render_frame(
        {
            "reason": "unfinished_tasks",
            "task_count": 1,
            "open_tasks": [{"task_id": "t1", "title": "fix pikvm", "status": "in_progress"}],
        }
    )
    assert "1 open task" in text
    assert "fix pikvm" in text and "t1" in text and "in_progress" in text


def test_render_unassigned():
    text = render_frame({"reason": "unassigned_tasks", "unassigned_count": 3})
    assert "3 unassigned tasks" in text and "view_tasks" in text


def test_render_unknown_reason_is_safe_pointer():
    text = render_frame({"reason": "something_new"})
    assert "agent-mcp" in text and "pending" in text


def test_render_caps_long_lists():
    frame = {
        "reason": "unread_messages",
        "unread_count": 9,
        "unread_messages": [
            {"message_id": f"m{i}", "sender_id": "x", "subject": f"s{i}"}
            for i in range(9)
        ],
    }
    text = render_frame(frame)
    assert "and 4 more" in text  # 9 - 5 cap


# ── inject (both modes) ─────────────────────────────────────────────


def test_normalise_mode():
    assert normalise_mode("tmux") == TERMINAL
    assert normalise_mode(None) == TERMINAL
    assert normalise_mode("shell") == TERMINAL
    assert normalise_mode("structured") == STRUCTURED
    assert normalise_mode("ACP") == STRUCTURED
    assert normalise_mode("cityhall-composer") == STRUCTURED


def test_injection_request_terminal():
    path, body = injection_request("sess-1", "tmux", "hello")
    assert path == "/api/sessions/sess-1/send"
    assert body == {"message": "hello", "revive": True}


def test_injection_request_structured():
    path, body = injection_request("sess-2", "acp", "hello")
    assert path == "/api/sessions/sess-2/acp/prompt"
    assert body == {"prompt": "hello"}


@pytest.mark.asyncio
async def test_injector_calls_right_route_and_reports_success():
    calls = []

    async def fake_post(path, body):
        calls.append((path, body))
        return 200

    inj = AoeInjector(fake_post)
    assert await inj.inject("s1", "structured", "hi") is True
    assert calls == [("/api/sessions/s1/acp/prompt", {"prompt": "hi"})]


@pytest.mark.asyncio
async def test_injector_reports_failure_on_non_2xx():
    async def fake_post(path, body):
        return 404

    assert await AoeInjector(fake_post).inject("s1", "tmux", "hi") is False
