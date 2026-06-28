"""Full E2E test harness for agent-mcp integration tests.

Candidate E from the 2026-06-01 architecture review. Replaces the
~40 lines of boilerplate every integration test today re-implements
(build ASGI app, mount mock_ollama transport, run lifespan,
extract admin token, register worker via raw SQL, bind bearer to
`request_auth_token`, wire jsonschema-validating dispatcher) with a
single async context manager:

    async with mcp_session(tmp_path) as admin:
        alice = await admin.create_worker("alice")
        await alice.assert_unauthorized("view_status", {})
        await admin.assert_tool_succeeds("view_status", {})

What you get
------------
* `mcp_session(tmp_path)` — async context manager. Builds the
  Starlette app with `create_app(project_dir=str(tmp_path / "project"))`,
  installs the same httpx mock transport the `mock_ollama` fixture
  uses (so RAG/embeddings calls don't reach the network), runs the
  lifespan via `starlette.testclient.TestClient`, and yields an
  `AdminClient`. On exit it tears the TestClient down and resets the
  process-wide singletons (`g.*`, write queue, engine cache) the same
  way the `reset_globals` fixture in `conftest.py` does.

* `AdminClient` — `.admin_token`, `.client` (httpx TestClient), and
  the MCP-call helpers `.call`, `.list_tools`,
  `.assert_tool_succeeds`, `.assert_unauthorized`,
  `.create_worker(agent_id)`, `.create_admin_agent(agent_id)`.

* `WorkerSession` — same MCP-call helpers, bound to a specific
  bearer (worker token, or the admin token for `create_admin_agent`).
  Tool calls go through the registered framework handler
  (`mcp_app_instance.request_handlers[CallToolRequest]`), which is
  what real SSE/JSON-RPC clients hit — so `tools/list` filtering
  (PR #55), jsonschema validation (PR #43), and the issue-H auth
  error path all run for every call.

What you don't get
------------------
* No real SSE transport / JSON-RPC framing. The harness drives the
  registered request handlers directly with the bearer bound on the
  `request_auth_token` ContextVar — that's the same path the HTTP
  middleware ends at, so behavior is wire-equivalent for everything
  except the streaming-protocol bits (which the existing
  `test_sse_handshake.py` covers structurally).

* No live tmux, no real Ollama. The `mock_ollama`-equivalent
  transport returns deterministic zero-vector embeddings; tools that
  shell out to tmux (`send_agent_message` with deliver_method="tmux")
  should pass `deliver_method="store"` per the existing test
  convention.

Concurrency note
----------------
Because `mcp_session` mutates module-level singletons
(`agent_mcp.core.globals`, the write queue, the SQLAlchemy engine
cache), tests using it must not run concurrently in the same process.
pytest-xdist runs each worker in its own process — that's fine. Inside
a worker, the harness's exit hook snapshots/restores per the same
convention as `conftest.py::reset_globals`.

Co-existence with the older pattern
-----------------------------------
This harness is opt-in. The pre-existing `client` and `app` fixtures
in `tests/conftest.py` still work; ~27 tests continue to use them.
Only the 4 example tests migrated in the same PR (per the architecture
review's "proof-of-life" mandate) use `mcp_session`.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as _dt
import os
import secrets
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, List, Optional

import httpx
import mcp.types as mcp_types
import pytest


# --- Internal helpers (mirroring the patterns scattered across tests/) ---


def _install_mock_ollama_transport(monkeypatch_or_stack: ExitStack) -> None:
    """Install the same in-process httpx mock transport that
    `conftest.py::mock_ollama` installs. Implemented against an
    AsyncExitStack so the harness can roll it back on exit without
    requiring callers to depend on the pytest `monkeypatch` fixture.
    """
    DIM = 1024

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/embeddings"):
            body = request.read()
            import json as _json

            data = _json.loads(body) if body else {}
            inputs = data.get("input", "")
            if isinstance(inputs, str):
                inputs = [inputs]
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [
                        {
                            "object": "embedding",
                            "embedding": [0.0] * DIM,
                            "index": i,
                        }
                        for i in range(len(inputs))
                    ],
                    "model": data.get("model", "mock-embed"),
                    "usage": {"prompt_tokens": 0, "total_tokens": 0},
                },
            )
        return httpx.Response(404, json={"error": "not found"})

    transport = httpx.MockTransport(_handler)
    original_client_init = httpx.Client.__init__
    original_async_init = httpx.AsyncClient.__init__

    def _patched_client_init(self, *args, **kwargs):
        kwargs.setdefault("transport", transport)
        original_client_init(self, *args, **kwargs)

    def _patched_async_init(self, *args, **kwargs):
        kwargs.setdefault("transport", transport)
        original_async_init(self, *args, **kwargs)

    httpx.Client.__init__ = _patched_client_init  # type: ignore[assignment]
    httpx.AsyncClient.__init__ = _patched_async_init  # type: ignore[assignment]

    def _restore() -> None:
        httpx.Client.__init__ = original_client_init  # type: ignore[assignment]
        httpx.AsyncClient.__init__ = original_async_init  # type: ignore[assignment]

    monkeypatch_or_stack.callback(_restore)


def _snapshot_and_reset_globals(stack: ExitStack) -> None:
    """Mirror of `conftest.py::reset_globals`. Snapshots the
    module-level singletons agent-mcp relies on so the harness's
    teardown restores them — keeps mcp_session safe to use multiple
    times per test process (across consecutive `async with`)."""
    from agent_mcp.core import globals as g
    from agent_mcp.db import engine as _engine
    from agent_mcp.db import write_queue as _wq

    _wq._global_write_queue = None
    _engine.reset_engine_cache()
    # `agent_event_signals` holds asyncio.Event objects bound to
    # whatever loop created them. Pytest-asyncio gives each test its
    # own loop; an Event leftover from a prior test cannot be awaited
    # in a new loop ("Event is bound to a different event loop").
    # Clear unconditionally — `signal_for(agent_id)` recreates lazily.
    g.agent_event_signals.clear()
    # PR-2 event-coord: per-agent serialization locks and out-of-band
    # event queues. Locks share the event-loop binding concern with
    # signals; queues are transient by design.
    g.agent_event_locks.clear()
    g.agent_event_queues.clear()
    # PR-B / v5.0.24: per-waiter queue registry (asyncio.Queue is also
    # loop-bound — same recreation pattern as signals).
    g.agent_event_waiters.clear()
    # Same loop-binding concern applies to `startup_complete_event`;
    # rebuild it so the lifespan inside `mcp_session` signals on the
    # current loop and bg-task waiters can `await` without raising.
    g.reset_startup_complete_event()

    snapshot = {
        "connections": dict(g.connections),
        "active_agents": dict(g.active_agents),
        "tasks": dict(g.tasks),
        "file_map": dict(g.file_map),
        "agent_working_dirs": dict(g.agent_working_dirs),
        "agent_tmux_sessions": dict(g.agent_tmux_sessions),
        "audit_log": list(g.audit_log),
        "openai_client_instance": g.openai_client_instance,
        "global_vss_load_tested": g.global_vss_load_tested,
        "global_vss_load_successful": g.global_vss_load_successful,
    }

    def _restore() -> None:
        g.connections.clear()
        g.connections.update(snapshot["connections"])
        g.active_agents.clear()
        g.active_agents.update(snapshot["active_agents"])
        g.tasks.clear()
        g.tasks.update(snapshot["tasks"])
        g.file_map.clear()
        g.file_map.update(snapshot["file_map"])
        g.agent_working_dirs.clear()
        g.agent_working_dirs.update(snapshot["agent_working_dirs"])
        g.agent_tmux_sessions.clear()
        g.agent_tmux_sessions.update(snapshot["agent_tmux_sessions"])
        g.audit_log.clear()
        g.audit_log.extend(snapshot["audit_log"])
        g.openai_client_instance = snapshot["openai_client_instance"]
        g.global_vss_load_tested = snapshot["global_vss_load_tested"]
        g.global_vss_load_successful = snapshot["global_vss_load_successful"]
        _wq._global_write_queue = None
        _engine.reset_engine_cache()
        # Drop any signals created during this test so the next test
        # (different event loop) starts with a fresh registry.
        g.agent_event_signals.clear()
        g.agent_event_locks.clear()
        g.agent_event_queues.clear()
        g.agent_event_waiters.clear()
        g.reset_startup_complete_event()

    stack.callback(_restore)


# --- Session classes ---


def _result_text(result: List[mcp_types.TextContent]) -> str:
    """Concatenate text blocks from a tool-call result."""
    if not result:
        return ""
    parts: list[str] = []
    for block in result:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def _first_text(result: List[mcp_types.TextContent]) -> str:
    if not result:
        return ""
    return getattr(result[0], "text", "") or ""


def seed_agent_rows(*agent_ids: str) -> None:
    """INSERT OR IGNORE a minimal `agents` row for each id.

    Tests that bypass the public tool surface and write directly to
    tables that FK -> agents (`agent_messages`, `mcp_sessions`,
    `claude_code_sessions`, `tasks.assigned_to`) need the referenced
    `agents.agent_id` to exist or the insert raises
    `FOREIGN KEY constraint failed`.

    `admin` is pre-seeded by lifespan startup; only worker agents
    need this helper. The synthetic rows mirror what `create_worker`
    does, minus the per-token bookkeeping — `token` here is generated
    deterministically from the agent_id so re-calling the helper for
    the same id is a no-op (INSERT OR IGNORE on the `agent_id` UNIQUE
    constraint, but the PK on `token` also needs to be unique per
    distinct id).
    """
    from agent_mcp.db.connection import get_db_connection

    if not agent_ids:
        return
    now = _dt.datetime.now().isoformat()
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        for agent_id in agent_ids:
            cursor.execute(
                "INSERT OR IGNORE INTO agents (token, agent_id, "
                "capabilities, created_at, status, working_directory, "
                "color, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    f"__test_seed_{agent_id}",
                    agent_id,
                    "[]",
                    now,
                    "active",
                    "/tmp",
                    "#888",
                    now,
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _is_unauthorized(text: str) -> bool:
    if not text:
        return False
    head = text.strip().lower()
    return head.startswith("unauthorized") or head.startswith("invalid") and (
        "token" in head
    )


@dataclass
class WorkerSession:
    """A bearer-bound MCP session.

    Holds a token, the parent AdminClient (for shared TestClient
    access), and the agent_id this bearer authenticates as. All MCP
    calls go through the registered framework handlers with
    `request_auth_token` set to this session's token — mirroring the
    contextvar an HTTP request would set via the Authorization-header
    middleware in production.

    ``is_admin_caller`` toggles the harness's operator-session stamping
    on the registry contextvars during ``.call`` / ``.list_tools``.
    Set by :meth:`AdminClient.create_admin_agent` (and the AdminClient
    itself) so admin-side tool calls satisfy
    ``@requires_role("operator")`` after retire-system-token Wave 1
    removed the system-bearer god-key from ``verify_token``.
    """

    token: str
    agent_id: str
    _admin: "AdminClient"
    is_admin_caller: bool = False

    # --- Low-level call surface ---

    async def call(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> List[mcp_types.TextContent]:
        """Invoke a tool through the registered CallToolRequest handler.

        Same path real SSE/JSON-RPC clients take. Bearer is bound via
        `request_auth_token` so the dispatcher's Q6e fallback fills
        `arguments.token` if absent. Returns the raw content blocks;
        use the `assert_*` helpers for wire-isError semantics.

        retire-system-token Wave 1: when invoked through the
        :class:`AdminClient` (i.e. this session represents the
        harness's manager-role admin agent), the call also stamps
        ``operator_session_active=True`` and the operator user_id on
        the registry contextvars. Without it,
        ``@requires_role("operator")``-gated tools would reject the
        call because the system-bearer fallback that previously
        admitted operator-tier callers is gone. The harness operator
        identity mirrors what the REST seam (``routes.py``) does for
        cookie-authenticated dashboard mutations.

        Wave 6 PR 5: for admin-caller sessions calling a tool whose
        declared visibility is ``"operator"`` AND whose impl has
        been migrated to take a :class:`Principal` kwarg, bypass the
        MCP framework handler and call :func:`dispatch_tool_call`
        directly with an explicit operator-session Principal. The
        bridge's contextvar derivation would otherwise prefer the
        admin bearer (more specific identity for audit attribution)
        and return ``agent_bearer`` — which fails the migrated
        tool's inline ``principal.has_role("operator")`` check. The
        explicit principal kwarg short-circuits the bridge so the
        operator-tier tool sees the operator identity. Tools whose
        declared visibility is ``"any"`` / ``"manager"`` /
        ``"worker-if-toggled:..."`` still flow through the framework
        handler with bridge-derived ``agent_bearer`` — preserving
        the bridge's audit-attribution contract (PR 0's add_task_note
        demo expects ``author="admin"`` from the agent_bearer path).
        Legacy (unmigrated) tools also use the framework handler so
        their ``@requires_role`` decorators keep reading the
        ContextVars unchanged.
        """
        from agent_mcp.tools.registry import (
            request_auth_token,
            operator_session_active,
            operator_user_id,
            operator_project_name,
            dispatch_tool_call,
            tool_implementations,
        )
        from agent_mcp.core.tool_result import render_as_text_content
        from agent_mcp.tools.access import TOOL_ACCESS as _TOOL_ACCESS

        # Wave 6 PR 5 — operator-tool short-circuit for admin caller.
        # Only triggers when ALL three conditions hold:
        #   1. This session represents the harness's admin (the
        #      pre-Wave-6 surface that stood in for "operator").
        #   2. The tool's declared visibility is ``"operator"`` —
        #      we leave ``"any"`` / ``"manager"`` tools on the
        #      bridge-derived path so PR 0's agent_bearer attribution
        #      contract holds.
        #   3. The tool impl is migrated (accepts a ``principal``
        #      kwarg) — same predicate the bridge uses. Skipping
        #      unmigrated tools keeps their decorator-based
        #      ContextVar gates unchanged.
        if self.is_admin_caller:
            access_level = _TOOL_ACCESS.get(tool_name)
            if access_level == "operator":
                impl = tool_implementations.get(tool_name)
                tool_takes_principal = False
                if impl is not None:
                    try:
                        import inspect as _inspect
                        tool_takes_principal = (
                            "principal" in _inspect.signature(impl).parameters
                        )
                    except (TypeError, ValueError):  # pragma: no cover
                        tool_takes_principal = False
                if tool_takes_principal:
                    from agent_mcp.core.principal import Principal

                    principal = Principal(
                        kind="operator_session",
                        user_id=_HARNESS_OPERATOR_ID,
                        agent_id=None,
                        sysadmin=False,
                        project_name="harness",
                        project_role="operator",
                        agent_role=None,
                        can_wake_loop=False,
                        source_token=None,
                    )
                    cv_token = request_auth_token.set(self.token)
                    try:
                        result = await dispatch_tool_call(
                            tool_name, arguments, principal=principal,
                        )
                    finally:
                        request_auth_token.reset(cv_token)
                    self._last_is_error = False
                    return render_as_text_content(result)

        handler = self._admin._call_tool_handler()
        req = mcp_types.CallToolRequest(
            method="tools/call",
            params=mcp_types.CallToolRequestParams(
                name=tool_name, arguments=arguments
            ),
        )
        cv_token = request_auth_token.set(self.token)
        # The harness sets ``operator_session_active=True`` for the
        # whole session by default (so per-tool admin gates admit by
        # default — matching the pre-Wave-1 god-key behaviour). For a
        # worker session, we explicitly clear the contextvars before
        # dispatch so the role-based filters see a worker request,
        # not an operator one.
        cv_op_session = None
        cv_op_user = None
        cv_op_project = None
        if not (self.is_admin_caller or self is self._admin):
            cv_op_session = operator_session_active.set(False)
            cv_op_user = operator_user_id.set(None)
            cv_op_project = operator_project_name.set(None)
        try:
            server_result = await handler(req)
        finally:
            request_auth_token.reset(cv_token)
            if cv_op_session is not None:
                operator_session_active.reset(cv_op_session)
            if cv_op_user is not None:
                operator_user_id.reset(cv_op_user)
            if cv_op_project is not None:
                operator_project_name.reset(cv_op_project)

        inner = (
            server_result.root
            if hasattr(server_result, "root")
            else server_result
        )
        # Stash isError for assert_tool_succeeds.
        self._last_is_error = bool(getattr(inner, "isError", False))
        return list(getattr(inner, "content", None) or [])

    async def list_tools(self) -> List[mcp_types.Tool]:
        """`tools/list` as this bearer sees it (admin or worker filter
        per PR #55).

        retire-system-token Wave 1: when invoked through the
        :class:`AdminClient`, the call stamps
        ``operator_session_active=True`` so the admin-tier branch of
        the visibility filter (which now reads the operator-session
        contextvar instead of ``verify_token(.., "admin")`` on the
        system bearer) takes effect.
        """
        from agent_mcp.tools.registry import (
            request_auth_token,
            operator_session_active,
            operator_user_id,
            operator_project_name,
        )

        handler = self._admin._list_tools_handler()
        req = mcp_types.ListToolsRequest(method="tools/list")
        cv_token = request_auth_token.set(self.token)
        cv_op_session = None
        cv_op_user = None
        cv_op_project = None
        if not (self.is_admin_caller or self is self._admin):
            cv_op_session = operator_session_active.set(False)
            cv_op_user = operator_user_id.set(None)
            cv_op_project = operator_project_name.set(None)
        try:
            result = await handler(req)
        finally:
            request_auth_token.reset(cv_token)
            if cv_op_session is not None:
                operator_session_active.reset(cv_op_session)
            if cv_op_user is not None:
                operator_user_id.reset(cv_op_user)
            if cv_op_project is not None:
                operator_project_name.reset(cv_op_project)
        inner = result.root if hasattr(result, "root") else result
        return list(getattr(inner, "tools", []) or [])

    # --- Assertion helpers ---

    async def assert_tool_succeeds(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> List[mcp_types.TextContent]:
        """Call `tool_name`; pytest.fail if isError or the response
        text matches the Unauthorized shape. Returns the content blocks
        on success so callers can assert on the result text.
        """
        result = await self.call(tool_name, arguments)
        text = _first_text(result)
        if getattr(self, "_last_is_error", False):
            pytest.fail(
                f"{tool_name}({arguments!r}) returned isError=true: {text}"
            )
        if _is_unauthorized(text):
            pytest.fail(
                f"{tool_name}({arguments!r}) returned Unauthorized: {text}"
            )
        return result

    # --- Event-driven helpers (plan Phase 5) ---

    async def wait_for_event(
        self,
        since: Optional[str] = None,
        timeout: int = 5,
    ) -> dict[str, Any]:
        """Call the `wait_for_events` MCP tool for this session and
        return the parsed envelope dict.

        Thin wrapper that hides the JSON-text decode + content-block
        unwrap; tests can write::

            env = await alice.wait_for_event(since=ts, timeout=2)
            assert env["events"][0]["type"] == "message"

        rather than re-implementing the unwrap on every call site.
        """
        import json as _json

        args: dict[str, Any] = {"timeout_seconds": int(timeout)}
        if since is not None:
            args["since"] = since
        result = await self.call("wait_for_events", args)
        if not result:
            return {"events": [], "next_cursor": since or ""}
        text = getattr(result[0], "text", "") or ""
        try:
            return _json.loads(text)
        except _json.JSONDecodeError:
            # Unexpected non-JSON response — surface the raw text so the
            # caller's assertion has something to grip.
            return {"events": [], "next_cursor": since or "", "raw": text}

    async def _read_resource(self, uri: str) -> str:
        """Hit `resources/read` for `uri` through the registered MCP
        handler with this session's bearer; return the first text
        block, empty string if none."""
        from agent_mcp.tools.registry import request_auth_token
        from pydantic_core import Url

        handler = self._admin._mcp_app_instance().request_handlers[
            mcp_types.ReadResourceRequest
        ]
        req = mcp_types.ReadResourceRequest(
            method="resources/read",
            params=mcp_types.ReadResourceRequestParams(uri=Url(uri)),
        )
        tok = request_auth_token.set(self.token)
        try:
            result = await handler(req)
        finally:
            request_auth_token.reset(tok)
        inner = result.root if hasattr(result, "root") else result
        for content in getattr(inner, "contents", []) or []:
            text = getattr(content, "text", None)
            if isinstance(text, str):
                return text
        return ""

    async def read_inbox(self) -> dict[str, Any]:
        """`resources/read` on `agent-mcp://inbox/<agent_id>` →
        parsed JSON envelope (same shape as `wait_for_event`)."""
        import json as _json
        text = await self._read_resource(
            f"agent-mcp://inbox/{self.agent_id}"
        )
        return _json.loads(text) if text else {
            "events": [], "next_cursor": ""
        }

    async def read_status(self) -> dict[str, Any]:
        """`resources/read` on `agent-mcp://status/<agent_id>` →
        parsed counter dict."""
        import json as _json
        text = await self._read_resource(
            f"agent-mcp://status/{self.agent_id}"
        )
        return _json.loads(text) if text else {}

    async def assert_unauthorized(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> None:
        """Call `tool_name`; pytest.fail unless the wire response is an
        Unauthorized text block (either via the issue-H isError path or
        a plain TextContent starting with 'Unauthorized'/'Invalid …
        token').

        Note: asserts on the *wire response*, not on a Python exception.
        That keeps the helper robust if auth-decorator refactors (the
        parallel A-agent work) change the internal exception type while
        keeping the wire shape (`isError=true` + 'Unauthorized: …' text)
        identical.
        """
        result = await self.call(tool_name, arguments)
        text = _first_text(result)
        is_error = getattr(self, "_last_is_error", False)
        if is_error and _is_unauthorized(text):
            return
        # Issue-H may surface as isError=true with the exception's
        # message verbatim; the text-prefix check covers that.
        if _is_unauthorized(text):
            return
        pytest.fail(
            f"{tool_name}({arguments!r}) was expected to be Unauthorized; "
            f"got isError={is_error} text={text!r}"
        )


#: Operator id the harness signs the forwarding header for. Stable
#: across the test suite so audit-log assertions can name the actor
#: explicitly.
_HARNESS_OPERATOR_ID = "test-harness-operator"

#: agent_id of the manager-role agent the harness seeds at startup.
#: The harness seeds a real per-agent row in the ``agents`` table —
#: the agent_id stays ``"admin"`` so the rest of the codebase
#: (~50 callsites that special-case the literal ``"admin"`` for
#: routing, filtering, ownership checks) keeps working unchanged.
#:
#: Migration 0014 deletes the synthetic ``admin`` row at startup; the
#: harness re-inserts a fresh row inside ``mcp_session`` so the
#: principal has a real DB row. The
#: ``test_db_admin_pseudo_agent.py`` suite (which pins the
#: zero-rows-after-fresh-init invariant) queries before the harness
#: seeds so its assertions stay valid.
_HARNESS_ADMIN_AGENT_ID = "admin"


def _seed_harness_admin_agent() -> str:
    """Insert a manager-role agent row + return its bearer token.

    The harness's ``AdminClient`` uses this token for MCP tool calls
    that drop through ``request_auth_token`` into a real
    ``verify_token`` chain. Manager-role tokens satisfy
    ``verify_token(.., "manager")`` and the agent path of
    ``verify_token(.., "agent")``; operator-tier tools are gated
    separately via the ``operator_session_active`` contextvar (which
    the REST seam and harness REST helpers stamp).

    Idempotent on agent_id: a re-seed within the same test process
    returns the previously-inserted token. Each call to
    :func:`mcp_session` runs inside a fresh tmp DB, so the
    cross-test concern is bounded by the engine-cache reset.
    """
    from agent_mcp.core import globals as g
    from agent_mcp.db.connection import get_db_connection

    now = _dt.datetime.now().isoformat()
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT token FROM agents WHERE agent_id = ?",
            (_HARNESS_ADMIN_AGENT_ID,),
        )
        existing = cursor.fetchone()
        if existing and existing["token"]:
            token = existing["token"]
        else:
            token = secrets.token_hex(16)
            cursor.execute(
                "INSERT INTO agents (token, agent_id, capabilities, "
                "created_at, status, working_directory, color, "
                "updated_at, agent_role) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    token,
                    _HARNESS_ADMIN_AGENT_ID,
                    "[]",
                    now,
                    "active",
                    "/tmp",
                    "#888",
                    now,
                    "manager",
                ),
            )
            conn.commit()
    finally:
        conn.close()

    # Also surface the agent in the in-memory cache so ``verify_token(..,
    # "agent")`` admits without a DB roundtrip on the hot path.
    g.active_agents[token] = {
        "agent_id": _HARNESS_ADMIN_AGENT_ID,
        "status": "active",
        "created_at": now,
        "capabilities": [],
        "agent_role": "manager",
    }
    return token


class AdminClient(WorkerSession):
    """The session yielded by `mcp_session`. Adds factory methods
    for spawning worker / admin-agent sessions and exposes the
    underlying TestClient for REST assertions.

    retire-system-token Wave 1: the harness no longer authenticates
    by handing out ``g.system_token`` (the god-key is gone). Instead
    we mint a real per-agent manager-role token for MCP tool calls
    AND configure the forwarding-header HMAC key on app startup so
    REST helper calls (``.get`` / ``.post`` etc.) attach a signed
    ``X-Agent-MCP-Forwarded-Operator`` header — the same path the
    router will use post-Wave-2.

    ``self.admin_token`` is preserved as an attribute and still
    points at a credential the test suite can use for ``token=...``
    body fields and ``request_auth_token``-fallback MCP calls. Its
    value is now the per-agent manager bearer (not ``g.system_token``).
    """

    def __init__(
        self,
        admin_token: str,
        test_client: Any,
        forwarding_hmac_key: bytes,
    ) -> None:
        super().__init__(
            token=admin_token,
            agent_id=_HARNESS_ADMIN_AGENT_ID,
            _admin=self,
            is_admin_caller=True,
        )
        self.admin_token = admin_token
        self.client = test_client
        self._mcp_app = None
        self._forwarding_hmac_key = forwarding_hmac_key

    # --- Forwarding-header helpers ---------------------------------

    def forwarding_header(
        self, operator_id: str = _HARNESS_OPERATOR_ID
    ) -> dict[str, str]:
        """Return a one-shot signed forwarding header for ``operator_id``.

        Wraps ``agent_mcp.app.forwarding_header.sign`` against the
        per-test HMAC key the harness stamped on
        ``g.forwarding_hmac_key`` at startup. Tests that need to
        authenticate against backend REST routes via the router
        path (cookie → signed header) use this helper to build the
        header on each call.
        """
        from agent_mcp.app import forwarding_header as _fh

        return {
            _fh.HEADER_NAME: _fh.sign(
                operator_id, self._forwarding_hmac_key, ttl_sec=30
            )
        }

    # --- Authenticated GET helper for Wave-1+ REST routes ----------

    def get(self, url: str, **kwargs: Any):
        """Authenticated convenience wrapper around `self.client.get`.

        Wave 1 of prancy-napping-pie put auth-less GET endpoints (notably
        ``/api/tokens`` and ``/api/all-data``) behind
        ``require_operator_session``. retire-system-token Wave 1
        removed the system-bearer legacy fallback from the dep; we
        now authenticate via the signed forwarding header (the same
        path the router will use post-Wave-2).

        Tests can still call ``admin.client.get(url, ...)`` directly
        when they want to assert the auth-less wire shape (e.g. the
        RED 401 tests).
        """
        headers = dict(kwargs.pop("headers", {}) or {})
        # Forwarding header authenticates as the harness operator.
        for k, v in self.forwarding_header().items():
            headers.setdefault(k, v)
        return self.client.get(url, headers=headers, **kwargs)

    def post(self, url: str, **kwargs: Any):
        """Authenticated convenience wrapper around ``client.post``.

        Mirrors :meth:`get` — attaches a signed forwarding header so
        REST mutation routes guarded by ``require_operator_session``
        pass auth.
        """
        headers = dict(kwargs.pop("headers", {}) or {})
        for k, v in self.forwarding_header().items():
            headers.setdefault(k, v)
        return self.client.post(url, headers=headers, **kwargs)

    def request(self, method: str, url: str, **kwargs: Any):
        """Authenticated convenience wrapper around ``client.request``.

        Same forwarding-header attach pattern as :meth:`get` /
        :meth:`post`, for tests that drive PUT / DELETE / PATCH.
        """
        headers = dict(kwargs.pop("headers", {}) or {})
        for k, v in self.forwarding_header().items():
            headers.setdefault(k, v)
        return self.client.request(method, url, headers=headers, **kwargs)

    # --- Lazy handler accessors (avoid importing main_app at module load) ---

    def _mcp_app_instance(self):
        if self._mcp_app is None:
            from agent_mcp.app.main_app import mcp_app_instance

            self._mcp_app = mcp_app_instance
        return self._mcp_app

    def _call_tool_handler(self):
        return self._mcp_app_instance().request_handlers[
            mcp_types.CallToolRequest
        ]

    def _list_tools_handler(self):
        return self._mcp_app_instance().request_handlers[
            mcp_types.ListToolsRequest
        ]

    # --- Agent registration ---

    async def create_worker(self, agent_id: str) -> WorkerSession:
        """Register a worker via the same raw-SQL insert the existing
        tests use (`tests/test_worker_peer_messaging.py` and
        siblings). Returns a `WorkerSession` bound to a fresh
        per-agent token; subsequent `.call`/`.list_tools` on the
        returned session run with the worker role.
        """
        from agent_mcp.core import globals as g
        from agent_mcp.db.connection import get_db_connection

        worker_token = secrets.token_hex(16)
        now = _dt.datetime.now().isoformat()

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO agents (token, agent_id, capabilities, "
            "created_at, status, working_directory, color, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                worker_token,
                agent_id,
                "[]",
                now,
                "active",
                "/tmp",
                "#888",
                now,
            ),
        )
        conn.commit()
        conn.close()

        g.active_agents[worker_token] = {
            "agent_id": agent_id,
            "status": "active",
            "created_at": now,
            "capabilities": [],
        }
        return WorkerSession(
            token=worker_token, agent_id=agent_id, _admin=self
        )

    async def create_admin_agent(self, agent_id: str) -> WorkerSession:
        """Register an agent record bound to the admin token. Useful
        for tests that want to simulate an admin-driven MCP caller
        distinct from the AdminClient itself (e.g. populating
        `active_agents` with a named admin entry while still using the
        admin bearer for auth)."""
        from agent_mcp.core import globals as g

        g.active_agents[self.admin_token] = {
            "agent_id": agent_id,
            "status": "active",
            "created_at": _dt.datetime.now().isoformat(),
            "capabilities": ["admin"],
        }
        return WorkerSession(
            token=self.admin_token,
            agent_id=agent_id,
            _admin=self,
            is_admin_caller=True,
        )

    # --- Builder helpers for common project_context shapes ---

    def set_toggle(self, key: str, value: str) -> None:
        """Seed or update a `config_*` toggle via the REST memory API
        (same pattern existing tests use)."""
        r = self.client.post(
            "/api/memories",
            json={
                "token": self.admin_token,
                "context_key": key,
                "context_value": value,
            },
        )
        if r.status_code == 409:
            r = self.client.request(
                "PUT",
                f"/api/memories/{key}",
                json={
                    "token": self.admin_token,
                    "context_value": value,
                },
            )
        assert r.status_code == 200, r.text

    def task_row(self, task_id: str) -> dict | None:
        """Look up a task row in `/api/tasks` by task_id. Returns None
        if absent."""
        listing = self.client.get("/api/tasks").json()
        if isinstance(listing, dict):
            listing = listing.get("tasks", [])
        for entry in listing:
            if entry.get("task_id") == task_id:
                return entry
        return None


# --- Public entry point ---


@contextlib.contextmanager
def with_principal(principal):
    """Stamp a :class:`agent_mcp.core.principal.Principal` on the
    legacy ContextVars for the duration of the ``with`` block.

    Wave 6 PR 0 — the new test-side helper for the Principal value
    type. The harness's per-session contextvar stamping (set up
    inside :func:`mcp_session`) deprecated in favour of this; the
    older approach still works for now (the bridge in
    ``dispatch_tool_call`` falls back to ContextVars when no
    Principal is in hand), so existing tests don't need to migrate
    in this PR.

    Usage::

        from agent_mcp.core.principal import Principal
        from tests.harness import with_principal

        p = Principal(
            kind="operator_session",
            user_id="alice",
            agent_id=None,
            sysadmin=False,
            project_name="proj-a",
            project_role="operator",
            agent_role=None,
            can_wake_loop=False,
            source_token=None,
        )
        with with_principal(p):
            ... # in-process tool calls that consult ContextVars / dispatch
            ... # see p as the calling Principal

    For ``operator_session`` and ``forwarding_header`` kinds, stamps
    ``operator_session_active=True`` so legacy decorators that read
    the ContextVar admit. For ``agent_bearer`` kinds, stamps
    ``request_auth_token`` from ``principal.source_token`` so
    bearer-based gates see the right agent.

    Resets in LIFO order on block exit. Safe to nest; each nested
    use returns a fresh handle that resets its own scope.

    .. deprecated:: 6.0
       The older per-session contextvar stamping in
       :func:`mcp_session` continues to work for the legacy bridge;
       new tests should use :func:`with_principal`. The shim helper
       deletes in Wave 6 PR 6 alongside the ContextVars themselves.
    """
    from agent_mcp.tools.registry import (
        operator_session_active,
        operator_user_id,
        operator_project_name,
        request_auth_token,
    )

    cv_op_session = None
    cv_op_user = None
    cv_op_project = None
    cv_token = None
    try:
        if principal.kind in ("operator_session", "forwarding_header"):
            cv_op_session = operator_session_active.set(True)
            if principal.user_id is not None:
                cv_op_user = operator_user_id.set(principal.user_id)
            if principal.project_name is not None:
                cv_op_project = operator_project_name.set(principal.project_name)
        elif principal.kind == "agent_bearer":
            cv_op_session = operator_session_active.set(False)
            if principal.source_token:
                cv_token = request_auth_token.set(principal.source_token)
        yield principal
    finally:
        if cv_token is not None:
            request_auth_token.reset(cv_token)
        if cv_op_project is not None:
            operator_project_name.reset(cv_op_project)
        if cv_op_user is not None:
            operator_user_id.reset(cv_op_user)
        if cv_op_session is not None:
            operator_session_active.reset(cv_op_session)


@contextlib.asynccontextmanager
async def mcp_session(tmp_path: Path) -> AsyncIterator[AdminClient]:
    """Build the app, run lifespan, yield an AdminClient.

    Usage:
        async with mcp_session(tmp_path) as admin:
            alice = await admin.create_worker("alice")
            ...

    Implementation notes:
      * Runs the Starlette TestClient lifespan in a worker thread so
        it doesn't block the running asyncio loop.
      * Installs the same mock-ollama httpx transport on entry; rolls
        back on exit so subsequent tests with a real `httpx.Client`
        don't get the mock.
      * Snapshots and restores process-wide singletons (mirrors
        `conftest.py::reset_globals`).
    """
    # Env isolation — match `conftest.py::_isolate_env` so the harness
    # works equally well from tests that don't use the autouse fixture
    # (e.g. callers that supply their own `tmp_path` without
    # `conftest.py`'s injection). ``os`` is imported at module top.
    env_snapshot = {
        "OPENAI_API_KEY": os.environ.get("OPENAI_API_KEY"),
        "DOTENV_PATH": os.environ.get("DOTENV_PATH"),
        "MCP_PROJECT_DIR": os.environ.get("MCP_PROJECT_DIR"),
    }
    os.environ["OPENAI_API_KEY"] = ""
    os.environ["DOTENV_PATH"] = "/dev/null"
    os.environ.pop("MCP_PROJECT_DIR", None)

    stack = ExitStack()
    try:
        # Reset and snapshot globals so the harness is safe to nest
        # under tests that don't use conftest's reset_globals fixture.
        _snapshot_and_reset_globals(stack)
        _install_mock_ollama_transport(stack)

        project_dir = tmp_path / "project"
        project_dir.mkdir(exist_ok=True)

        from agent_mcp.app.main_app import create_app
        from starlette.testclient import TestClient

        app = create_app(project_dir=str(project_dir))

        # TestClient.__enter__ runs the lifespan synchronously
        # (blocking the event loop) — push it onto a worker thread so
        # we don't deadlock under pytest-asyncio.
        def _start() -> TestClient:
            tc = TestClient(app)
            tc.__enter__()
            return tc

        test_client = await asyncio.to_thread(_start)

        def _close() -> None:
            test_client.__exit__(None, None, None)

        # TestClient teardown also blocks the loop on lifespan shutdown;
        # the stack runs callbacks synchronously, so wrap with to_thread
        # via a helper closure executed in the outer finally block.
        stack.callback(_close)

        # retire-system-token Wave 1: the harness no longer hands
        # out ``g.system_token`` as the bearer (it's no longer
        # accepted). Instead:
        #   1. Stamp a fresh per-test HMAC key on
        #      ``g.forwarding_hmac_key`` so REST helpers can sign a
        #      forwarding header.
        #   2. Mint a real manager-role agent row in the agents table
        #      so MCP tool calls that need a bearer get a valid
        #      per-agent token.
        #   3. Stamp ``operator_session_active=True`` for the lifetime
        #      of the harness session so per-tool admin gates (which
        #      now consult the contextvar instead of a system-bearer
        #      check) admit by default — every test running under the
        #      harness is "the operator at the dashboard" unless it
        #      explicitly resets the var to drive a worker call.
        from agent_mcp.core import globals as g
        from agent_mcp.tools.registry import (
            operator_session_active,
            operator_user_id,
            operator_project_name,
        )

        g.forwarding_hmac_key = os.urandom(32)

        admin_token = _seed_harness_admin_agent()

        # ContextVar resets MUST happen in the same Context the
        # ``.set()`` was made in. The ExitStack teardown runs via
        # ``asyncio.to_thread`` (so the synchronous TestClient lifespan
        # shutdown doesn't block the event loop), which is a different
        # Context — calling ``contextvar.reset(token)`` from there
        # raises ``ValueError: Token was created in a different Context``.
        # So we manage the reset in this async generator's try/finally
        # rather than on the stack.
        cv_op_session = operator_session_active.set(True)
        cv_op_user = operator_user_id.set(_HARNESS_OPERATOR_ID)
        cv_op_project = operator_project_name.set("harness")

        admin = AdminClient(
            admin_token=admin_token,
            test_client=test_client,
            forwarding_hmac_key=g.forwarding_hmac_key,
        )
        try:
            yield admin
        finally:
            # Reset contextvars in the same context they were set in
            # (this coroutine), BEFORE the to_thread stack.close.
            operator_project_name.reset(cv_op_project)
            operator_user_id.reset(cv_op_user)
            operator_session_active.reset(cv_op_session)
    finally:
        # Restore env vars first; teardown of app comes via the stack.
        for k, v in env_snapshot.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        await asyncio.to_thread(stack.close)
        # ExitStack.close drains callbacks in LIFO order. Running it
        # via to_thread keeps the synchronous TestClient teardown
        # (which blocks on lifespan shutdown) off the event loop.
