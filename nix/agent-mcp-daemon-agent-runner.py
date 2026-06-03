#!/usr/bin/env python3
"""Event-loop runner for the agent-mcp-daemon-agent systemd template.

Reads env vars set by the bash wrapper (`agent-mcp-daemon-agent.sh.in`):

  AGENT_MCP_MCP_URL      — the project's Streamable HTTP /mcp endpoint.
  AGENT_MCP_PROJECT      — project slug (logging only today).
  AGENT_MCP_AGENT_ID     — agent slug (logging + future Claude session id).
  AGENT_MCP_BEARER       — bearer token for the agent.
  AGENT_MCP_CURSOR_FILE  — where to persist the `next_cursor` value
                           between iterations so a restart doesn't
                           replay the entire event backlog.

The loop:

  while True:
      env = wait_for_events(since=cursor, timeout_seconds=60)
      for event in env.events:
          log(event)              # placeholder for Claude hand-off
      cursor = env.next_cursor
      write(cursor_file, cursor)

This is intentionally a thin reference. Hand-off to a real Claude
Code session (so the agent can actually reply / update tasks /
etc) is the next iteration — wire it in `_handle_event` once a
concrete hand-off design exists (sketch in the doc).
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

MCP_URL = os.environ["AGENT_MCP_MCP_URL"]
PROJECT = os.environ["AGENT_MCP_PROJECT"]
AGENT_ID = os.environ["AGENT_MCP_AGENT_ID"]
BEARER = os.environ["AGENT_MCP_BEARER"]
CURSOR_FILE = Path(os.environ["AGENT_MCP_CURSOR_FILE"])

# How long to wait_for_events on each iteration. Matches the tool's
# server-side default; raising it above 900s is rejected by the
# server (tool clamps to MAX_TIMEOUT).
TIMEOUT_SECONDS = 60

# Polling delay between empty responses. wait_for_events with a 60s
# timeout self-throttles, but we add a small inter-iteration sleep
# so a tight server-side return loop (e.g. error → 0s return)
# doesn't peg CPU.
INTER_ITER_SLEEP_SECONDS = 0.5

# Reconnect backoff for transport-level failures. Bounded so a
# transient network blip doesn't take the daemon down for hours.
RECONNECT_BACKOFF_INITIAL_SECONDS = 2
RECONNECT_BACKOFF_MAX_SECONDS = 60


def _log(level: str, message: str, **extra: object) -> None:
    """Structured one-line log so journalctl shows readable output."""
    record = {"level": level, "agent": AGENT_ID, "project": PROJECT, "msg": message}
    record.update(extra)
    print(json.dumps(record, default=str), flush=True)


def _load_cursor() -> str:
    if CURSOR_FILE.exists():
        try:
            return CURSOR_FILE.read_text(encoding="utf-8").strip()
        except OSError as e:
            _log("warn", "cursor read failed", path=str(CURSOR_FILE), error=str(e))
    return ""


def _save_cursor(cursor: str) -> None:
    try:
        CURSOR_FILE.parent.mkdir(parents=True, exist_ok=True)
        CURSOR_FILE.write_text(cursor, encoding="utf-8")
    except OSError as e:
        _log("warn", "cursor write failed", path=str(CURSOR_FILE), error=str(e))


def _wait_for_events(cursor: str) -> dict:
    """Single POST /mcp call. Returns the decoded envelope:

        {"events": [...], "next_cursor": "<iso-ts>"}
    """
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "wait_for_events",
            "arguments": {
                "since": cursor,
                "timeout_seconds": TIMEOUT_SECONDS,
            },
        },
    }
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        MCP_URL,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {BEARER}",
            # Streamable HTTP responds in SSE shape; we accept JSON
            # too so an upstream change to json_response=True still
            # parses cleanly.
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        },
    )
    # Slightly longer client-side timeout than server-side so we
    # don't preempt a healthy long-poll just shy of its return.
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS + 30) as resp:
        raw = resp.read().decode("utf-8", errors="replace")

    # SSE body shape: lines prefixed `data: ` carry one JSON-RPC
    # response each. The framework currently sends exactly one.
    for line in raw.splitlines():
        if line.startswith("data: "):
            jrpc = json.loads(line[len("data: ") :])
            text = jrpc["result"]["content"][0]["text"]
            return json.loads(text)
    # Plain-JSON path (json_response=True): the body IS the response.
    try:
        jrpc = json.loads(raw)
        text = jrpc["result"]["content"][0]["text"]
        return json.loads(text)
    except (json.JSONDecodeError, KeyError, IndexError):
        return {"events": [], "next_cursor": cursor}


def _handle_event(event: dict) -> None:
    """Hook for hand-off to a real Claude Code session.

    Today this just structured-logs the event so the systemd journal
    captures it. To wire to a real Claude conversation, add an
    invocation here (subprocess.run claude --print --resume <session-id>
    "<event-summary>"), being careful about idempotency: the agent's
    response to one event mustn't be re-processed as a fresh event
    via send_agent_message → recipient's wait_for_events return.
    """
    _log(
        "event",
        "received",
        type=event.get("type"),
        timestamp=event.get("timestamp"),
        data_preview=str(event.get("data"))[:200],
    )


def _run_loop() -> None:
    cursor = _load_cursor()
    backoff = RECONNECT_BACKOFF_INITIAL_SECONDS
    _log("info", "daemon started", url=MCP_URL, cursor=cursor)

    while True:
        try:
            envelope = _wait_for_events(cursor)
        except urllib.error.URLError as e:
            _log("warn", "transport error, backing off", error=str(e), backoff=backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_BACKOFF_MAX_SECONDS)
            continue
        except Exception as e:  # pragma: no cover - belt and braces
            _log("error", "unexpected error, backing off", error=str(e), backoff=backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_BACKOFF_MAX_SECONDS)
            continue

        # Successful round-trip — reset backoff.
        backoff = RECONNECT_BACKOFF_INITIAL_SECONDS

        events = envelope.get("events", []) or []
        for event in events:
            try:
                _handle_event(event)
            except Exception as e:  # pragma: no cover - defensive
                _log(
                    "error", "event handler raised",
                    event_type=event.get("type"), error=str(e),
                )

        next_cursor = envelope.get("next_cursor") or cursor
        if next_cursor != cursor:
            cursor = next_cursor
            _save_cursor(cursor)

        time.sleep(INTER_ITER_SLEEP_SECONDS)


def _install_signal_handlers() -> None:
    def _shutdown(signum: int, _frame: object) -> None:
        _log("info", "received signal, exiting", signal=signum)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)


if __name__ == "__main__":
    _install_signal_handlers()
    _run_loop()
