"""Shared test fixtures for in-process integration testing.

The goal is to make integration tests cheap to write: spin up agent-mcp
as an in-process Starlette app, hit it with httpx via Starlette's
TestClient (which handles lifespan startup/shutdown), assert behavior.

No systemd, no real Ollama, no network. Each test gets a fresh tmpdir
SQLite DB.

arch-r6 #2: this module is the single owner of the test-isolation
contract (`reset_and_snapshot_globals`, `install_mock_ollama`,
`seed_agent_row`) — see each function's docstring for why. Before this
refactor, `tests/harness.py` carried standalone byte-for-byte copies of
the globals-reset and mock-ollama logic (labelled "mirror of
conftest.py" in its own comments) plus three more copies of the
agent-row seed SQL; a change to any one invariant (e.g. a new
NOT-NULL `agents` column, or a new module-level global needing reset)
had to be applied in every copy independently or tests would leak
state / break in ways that were hard to attribute to a specific
fixture vs. the harness. `tests/harness.py` now imports these seams
instead of re-implementing them.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest


# Module-level isolation: keep tests from accidentally hitting real APIs
# or reading the user's home OPENAI_API_KEY.
@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Empty key → embedding_client()/completion_client() resolve to the
    # Ollama provider (no OpenAI network call goes out).
    monkeypatch.setenv("OPENAI_API_KEY", "")
    # Don't load whatever .env happens to be in the cwd.
    monkeypatch.setenv("DOTENV_PATH", "/dev/null")
    # Belt and suspenders against stray dashboard ports getting probed.
    monkeypatch.delenv("MCP_PROJECT_DIR", raising=False)
    # create_agent spins up a real tmux session and sleeps 1s between
    # ~6 setup commands (6s/test) in production. Tests don't need the
    # settle delay — zero it so create_agent tests aren't dominated by
    # blocking sleeps.
    monkeypatch.setenv("AGENT_MCP_AGENT_SETUP_DELAY", "0")
    # Router-DB isolation FLOOR. ``migrations_runner.get_router_db_path()``
    # falls back to the PRODUCTION default ``/var/lib/agent-mcp/router.db``
    # when ``AGENT_MCP_ROUTER_DB`` is unset — a system path that is shared
    # across every pytest-xdist worker (and unwritable on most machines).
    # Any test that touches router code without setting its own DB would
    # either crash on that path or, worse, share one file across workers.
    # Guarantee a per-test temp floor so no test can ever fall through to
    # the system default. Router tests that need a specific DB still
    # override this via their own ``monkeypatch.setenv`` (last write wins;
    # this autouse fixture runs first). Set only when absent so an
    # already-configured (e.g. session-level) override is respected.
    if not os.environ.get("AGENT_MCP_ROUTER_DB"):
        monkeypatch.setenv(
            "AGENT_MCP_ROUTER_DB", str(tmp_path / "router-floor.db")
        )
    # R12-F3: embedding_client() now caches one client per resolved
    # (provider, model, dimension, base_url, api_key) tuple for the
    # life of the process (fixing an unbounded connection leak — see
    # embedding_service.py). That's correct for a real server process,
    # but a cache hit could hand a later test an already-constructed
    # openai/httpx client built BEFORE that test's own `mock_ollama` /
    # `mcp_session` transport patch was installed — httpx.Client.__init__
    # is only patched for NEWLY constructed instances, so an
    # already-built client would silently keep talking through a stale
    # prior test's mock transport instead of the current test's. Reset
    # the cache every test so each test's first embedding_client() call
    # builds (and binds) its SDK client while ITS OWN mocks are active.
    from agent_mcp.external.embedding_service import reset_embedding_client_cache

    reset_embedding_client_cache()

    # R13-F3: completion_client() now caches one client per resolved
    # (provider, model, base_url, api_key) tuple, mirroring R12-F3's
    # embedding_client() cache above — same stale-mock-binding hazard,
    # same per-test reset.
    from agent_mcp.external.completion_service import reset_completion_client_cache

    reset_completion_client_cache()


def reset_and_snapshot_globals() -> Callable[[], None]:
    """Reset `agent_mcp.core.globals` state; return a restore closure.

    Single owner of "what mutable state must be isolated between
    tests" — the sole invariant every caller needs, and the one that
    used to be duplicated between the ``reset_globals`` fixture below
    and ``tests.harness.mcp_session`` (see module docstring). Both call
    this function; the fixture restores via ``yield``/teardown, the
    harness registers the returned closure on its ``ExitStack``.

    agent-mcp uses module-level singletons (in-memory task/agent
    caches, etc). Anything that builds its own app instance must reset
    these or state leaks across tests in surprising ways.
    """
    from agent_mcp.core import globals as g
    from agent_mcp.db import engine as _engine

    # Drop SQLAlchemy engines bound to a previous test's tmp DB
    # path — each test gets its own project_dir + DB.
    _engine.reset_engine_cache()
    # wait_for_events Phase 2: drop signals bound to a prior test's
    # event loop. asyncio.Event instances cannot be awaited across
    # loops; signal_for() lazily recreates as needed.
    g.agent_event_signals.clear()
    # PR-2 event-coord: locks and queues are also per-test (locks bound
    # to event loop; queues are transient by design). PR-B / v5.0.24
    # added the per-waiter queue registry; clear it for the same
    # reason — asyncio.Queue is bound to its loop.
    g.agent_event_locks.clear()
    g.agent_event_queues.clear()
    g.agent_event_waiters.clear()
    # Lifespan startup-complete sentinel: every test starts with a
    # fresh, cleared Event so we can detect a regression where
    # application_startup forgets to set it.
    g.reset_startup_complete_event()
    # Agent self-service profiles (PR3): the profile-review greet flag is
    # a module-global set in session_registry. Clear it between tests so a
    # greeted agent_id can't leak across tests and suppress a greet another
    # test expects.
    from agent_mcp.core import session_registry as _sr
    _sr._profile_greeted_agents.clear()

    # R17-F2: offset-pagination anchor caches are process-lifetime
    # singletons (module-level in task_tools.py; class-level on
    # AgentRepository/MessageRepository) by design — that's what lets
    # them survive across the fresh per-call objects that hold a
    # reference to them. That same persistence means a cache entry
    # anchored by one test's fixture data (task/agent/message ids tied
    # to a tmp DB that's about to be torn down) would otherwise leak
    # into the next test that happens to build an identical filter/sort
    # cache key. Clear all three on both sides of a test.
    from agent_mcp.repositories.agent_repository import AgentRepository
    from agent_mcp.repositories.message_repository import MessageRepository
    from agent_mcp.tools.task_tools import _VIEW_TASKS_PAGINATION_CACHE

    _VIEW_TASKS_PAGINATION_CACHE.clear()
    AgentRepository._pagination_cache.clear()
    MessageRepository._pagination_cache.clear()

    snapshot = {
        "connections": dict(g.connections),
        "active_agents": dict(g.active_agents),
        # retire-system-token Wave 3: ``g.admin_token`` is deleted as a
        # declared global. The ``client`` fixture below (and the
        # harness's ``AdminClient``) still set it dynamically as an
        # attribute; capture defensively so the snapshot survives when
        # no caller has set it.
        "admin_token": getattr(g, "admin_token", None),
        "tasks": dict(g.tasks),
        "file_map": dict(g.file_map),
        "agent_working_dirs": dict(g.agent_working_dirs),
        "audit_log": list(g.audit_log),
        "global_vss_load_tested": g.global_vss_load_tested,
        "global_vss_load_successful": g.global_vss_load_successful,
    }

    def _restore() -> None:
        # Dicts/lists are mutated in place, so clear then update.
        g.connections.clear()
        g.connections.update(snapshot["connections"])
        g.active_agents.clear()
        g.active_agents.update(snapshot["active_agents"])
        # retire-system-token Wave 3: only restore admin_token if a
        # prior caller dynamically set it; otherwise leave the attr
        # absent so reads via ``getattr(g, "admin_token", ...)``
        # behave consistently across tests.
        if snapshot["admin_token"] is not None:
            g.admin_token = snapshot["admin_token"]
        elif hasattr(g, "admin_token"):
            delattr(g, "admin_token")
        g.tasks.clear()
        g.tasks.update(snapshot["tasks"])
        g.file_map.clear()
        g.file_map.update(snapshot["file_map"])
        g.agent_working_dirs.clear()
        g.agent_working_dirs.update(snapshot["agent_working_dirs"])
        g.audit_log.clear()
        g.audit_log.extend(snapshot["audit_log"])
        g.global_vss_load_tested = snapshot["global_vss_load_tested"]
        g.global_vss_load_successful = snapshot["global_vss_load_successful"]
        _engine.reset_engine_cache()
        g.agent_event_signals.clear()
        g.agent_event_locks.clear()
        g.agent_event_queues.clear()
        g.agent_event_waiters.clear()
        g.reset_startup_complete_event()
        _VIEW_TASKS_PAGINATION_CACHE.clear()
        AgentRepository._pagination_cache.clear()
        MessageRepository._pagination_cache.clear()

    return _restore


@pytest.fixture
def reset_globals() -> Iterator[None]:
    """Reset agent_mcp.core.globals state between tests.

    Thin pytest wrapper around :func:`reset_and_snapshot_globals` — see
    that function's docstring for what it resets and why.
    """
    restore = reset_and_snapshot_globals()
    yield
    restore()


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    """A fresh workspace directory for one test.

    agent-mcp will create `<project_dir>/.agent/mcp_state.db` inside it
    during application startup.
    """
    workspace = tmp_path / "project"
    workspace.mkdir()
    return workspace


@pytest.fixture
def app(project_dir: Path, reset_globals: None):
    """A Starlette app instance pointed at a fresh project dir.

    Use with TestClient (`client` fixture) for HTTP testing. The
    TestClient context manager runs the lifespan, which initializes the
    DB schema, generates the admin token, and runs the rest of
    `application_startup`.
    """
    from agent_mcp.app.main_app import create_app

    return create_app(project_dir=str(project_dir))


def seed_agent_row(
    conn: Any,
    agent_id: str,
    *,
    token: str | None = None,
    role: str | None = "worker",
    status: str = "active",
    working_directory: str = "/tmp",
    color: str = "#888",
    or_ignore: bool = False,
    created_at: str | None = None,
) -> str:
    """INSERT a minimal `agents` row directly via raw SQL. Returns the token.

    Single owner of the "what does a synthetic test agent row look
    like" shape — before this refactor, four call sites (this
    module's ``client`` fixture, and three sites in
    ``tests/harness.py``) each re-listed the same 8-9 columns with the
    literals ``"[]"``, ``"active"``, ``"/tmp"``, ``"#888"``.

    Deliberately bypasses ``agent_repo.create()``:

    * that repo method rejects ``agent_id`` values matching
      ``_RESERVED_AGENT_ID_PREFIXES`` (``"admin"`` is reserved), but
      several call sites need to seed exactly that id.
    * it always raises on a duplicate row; some call sites need
      idempotent re-seeding (``or_ignore=True``) across repeated calls
      in the same test process.
    * it validates ``agent_id`` against a slug regex; some call sites
      (e.g. ``tests/test_rest_messages_endpoints.py``'s
      ``"[deleted-old-worker-1]"``) deliberately seed ids that would
      fail that regex.

    ``token`` defaults to a fresh ``secrets.token_hex(16)`` when not
    supplied. ``role=None`` omits the ``agent_role`` column entirely
    (relies on the schema default) — used by bare FK-satisfying rows
    that don't care about auth. ``created_at`` lets a caller thread the
    same timestamp it also uses to populate an in-memory
    ``g.active_agents`` cache entry, so the DB row and the cache entry
    agree.

    Caller owns the connection: this commits but does not close it.
    """
    import datetime as _dt
    import secrets as _secrets

    tok = token if token is not None else _secrets.token_hex(16)
    now = created_at or _dt.datetime.now().isoformat()
    verb = "INSERT OR IGNORE" if or_ignore else "INSERT"
    cursor = conn.cursor()
    if role is not None:
        cursor.execute(
            f"{verb} INTO agents (token, agent_id, "
            "created_at, status, working_directory, color, updated_at, "
            "agent_role) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                tok,
                agent_id,
                now,
                status,
                working_directory,
                color,
                now,
                role,
            ),
        )
    else:
        cursor.execute(
            f"{verb} INTO agents (token, agent_id, "
            "created_at, status, working_directory, color, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (tok, agent_id, now, status, working_directory, color, now),
        )
    conn.commit()
    return tok


def existing_root_task_id() -> str | None:
    """Return the current root task_id (``parent_task IS NULL``), or None.

    R15-BL-1: the single-root-task invariant is now enforced by a partial
    UNIQUE index (``idx_tasks_single_root``), so a test that seeds several
    tasks can no longer make every row a root. Fixtures chain seeded tasks
    under ONE root: the first parentless seed becomes the sole root, and
    every subsequent seed passes this value as its ``parent_task`` (a
    child, which the index permits). Opens its own short-lived connection,
    so it only sees roots that were already COMMITTED — fine for the
    commit-per-seed helpers; batch-insert loops must track the root
    locally instead.

    Use this in COUNT/bound-sensitive fixtures (no extra row is added).
    Where a test asserts on parent-context display or hierarchy, prefer
    :func:`ensure_seed_root` so the asserted ids are never a parent.
    """
    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT task_id FROM tasks WHERE parent_task IS NULL LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        # sqlite3.Row supports index access; plain tuples do too.
        return row[0]
    finally:
        conn.close()


_SEED_ROOT_ID = "task_seed_root_0000"


def ensure_seed_root() -> str:
    """Ensure a DEDICATED hidden root task exists; return its task_id.

    R15-BL-1: for fixtures where the seeded tasks are themselves under
    assertion (presence/absence, or parent-context display), chaining
    under the FIRST seed would leak that seed's id into hierarchy output.
    Instead every seeded task parents under this dedicated root, whose id
    (:data:`_SEED_ROOT_ID`) no test asserts on. Idempotent: created once
    per DB, reused thereafter. ``assigned_to`` is NULL (no agents FK) and
    the status is terminal (``completed``) so it stays out of most
    active-task filters. Adds ONE row — do NOT use in count/bound tests;
    use :func:`existing_root_task_id` there.
    """
    import datetime as _dt

    from agent_mcp.db.connection import get_db_connection

    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT task_id FROM tasks WHERE task_id = ?",
            (_SEED_ROOT_ID,),
        ).fetchone()
        if row is not None:
            return _SEED_ROOT_ID
        now = _dt.datetime.now().isoformat()
        conn.execute(
            "INSERT INTO tasks (task_id, title, description, assigned_to, "
            "created_by, status, priority, created_at, updated_at, "
            "parent_task, child_tasks, depends_on_tasks, notes) "
            "VALUES (?, 'seed root', 'hidden root for seeded tasks', NULL, "
            "'admin', 'completed', 'medium', ?, ?, NULL, '[]', '[]', '[]')",
            (_SEED_ROOT_ID, now, now),
        )
        conn.commit()
        return _SEED_ROOT_ID
    finally:
        conn.close()


@pytest.fixture
def client(app):
    """An httpx TestClient against the in-process app.

    Using it as a context manager triggers lifespan startup/shutdown.
    Routes are reachable as `client.get("/api/tokens")` etc.

    retire-system-token Wave 1: pre-Wave-1, tests using this fixture
    passed ``g.admin_token`` (the system bearer) as ``body['token']``
    on REST mutation routes, and the dep admitted via the god-key
    check. That check is gone; we seed a real per-agent manager-role
    row (post-lifespan) so the body-token path through
    ``_bearer_is_operator_tier`` admits via
    ``verify_token(token, "manager")`` — same wire shape, real
    per-principal credential.

    retire-system-token Wave 3: ``g.admin_token`` is deleted as a
    declared global in ``agent_mcp.core.state``. This fixture still
    assigns ``g.admin_token = token`` dynamically (Python attribute
    assignment works on the state module without prior declaration) so
    the many tests that read ``g.admin_token`` as their operator-tier
    bearer continue to function without per-callsite edits. The
    snapshot/restore plumbing in ``reset_and_snapshot_globals`` reads
    it defensively via ``getattr``.
    """
    import datetime as _dt
    import secrets as _secrets

    from starlette.testclient import TestClient

    with TestClient(app) as test_client:
        # Seed a manager-role agent row that the dep's
        # ``_bearer_is_operator_tier`` admits, and re-point
        # ``g.admin_token`` at that row's token so existing tests
        # (which dereference ``g.admin_token`` as their operator-tier
        # bearer / body-token credential) keep working without per-
        # callsite edits.
        from agent_mcp.core import globals as g
        from agent_mcp.db.connection import get_db_connection

        token = _secrets.token_hex(16)
        now = _dt.datetime.now().isoformat()
        conn = get_db_connection()
        try:
            seed_agent_row(
                conn,
                "admin",
                token=token,
                role="manager",
                or_ignore=True,
                created_at=now,
            )
        finally:
            conn.close()
        g.active_agents[token] = {
            "agent_id": "admin",
            "status": "active",
            "created_at": now,
            "capabilities": [],
            "agent_role": "manager",
        }
        g.admin_token = token
        yield test_client


def install_mock_ollama(
    register: Callable[[Callable[[], None]], None],
) -> None:
    """Install the in-process httpx mock transport shared by the
    `mock_ollama` fixture (below) and `tests.harness.mcp_session`.

    Replaces the OpenAI-shaped embeddings endpoint with a deterministic
    fake so RAG/indexing test flows don't reach a real Ollama instance.
    Returns deterministic 1024-dim zero-vectors (matching the
    `qwen3-embedding:0.6b` dimension used by the deployment).

    ``register`` takes a single zero-arg cleanup callable and is
    responsible for scheduling it to run at teardown. Both call sites'
    native teardown mechanisms have exactly that shape, so one
    implementation serves both: pytest's
    ``FixtureRequest.addfinalizer`` (LIFO at fixture-scope teardown) or
    ``contextlib.ExitStack.callback`` (LIFO at ``stack.close()``).
    """
    import httpx

    DIM = 1024

    def _handler(request: httpx.Request) -> httpx.Response:
        # /v1/embeddings is the OpenAI shape Ollama serves
        if request.url.path.endswith("/embeddings"):
            body = request.read()
            # naive: count inputs by occurrences of "input" key in body
            # OpenAI spec: {"input": str | list[str], "model": "..."}
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
                        {"object": "embedding", "embedding": [0.0] * DIM, "index": i}
                        for i in range(len(inputs))
                    ],
                    "model": data.get("model", "mock-embed"),
                    "usage": {"prompt_tokens": 0, "total_tokens": 0},
                },
            )
        return httpx.Response(404, json={"error": "not found"})

    transport = httpx.MockTransport(_handler)
    # Patch httpx.Client and httpx.AsyncClient defaults; agent-mcp's
    # openai client uses httpx under the hood.
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

    register(_restore)


@pytest.fixture
def mock_ollama(request: pytest.FixtureRequest) -> None:
    """Replace the OpenAI-shaped embeddings endpoint with an in-process fake.

    Tests that exercise RAG/indexing flows opt in by depending on this
    fixture. See :func:`install_mock_ollama` for the shared
    implementation this delegates to.

    Not strictly needed for Phase 1's smoke test, but provided so Phase
    3+ tests don't each invent their own mock.
    """
    install_mock_ollama(request.addfinalizer)
