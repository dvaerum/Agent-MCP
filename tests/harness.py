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
a worker, the harness's exit hook snapshots/restores via
`tests.conftest.reset_and_snapshot_globals` — the same seam
`conftest.py::reset_globals` uses (arch-r6 #2: this used to be a
standalone byte-for-byte copy; see that module's docstring).

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
import time
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator, List, Optional

import mcp.types as mcp_types
import pytest

from tests.conftest import (
    install_mock_ollama,
    reset_and_snapshot_globals,
    seed_agent_row,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from agent_mcp.core.principal import AgentRole, Principal, PrincipalKind


async def assert_ran_off_event_loop(
    started_at: "List[float]",
    loop_free_at: "List[float]",
    *,
    block_sec: float,
    what: str,
    liveness_timeout: float = 5.0,
) -> None:
    """Assert a blocking probe ran OFF the event loop — robust to slow CI.

    Shared by the OBS-R34 / BL-R7 "must run via ``asyncio.to_thread``"
    regression tests. Each caller:

    * stubs a blocking ``systemctl`` verb that, in the worker thread,
      appends ``time.monotonic()`` to ``started_at`` then sleeps
      ``block_sec`` (bounded, so a regression can't hang forever), and
    * runs a probe coroutine that appends ``time.monotonic()`` to
      ``loop_free_at`` the instant it observes the blocking call began.

    Why this instead of the old ``await asyncio.sleep(0.15); assert
    loop_free_at is not None; assert (loop_free_at - t0) < 0.25`` pattern:
    the FIRST request under test triggers lazy init (Alembic migrations)
    whose duration varies wildly on a loaded runner, so a fixed 0.15s budget
    for routing to *reach* the blocking call flakes, and measuring the
    loop-free gap from ``t0`` (before routing) folds that variable init time
    into the result. Here we wait a generous bound for the probe to begin,
    then measure the gap from when the blocking call *actually started*.
    Off-loop: the loop is free, so the probe records within a few ms.
    On-loop: the loop is frozen for ~``block_sec``, so the gap approaches
    ``block_sec`` and the assertion fails — preserving the regression's teeth.
    """
    deadline = time.monotonic() + liveness_timeout
    while not loop_free_at and time.monotonic() < deadline:
        await asyncio.sleep(0.005)
    assert loop_free_at, f"{what} never began within {liveness_timeout:.0f}s"
    gap = loop_free_at[0] - started_at[0]
    assert gap < block_sec / 2, (
        f"event loop was blocked ~{gap:.3f}s after {what} began — it must "
        f"run off-loop via asyncio.to_thread (block_sec={block_sec})"
    )


# --- Internal helpers (mirroring the patterns scattered across tests/) ---
#
# arch-r6 #2: the globals-reset and mock-ollama-transport helpers that
# used to live here are now `tests.conftest.reset_and_snapshot_globals`
# and `tests.conftest.install_mock_ollama` (imported above) — this file
# had standalone byte-for-byte copies of both; see conftest.py's module
# docstring for why that was a problem.


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


def seed_config_context_as_sysadmin(key: str, value: Any) -> None:
    """Seed a ``project_context`` row the way a SYSADMIN write would land.

    ``config_aoe_*`` keys became sysadmin-only to write (pentest R8-F1:
    they configure a machine-level outbound integration target). The
    per-project-operator REST seam (``admin.client.post("/api/memories",
    {"token": admin_token, ...})``) now 403s on those keys, so feature /
    redaction tests that merely need the row PRESENT seed it here instead
    of through the operator path.

    Writes directly on the test DB via the same repository + JSON value
    encoding the tool uses (``json.dumps`` — project_context stores every
    value JSON-encoded), stamping ``created_by="sysadmin"``. Faithful to a
    sysadmin ``update_project_context`` for the READ paths these tests
    exercise (``_read_ctx`` / live-context / ``/api/context-data``).
    """
    import json as _json

    from agent_mcp.db.connection import get_db_connection
    from agent_mcp.repositories import project_context_repository as _pc_repo

    conn = get_db_connection()
    try:
        # The repo reads/writes through an open CURSOR (so pending writes
        # are visible within the txn) — mirror the tool's ``u.cursor`` seam.
        cursor = conn.cursor()
        _pc_repo.upsert(
            key,
            _json.dumps(value),
            None,
            description_provided=False,
            actor="sysadmin",
            connection=cursor,
        )
        conn.commit()
    finally:
        conn.close()


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
    conn = get_db_connection()
    try:
        for agent_id in agent_ids:
            seed_agent_row(
                conn,
                agent_id,
                token=f"__test_seed_{agent_id}",
                role=None,
                or_ignore=True,
            )
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

        Wave 6 PR 6: the harness mints a typed :class:`Principal` per
        session (operator-tier for the admin / admin-caller, worker
        for everyone else) and stamps it on
        :data:`tools.registry.request_principal` — the same ContextVar
        the AuthHeaderMiddleware sets in production. The MCP framework
        handler reads it back and threads it through the dispatcher;
        the legacy ``operator_session_active`` ContextVar plumbing is
        gone.
        """
        from agent_mcp.tools.registry import (
            request_auth_token,
            request_principal,
        )

        principal = self._principal()

        handler = self._admin._call_tool_handler()
        req = mcp_types.CallToolRequest(
            method="tools/call",
            params=mcp_types.CallToolRequestParams(
                name=tool_name, arguments=arguments
            ),
        )
        cv_token = request_auth_token.set(self.token)
        cv_principal = request_principal.set(principal)
        try:
            server_result = await handler(req)
        finally:
            request_principal.reset(cv_principal)
            request_auth_token.reset(cv_token)

        inner = (
            server_result.root
            if hasattr(server_result, "root")
            else server_result
        )
        # Stash isError for assert_tool_succeeds.
        self._last_is_error = bool(getattr(inner, "isError", False))
        return list(getattr(inner, "content", None) or [])

    def _principal(self):
        """Build the :class:`Principal` for this session.

        Admin-caller sessions surface as an ``agent_bearer`` Principal
        with ``agent_id="admin"`` AND ``sysadmin=True`` so they
        simultaneously:

          * satisfy ``@requires("any")`` (agent_bearer kind),
          * satisfy ``@requires_role("operator")`` /
            ``@requires_role("manager")`` (sysadmin override),
          * supply ``agent_id="admin"`` for audit attribution (matches
            the pre-Wave-6 bridge contract where tests expect
            ``created_by="admin"``).

        Plain worker sessions surface as plain ``agent_bearer``. The
        harness stamps this on :data:`request_principal` before
        driving the MCP framework handler so the wire path sees the
        same identity a real HTTP request would.
        """
        if self.is_admin_caller:
            return make_principal(
                kind="agent_bearer",
                user_id=_HARNESS_OPERATOR_ID,
                agent_id=self.agent_id,
                sysadmin=True,
                project_name="harness",
                project_role="operator",
                agent_role="manager",
                source_token=self.token,
            )
        # Resolve the worker's actual agent_role from the in-memory
        # cache (tests like ``test_assign_task_admits_manager_targeting_other``
        # promote a worker to manager via direct DB mutation; the
        # Principal must reflect that or downstream role checks
        # silently treat the agent as worker-only).
        from agent_mcp.core import globals as _g
        row = _g.active_agents.get(self.token) or {}
        cached_role = row.get("agent_role")
        if cached_role not in ("worker", "manager"):
            from agent_mcp.repositories import agent_repo as _agent_repo
            db_row = _agent_repo.get_by_token(self.token)
            if isinstance(db_row, dict):
                cached_role = db_row.get("agent_role")
        normalized_role = (
            cached_role if cached_role in ("worker", "manager") else None
        )
        return make_principal(
            kind="agent_bearer",
            agent_id=self.agent_id,
            agent_role=normalized_role,
            source_token=self.token,
        )

    async def list_tools(self) -> List[mcp_types.Tool]:
        """`tools/list` as this bearer sees it (admin or worker filter
        per PR #55).

        Wave 6 PR 6: stamps the typed Principal on
        :data:`request_principal` so the list-tools handler's
        visibility filter reads the same role surface a real HTTP
        request would.
        """
        from agent_mcp.tools.registry import (
            request_auth_token,
            request_principal,
        )

        principal = self._principal()
        handler = self._admin._list_tools_handler()
        req = mcp_types.ListToolsRequest(method="tools/list")
        cv_token = request_auth_token.set(self.token)
        cv_principal = request_principal.set(principal)
        try:
            result = await handler(req)
        finally:
            request_principal.reset(cv_principal)
            request_auth_token.reset(cv_token)
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
            seed_agent_row(
                conn,
                _HARNESS_ADMIN_AGENT_ID,
                token=token,
                role="manager",
                created_at=now,
            )
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
        self,
        operator_id: str = _HARNESS_OPERATOR_ID,
        role: str = "operator",
    ) -> dict[str, str]:
        """Return a one-shot signed forwarding header for ``operator_id``.

        Wraps ``agent_mcp.app.forwarding_header.sign`` against the
        per-test HMAC key the harness stamped on
        ``g.forwarding_hmac_key`` at startup. Tests that need to
        authenticate against backend REST routes via the router
        path (cookie → signed header) use this helper to build the
        header on each call.

        SEC-1: ``role`` is the signed per-project role (defaults to
        ``operator`` so existing callers keep operator-tier access;
        pass ``role="viewer"`` to exercise the viewer path over the
        wire).
        """
        from agent_mcp.app import forwarding_header as _fh

        return {
            _fh.HEADER_NAME: _fh.sign(
                operator_id, role, self._forwarding_hmac_key, ttl_sec=30
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

        Wave 9 PR 2: the cache dict carries ``agent_role: "worker"``
        explicitly so :func:`agent_mcp.core.capabilities.resolve_capabilities`
        gets the worker bundle (``mcp.connect``, ``tasks.view``,
        ``tasks.create``, ``coordination.assist`` …). Pre-Wave-9 the
        cache omitted the field — the DB default of ``"worker"``
        backfilled on cache miss, but ``WorkerSession._principal()``
        reads cache-first and resolved to ``agent_role=None``. The
        legacy ``@requires("any")`` decorator admitted any
        agent_bearer regardless of role, so the under-specified cache
        was invisible. Cap migration in PR 2 surfaced it because
        ``has_capability("tasks.create")`` checks the cap-set the
        role-bundle resolves to.
        """
        from agent_mcp.core import globals as g
        from agent_mcp.db.connection import get_db_connection

        worker_token = secrets.token_hex(16)
        now = _dt.datetime.now().isoformat()

        conn = get_db_connection()
        try:
            seed_agent_row(
                conn,
                agent_id,
                token=worker_token,
                role="worker",
                created_at=now,
            )
        finally:
            conn.close()

        g.active_agents[worker_token] = {
            "agent_id": agent_id,
            "status": "active",
            "created_at": now,
            "capabilities": [],
            "agent_role": "worker",
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


def make_principal(
    *,
    kind: "PrincipalKind",
    user_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    sysadmin: bool = False,
    project_name: Optional[str] = None,
    project_role: Optional[str] = None,
    agent_role: "Optional[AgentRole]" = None,
    can_wake_loop: bool = False,
    source_token: Optional[str] = None,
    groups: Optional[frozenset] = frozenset(),
    capabilities: Optional[frozenset] = None,
) -> "Principal":
    """Construct a test :class:`Principal` with HONEST capabilities.

    arch-r5 #1: replaces the ~30 test-local ``_worker_principal`` /
    ``_operator_principal`` / bare ``Principal(...)`` helpers that used
    to rely on the now-deleted ``__post_init__`` back-fill bridge. That
    bridge could not accept a ``groups=`` argument, so any test
    constructing a group-privileged identity via a bare ``Principal(...)``
    silently got a SMALLER cap set than
    :func:`agent_mcp.core.principal_builder.build_operator_principal`
    mints for the same identity in production — a bare-Principal test
    would pass with a plausible-but-wrong cap set.

    This factory closes that gap by funnelling through the SAME
    resolver the production builders call
    (:func:`agent_mcp.core.capabilities.resolve_capabilities`) unless
    the caller supplies an explicit ``capabilities=`` override (for
    tests that deliberately want a specific, possibly-nonsensical cap
    set to isolate one gate). ``groups`` defaults to an empty frozenset
    (not ``None``) so tests are deterministic by default — ``None``
    would make ``resolve_capabilities`` self-resolve transitive groups
    via ``router.db``, which isn't available in most test processes.

    Usage::

        from tests.harness import make_principal
        worker = make_principal(kind="agent_bearer", agent_id="w1", agent_role="worker")
        op = make_principal(kind="operator_session", user_id="alice", project_role="operator")
        sysadmin = make_principal(kind="operator_session", user_id="root", sysadmin=True)

    Pass ``capabilities=frozenset({...})`` to override resolution
    entirely — the same escape hatch :func:`with_capabilities` (below)
    provides pre-packaged for the common "operator carrying exactly
    these caps" shape.
    """
    from agent_mcp.core.capabilities import resolve_capabilities
    from agent_mcp.core.principal import Principal

    caps = (
        capabilities
        if capabilities is not None
        else resolve_capabilities(
            user_id=user_id,
            agent_id=agent_id,
            sysadmin=sysadmin,
            agent_role=agent_role,
            project_role=project_role,
            kind=kind,
            groups=groups,
        )
    )
    return Principal(
        kind=kind,
        user_id=user_id,
        agent_id=agent_id,
        sysadmin=sysadmin,
        project_name=project_name,
        project_role=project_role,
        agent_role=agent_role,
        can_wake_loop=can_wake_loop,
        source_token=source_token,
        capabilities=caps,
    )


def with_capabilities(*caps: str):
    """Construct a test :class:`Principal` carrying exactly ``caps``.

    Wave 9 PR 0 — convenience helper for tests that want to assert on
    capability gates (:meth:`Principal.has_capability` /
    ``@requires_capability``) without setting up a full identity +
    middleware resolution chain. The returned Principal is
    ``operator_session`` shaped with ``project_role="operator"`` so
    the non-``system.*`` cap gate's project-membership requirement
    admits; ``capabilities`` is the exact frozenset passed in — a thin
    wrapper over :func:`make_principal`'s ``capabilities=`` override.

    Pass :data:`agent_mcp.core.capabilities.SYSADMIN_WILDCARD` to
    model a sysadmin (``has_capability`` short-circuits on the
    wildcard).

    Usage::

        from tests.harness import with_capabilities
        p = with_capabilities("tasks.assign", "memories.update")
        assert p.has_capability("tasks.assign")
        assert not p.has_capability("system.users.manage")

    Returns the Principal directly (not a context manager) — for
    ContextVar-stamping, wrap in :func:`with_principal`.
    """
    return make_principal(
        kind="operator_session",
        user_id="harness-operator",
        project_name="harness",
        project_role="operator",
        capabilities=frozenset(caps),
    )


@contextlib.contextmanager
def with_principal(principal):
    """Stamp a :class:`agent_mcp.core.principal.Principal` on the
    request ContextVars for the duration of the ``with`` block.

    Wave 6 PR 6: the helper stamps :data:`request_principal` (the
    canonical carrier the dispatcher / MCP handler reads) and
    :data:`request_auth_token` (for the Q6e bearer-injection
    fallback). The legacy operator-session ContextVars are gone, so
    in-process tool calls that go through the dispatcher see the
    Principal directly.

    Usage::

        from tests.harness import make_principal, with_principal

        p = make_principal(
            kind="operator_session",
            user_id="alice",
            project_name="proj-a",
            project_role="operator",
        )
        with with_principal(p):
            ... # in-process tool calls see p as the calling Principal

    Resets in LIFO order on block exit. Safe to nest; each nested
    use returns a fresh handle that resets its own scope.
    """
    from agent_mcp.tools.registry import (
        request_principal,
        request_auth_token,
    )

    cv_principal = request_principal.set(principal)
    cv_token = None
    if principal.source_token:
        cv_token = request_auth_token.set(principal.source_token)
    try:
        yield principal
    finally:
        if cv_token is not None:
            request_auth_token.reset(cv_token)
        request_principal.reset(cv_principal)


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
        # `reset_and_snapshot_globals()` runs the reset immediately and
        # returns the restore closure; `stack.callback` schedules that
        # closure to run at `stack.close()` — same shape ExitStack
        # already expects.
        stack.callback(reset_and_snapshot_globals())
        install_mock_ollama(stack.callback)

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
        #
        # Wave 6 PR 6: per-session operator-ContextVar stamping is
        # gone — the harness builds a Principal per :meth:`call` /
        # :meth:`list_tools` via :meth:`WorkerSession._principal` and
        # stamps :data:`request_principal` for the duration of that
        # call. Tests that need to assert against a specific
        # Principal in in-process tool calls can wrap with
        # :func:`with_principal`.
        from agent_mcp.core import globals as g

        g.forwarding_hmac_key = os.urandom(32)

        admin_token = _seed_harness_admin_agent()

        admin = AdminClient(
            admin_token=admin_token,
            test_client=test_client,
            forwarding_hmac_key=g.forwarding_hmac_key,
        )
        yield admin
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
