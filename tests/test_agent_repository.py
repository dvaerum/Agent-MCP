"""Contract tests for the class-based ``AgentRepository`` (PR follow-up to #146).

PR #137 introduced module-of-functions repositories under
``agent_mcp.core.repositories``. PR #146 promoted ``TaskRepository`` to
a real class on ``agent_mcp.repositories``. This file pins the same
contract for the Agent concept.

What this test file pins:

* The singleton exists at ``agent_mcp.repositories.agent_repo`` after
  application lifespan startup, and points at an ``AgentRepository``
  instance.
* Every method preserves the wire-equivalent semantics of the legacy
  module-of-functions surface (return shapes, cache invariants —
  ``state.active_agents`` keyed by token + ``state.agent_working_dirs``
  keyed by agent_id — EventBus publishing) so call-site migrations are
  mechanical.
* ``terminate`` is net-new on the class form (parallel to
  ``TaskRepository.delete``): it centralises the "status →
  'terminated', evict both caches, publish ``agent.terminated``"
  ritual that admin_tools currently spells out by hand in a
  multi-table transaction.

These tests fail on ``main`` because:

* ``agent_mcp.repositories.agent_repository`` (the class module) does
  not yet exist.
* ``agent_mcp.repositories.agent_repo`` (the lifespan singleton) is
  not exposed from the top-level repositories package.
* ``AgentRepository.terminate`` is net-new.
"""

from __future__ import annotations

import datetime
import sys

import pytest
from starlette.testclient import TestClient

from agent_mcp.app.main_app import create_app

# --- Helpers -------------------------------------------------------------


def _make_client(project_dir):
    """Build the in-process app + TestClient.

    Using a fresh client per test means each call runs through the
    full lifespan startup (which is what wires the singleton).
    """
    app = create_app(project_dir=str(project_dir))
    return TestClient(app)


def _seed_agent(
    agent_id: str,
    *,
    token: str | None = None,
    status: str = "active",
    working_directory: str = "/tmp/seed",
    color: str = "#abcdef",
):
    """Insert an agent via the ORM path, bypassing the repo.

    The repo class under test must observe this row when callers ask
    for it — proves the read methods fall through to the DB rather
    than returning only what passed through ``create``.
    """
    from agent_mcp.db.engine import get_session
    from agent_mcp.db.models import Agent

    now = datetime.datetime.now().isoformat()
    with get_session() as session:
        session.add(
            Agent(
                token=token or f"tok-{agent_id}",
                agent_id=agent_id,
                created_at=now,
                status=status,
                current_task=None,
                working_directory=working_directory,
                color=color,
                terminated_at=None,
                updated_at=now,
                aoe_session_id=None,
            )
        )
        session.commit()


class _CapturingBus:
    """Drop-in replacement for ``agent_mcp.core.event_bus``.

    Captures every ``(agent_id, event_type, payload)`` tuple so a
    test can assert exactly one publish per write.
    """

    def __init__(self):
        self.events: list[tuple[str, str, dict]] = []

    def notify(self, agent_id, event_type, payload):
        self.events.append((agent_id, event_type, payload or {}))


# --- Singleton + lifespan wiring ----------------------------------------


def test_agent_repo_singleton_is_agentrepository_instance(
    project_dir, reset_globals,
):
    """``agent_mcp.repositories.agent_repo`` resolves to a class instance.

    The plan locks "module singletons, lifespan-owned" — so the
    attribute access shape is ``from agent_mcp.repositories import
    agent_repo`` and the value is an instance, not a module.
    """
    with _make_client(project_dir):
        from agent_mcp.repositories import agent_repo
        from agent_mcp.repositories.agent_repository import AgentRepository

        assert isinstance(agent_repo, AgentRepository), (
            "agent_repo must be an AgentRepository instance after lifespan "
            "startup so call sites can rely on the class-based contract"
        )


# --- Read interface ------------------------------------------------------


def test_get_by_id_returns_dict_when_present(project_dir, reset_globals):
    """``get_by_id`` returns the same dict shape legacy callers expect."""
    with _make_client(project_dir):
        from agent_mcp.repositories import agent_repo

        _seed_agent("agent-getbyid", token="tok-getbyid", status="active")

        row = agent_repo.get_by_id("agent-getbyid")
        assert row is not None
        assert row["agent_id"] == "agent-getbyid"
        assert row["token"] == "tok-getbyid"


def test_get_by_id_returns_none_when_missing(project_dir, reset_globals):
    with _make_client(project_dir):
        from agent_mcp.repositories import agent_repo
        assert agent_repo.get_by_id("does-not-exist") is None


def test_get_by_token_returns_dict_when_present(project_dir, reset_globals):
    """``get_by_token`` is the auth hot-path lookup. Cache-first read."""
    with _make_client(project_dir):
        from agent_mcp.repositories import agent_repo

        _seed_agent("agent-tok", token="tok-bearer-123", status="active")

        row = agent_repo.get_by_token("tok-bearer-123")
        assert row is not None
        assert row["agent_id"] == "agent-tok"


def test_list_active_excludes_terminated(project_dir, reset_globals):
    with _make_client(project_dir):
        from agent_mcp.repositories import agent_repo

        _seed_agent("alive-1", token="tok-alive-1", status="active")
        _seed_agent("alive-2", token="tok-alive-2", status="created")
        _seed_agent("dead-1", token="tok-dead-1", status="terminated")

        rows = agent_repo.list_active()
        ids = {row["agent_id"] for row in rows}
        assert "alive-1" in ids
        assert "alive-2" in ids
        assert "dead-1" not in ids


def test_list_active_excludes_tombstone(project_dir, reset_globals):
    """Purge-cascade tombstone rows (``status='tombstone'``) are FK
    artefacts, not agents — ``list_active`` must not surface them.

    Mirrors the REST ``WHERE status != 'tombstone'`` filter so MCP
    consumers of ``list_active`` (``/api/all-data`` startup hydration
    et al.) share the REST agent-list contract (BL-R31-3).
    """
    with _make_client(project_dir):
        from agent_mcp.repositories import agent_repo

        _seed_agent("alive-tomb", token="tok-alive-tomb", status="active")
        _seed_agent(
            "[deleted-ghost]",
            token="__tombstone_ghost",
            status="tombstone",
        )

        rows = agent_repo.list_active()
        ids = {row["agent_id"] for row in rows}
        assert "alive-tomb" in ids
        assert "[deleted-ghost]" not in ids, (
            f"tombstone row leaked into list_active(): {ids}"
        )


# --- Query interface (view_agents / MCP) --------------------------------


def test_query_excludes_tombstone_rows_and_count(project_dir, reset_globals):
    """``AgentRepository.query`` backs the MCP ``view_agents`` tool.

    BL-R31-3: it must exclude ``status='tombstone'`` rows AND those
    rows must not inflate ``total_count`` — matching every REST
    agent-list surface (``routers/agents.py`` applies
    ``WHERE status != 'tombstone'`` unconditionally). Before the fix
    the tombstone row leaks into both the page and the count.
    """
    with _make_client(project_dir):
        from agent_mcp.repositories import agent_repo

        _seed_agent("q-alive", token="tok-q-alive", status="active")
        _seed_agent("q-created", token="tok-q-created", status="created")
        _seed_agent(
            "[deleted-q-ghost]",
            token="__tombstone_q-ghost",
            status="tombstone",
        )

        rows, total = agent_repo.query({})
        ids = {r["agent_id"] for r in rows}
        statuses = {r["status"] for r in rows}

        assert "q-alive" in ids
        assert "q-created" in ids
        assert "[deleted-q-ghost]" not in ids, (
            f"tombstone row leaked into view_agents query: {ids}"
        )
        assert "tombstone" not in statuses, (
            f"any status='tombstone' row in view_agents is a leak: {statuses}"
        )
        # total_count must reflect the tombstone-excluded set (2 rows),
        # not inflate to 3.
        assert total == 2, (
            f"total_count must exclude tombstone rows; expected 2, got {total}"
        )


def _seed_agent_at(agent_id: str, *, token: str, status: str, created_at: str):
    """Like ``_seed_agent`` but with an explicit ``created_at`` so
    sort order across several seeded rows is deterministic."""
    from agent_mcp.db.engine import get_session
    from agent_mcp.db.models import Agent

    with get_session() as session:
        session.add(
            Agent(
                token=token,
                agent_id=agent_id,
                created_at=created_at,
                status=status,
                current_task=None,
                working_directory="/tmp/seed",
                color="#abcdef",
                terminated_at=None,
                updated_at=created_at,
                aoe_session_id=None,
            )
        )
        session.commit()


def test_query_offset_pagination_survives_concurrent_status_change(
    project_dir, reset_globals,
):
    """R17-F2 class-sweep sibling: ``AgentRepository.query`` (backs the
    MCP ``get_agent_tokens`` / dashboard agent-list surfaces) re-filters
    a live table on every call, exactly like the ``view_tasks`` case.
    5 agents, paginate ``limit=2``; between page 1 and page 2 the
    top-ranked agent (pg-a5) terminates and drops out of the
    ``include_terminated=False`` filter — an agent that was in-filter
    the ENTIRE time (pg-a3) must not be silently skipped.
    """
    with _make_client(project_dir):
        from agent_mcp.repositories import agent_repo
        from agent_mcp.db.engine import get_session
        from agent_mcp.db.models import Agent

        base = datetime.datetime(2025, 6, 1)
        for i in range(1, 6):
            _seed_agent_at(
                f"pg-a{i}",
                token=f"tok-pg-a{i}",
                status="active",
                created_at=(base + datetime.timedelta(minutes=i)).isoformat(),
            )

        filters = {
            "agent_id_pattern": "pg-a%",
            "include_terminated": False,
            "limit": 2,
        }
        page1, _ = agent_repo.query({**filters, "offset": 0})
        assert [r["agent_id"] for r in page1] == ["pg-a5", "pg-a4"]

        # Ordinary concurrent activity between the two page requests.
        with get_session() as session:
            session.query(Agent).filter(
                Agent.agent_id == "pg-a5"
            ).update({"status": "terminated"})
            session.commit()

        page2, _ = agent_repo.query({**filters, "offset": 2})

        seen_ids = {r["agent_id"] for r in page1} | {
            r["agent_id"] for r in page2
        }
        assert "pg-a3" in seen_ids, (
            "pg-a3 was active for the entire window and must not be "
            f"silently skipped; page1={page1!r} page2={page2!r}"
        )
        assert [r["agent_id"] for r in page2] == ["pg-a3", "pg-a2"]


def test_query_status_tombstone_filter_returns_empty(project_dir, reset_globals):
    """An explicit ``status='tombstone'`` filter must return nothing —
    tombstone is a DB-internal FK artefact, never an operator-queryable
    status (mirrors ``GET /api/agents?status=tombstone`` → ``[]``)."""
    with _make_client(project_dir):
        from agent_mcp.repositories import agent_repo

        _seed_agent(
            "[deleted-alpha]",
            token="__tombstone_alpha",
            status="tombstone",
        )
        _seed_agent(
            "[deleted-beta]",
            token="__tombstone_beta",
            status="tombstone",
        )

        rows, total = agent_repo.query({"status": "tombstone"})
        assert rows == []
        assert total == 0


# --- Write interface: create --------------------------------------------


def test_create_returns_dict_and_updates_caches_and_publishes(
    project_dir, reset_globals,
):
    """``create`` is the single seam for new agents.

    Contract:
      1. Returns the freshly-created dict (not just bool).
      2. ``state.active_agents`` (keyed by token) carries the row.
      3. ``state.agent_working_dirs`` (keyed by agent_id) carries the
         working directory — they're two views over the same row and
         must stay in sync.
      4. EventBus sees exactly one publish for the new agent.
    """
    bus = _CapturingBus()
    sys.modules["agent_mcp.core.event_bus"] = bus  # type: ignore[assignment]
    try:
        with _make_client(project_dir):
            from agent_mcp.core import state
            from agent_mcp.repositories import agent_repo

            entity = agent_repo.create(
                token="tok-create",
                agent_id="agent-create",
                status="created",
                working_directory="/tmp/agent-create",
                color="#112233",
            )

            assert entity["agent_id"] == "agent-create"
            assert entity["token"] == "tok-create"

            # Both caches reflect the new agent.
            assert "tok-create" in state.active_agents, (
                "create must populate active_agents (single ownership of "
                "cache+DB invariant)"
            )
            assert state.agent_working_dirs.get("agent-create") == (
                "/tmp/agent-create"
            ), (
                "create must populate agent_working_dirs in lockstep with "
                "active_agents (two views over the same row)"
            )

            # Exactly one publish for the create.
            create_events = [
                e for e in bus.events if "agent" in e[1] and "created" in e[1]
            ]
            assert len(create_events) == 1, bus.events
    finally:
        sys.modules.pop("agent_mcp.core.event_bus", None)


# --- Write interface: update_field --------------------------------------


def test_update_field_success_emits_event_and_updates_cache(
    project_dir, reset_globals,
):
    bus = _CapturingBus()
    sys.modules["agent_mcp.core.event_bus"] = bus  # type: ignore[assignment]
    try:
        with _make_client(project_dir):
            from agent_mcp.core import state
            from agent_mcp.repositories import agent_repo

            _seed_agent("agent-upd", token="tok-upd", status="created")
            # Prime the cache.
            agent_repo.get_by_token("tok-upd")
            bus.events.clear()

            result = agent_repo.update_field(
                "agent-upd", "status", "active",
            )

            assert result is not None
            assert result["status"] == "active"

            # Cache reflects new value.
            cached = state.active_agents.get("tok-upd")
            assert cached is not None
            assert cached["status"] == "active"

            # Exactly one event from this update_field call.
            assert len(bus.events) == 1, bus.events
            _addr, event_type, _payload = bus.events[0]
            assert "agent" in event_type
    finally:
        sys.modules.pop("agent_mcp.core.event_bus", None)


def test_update_field_missing_agent_returns_none(project_dir, reset_globals):
    with _make_client(project_dir):
        from agent_mcp.repositories import agent_repo
        result = agent_repo.update_field("nope", "status", "active")
        assert result is None


def test_update_field_invalid_field_returns_none(project_dir, reset_globals):
    """The allowlist rejects unknown fields, returning None.

    Mirrors the legacy ``update_agent_db_field`` allowlist guard — the
    class form is a thin shell around it.
    """
    with _make_client(project_dir):
        from agent_mcp.repositories import agent_repo

        _seed_agent("agent-bad", token="tok-bad", status="active")
        # token / agent_id / created_at are NOT in the mutable allowlist.
        assert agent_repo.update_field("agent-bad", "agent_id", "x") is None


# --- Write interface: terminate -----------------------------------------


def test_terminate_success_evicts_caches_and_publishes(
    project_dir, reset_globals,
):
    """``terminate`` is the class form's centralised teardown path.

    Contract:
      1. Sets status='terminated' + bumps terminated_at in the DB.
      2. Evicts BOTH caches (the two-view invariant).
      3. Publishes exactly one ``agent.terminated`` event.
    """
    bus = _CapturingBus()
    sys.modules["agent_mcp.core.event_bus"] = bus  # type: ignore[assignment]
    try:
        with _make_client(project_dir):
            from agent_mcp.core import state
            from agent_mcp.repositories import agent_repo

            _seed_agent(
                "agent-term", token="tok-term", status="active",
                working_directory="/tmp/term",
            )
            # Prime the caches.
            agent_repo.get_by_token("tok-term")
            assert "tok-term" in state.active_agents
            bus.events.clear()

            ok = agent_repo.terminate("agent-term")
            assert ok is True

            # Both caches evicted.
            assert "tok-term" not in state.active_agents
            assert "agent-term" not in state.agent_working_dirs

            # DB row still exists but status is terminated.
            row = agent_repo.get_by_id("agent-term")
            assert row is not None
            assert row["status"] == "terminated"

            # Exactly one terminate event.
            term_events = [e for e in bus.events if "terminat" in e[1]]
            assert len(term_events) == 1, bus.events
    finally:
        sys.modules.pop("agent_mcp.core.event_bus", None)


def test_terminate_missing_returns_false(project_dir, reset_globals):
    with _make_client(project_dir):
        from agent_mcp.repositories import agent_repo
        assert agent_repo.terminate("missing") is False


# --- Transaction-aware seam on create / terminate (PR #152) -------------


def test_create_with_sqlite_cursor_lands_in_caller_transaction(
    project_dir, reset_globals,
):
    """``create(connection=cursor)`` writes the agent row through the
    caller's cursor so it's atomic with the surrounding agent_actions
    audit-log INSERT (the create_agent_tool_impl pattern)."""
    with _make_client(project_dir):
        from agent_mcp.db.connection import get_db_connection
        from agent_mcp.repositories import agent_repo

        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("BEGIN")
            agent_repo.create(
                token="tok-seam-1",
                agent_id="seam-1",
                working_directory="/tmp/seam-1",
                connection=cursor,
            )
            conn.commit()
        finally:
            conn.close()

        row = agent_repo.get_by_id("seam-1")
        assert row is not None
        assert row["token"] == "tok-seam-1"


def test_create_with_sqlite_cursor_rolls_back_with_outer_transaction(
    project_dir, reset_globals,
):
    """Rollback of the caller's transaction must drop the row.

    Proves the agent INSERT really runs against the caller's
    transaction (not a hidden auto-committed session).
    """
    with _make_client(project_dir):
        from agent_mcp.db.connection import get_db_connection
        from agent_mcp.repositories import agent_repo

        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("BEGIN")
            agent_repo.create(
                token="tok-rollback",
                agent_id="seam-rollback",
                working_directory="/tmp/seam-rollback",
                connection=cursor,
            )
            conn.rollback()
        finally:
            conn.close()

        assert agent_repo.get_by_id("seam-rollback") is None


def test_terminate_with_sqlite_cursor_uses_caller_transaction(
    project_dir, reset_globals,
):
    """``terminate(connection=cursor)`` flips status via the caller's
    cursor. The caller is responsible for cache eviction post-commit
    (mirrors the task_repo.delete contract)."""
    with _make_client(project_dir):
        from agent_mcp.db.connection import get_db_connection
        from agent_mcp.repositories import agent_repo

        agent_repo.create(
            token="tok-term",
            agent_id="seam-term",
            working_directory="/tmp/seam-term",
        )

        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("BEGIN")
            ok = agent_repo.terminate("seam-term", connection=cursor)
            assert ok is True
            conn.commit()
        finally:
            conn.close()

        # Caller evicts cache after commit.
        agent_repo.evict_from_cache("seam-term", token="tok-term")
        row = agent_repo.get_by_id("seam-term")
        assert row is not None
        assert row["status"] == "terminated"


# --- Write interface: agent_id regex validation -------------------------
#
# VM e2e on 2026-06-16 surfaced that `create_agent` accepted garbage
# agent IDs (literal `"InvalidName!@#"` was successfully created). The
# dashboard form already pins the slug pattern but the server enforced
# nothing — a poisoning vector for URL routing (agent_id is a DB key +
# a URL segment). Interior `@` is now allowed (e.g. `worker@host`); see
# `_AGENT_ID_RE` in agent_repository.py.
#
# Locked design (Dennis, 2026-06-16): the validation lives in the
# Repository so every caller (MCP tool, REST, CLI) hits it. The repo
# already owns SQL invariants for this concept (PR 8 / Agent flip);
# this extends that contract to "the repo owns invariants on this
# concept's identity, too."


_VALID_AGENT_IDS = [
    "backend-dev",
    "a",                # single-char branch of the regex
    "agent-1",
    "a-b-c",
    "z9",
    "abc",
    "worker@team",      # '@' allowed in the interior (e.g. worker@host)
    "a@b",              # minimal '@' case
    "backend-dev@pikvm",
    "agent_with_underscore",  # '_' allowed in the interior
    "a_b",                    # minimal '_' case
    "pikvm_mcp_server@nixos-developer-system",  # '_' + '@' + '-' together
]

_INVALID_AGENT_IDS = [
    "InvalidName!@#",   # special chars + uppercase (the original VM finding)
    "1starts-with-digit",
    "-starts-with-dash",
    "ends-with-dash-",
    "has space",
    "",                 # empty
    "A",                # uppercase single
    "UPPERCASE",
    "agent.with.dots",
    "@starts-with-at",  # '@' allowed only in the interior, not at the start
    "ends-with-at@",    # ...nor at the end
    "_starts-with-underscore",  # '_' interior-only, not at the start
    "ends-with-underscore_",    # ...nor at the end
]


@pytest.mark.parametrize("agent_id", _VALID_AGENT_IDS)
def test_create_accepts_valid_agent_id(agent_id, project_dir, reset_globals):
    """``create`` accepts every agent_id the dashboard regex accepts.

    The regex is ``^[a-z][a-z0-9-]*[a-z0-9]$|^[a-z]$`` — matches the
    client-side pattern in the dashboard's Deploy modal exactly. The
    ``|^[a-z]$`` branch covers single-character names.
    """
    with _make_client(project_dir):
        from agent_mcp.repositories import agent_repo

        fresh = agent_repo.create(
            token=f"tok-valid-{agent_id}",
            agent_id=agent_id,
            status="created",
            working_directory=f"/tmp/{agent_id}",
        )
        assert fresh is not None
        assert fresh["agent_id"] == agent_id


@pytest.mark.parametrize("agent_id", _INVALID_AGENT_IDS)
def test_create_rejects_invalid_agent_id(agent_id, project_dir, reset_globals):
    """``create`` raises a ``ValueError`` on any agent_id the regex rejects.

    The Repository is the single owner of this invariant — every
    caller (MCP tool, REST, CLI) hits the same check.
    """
    with _make_client(project_dir):
        from agent_mcp.repositories import agent_repo

        with pytest.raises(ValueError):
            agent_repo.create(
                token=f"tok-invalid-{abs(hash(agent_id))}",
                agent_id=agent_id,
                status="created",
                working_directory="/tmp/invalid",
            )

        # And the row was NOT inserted — the rejection happens BEFORE
        # any DB write, so no partial state is left behind. Cache is
        # disabled for the lookup so we hit the DB authoritatively.
        with agent_repo.disable_cache():
            assert agent_repo.get_by_id(agent_id) is None


# --- _sanitise_field (arch-r5 #3) -----------------------------------------
#
# Before this PR the allowlist + per-field normalisation lived 3x:
# the standalone own-session writer (``update_agent_db_field``), the
# shared-cursor writer (``_update_field_with_cursor``), and a shared-
# session writer (``_update_field_with_session``) that a grep of every
# ``update_field(..., connection=...)`` call site in ``agent_mcp/`` and
# ``tests/`` showed was never exercised by any real caller — every
# site passes a raw ``sqlite3.Cursor``. That dead branch has been
# deleted; ``_sanitise_field`` is now the single place the two
# surviving writers (cursor + standalone session) both call. These
# tests pin its contract directly so the invariant the dead path made
# hard to isolate is covered without spinning up the app harness.

_SANITISE_FIELD_CASES = [
    # (field_name, new_value, expected_ok, expected_value)
    ("status", "active", True, "active"),
    ("current_task", "task_1", True, "task_1"),
    ("working_directory", "/tmp/x", True, "/tmp/x"),
    ("color", "#fff", True, "#fff"),
    ("aoe_session_id", "sess-1", True, "sess-1"),
    ("last_event_seen_at", "2026-01-01T00:00:00", True, "2026-01-01T00:00:00"),
    ("terminated_at", None, True, None),
    ("agent_role", "manager", True, "manager"),
    # auto_event_loop: SQLite has no native bool, coerce truthy/falsy -> 1/0.
    ("auto_event_loop", True, True, 1),
    ("auto_event_loop", False, True, 0),
    ("auto_event_loop", 1, True, 1),
    ("auto_event_loop", 0, True, 0),
    # unknown / off-allowlist fields are rejected outright.
    ("token", "new-secret", False, None),
    ("agent_id", "renamed", False, None),
    ("created_at", "2026-01-01", False, None),
    ("not_a_real_field", "x", False, None),
]


@pytest.mark.parametrize(
    "field_name, new_value, expected_ok, expected_value",
    _SANITISE_FIELD_CASES,
    ids=[c[0] + ("" if c[2] else "-rejected") for c in _SANITISE_FIELD_CASES],
)
def test_sanitise_field(field_name, new_value, expected_ok, expected_value):
    """Table-driven pin of the allowlist + per-field normalisation.

    Doesn't need the app harness — ``_sanitise_field`` is a pure
    function of ``(field_name, new_value)``.
    """
    from agent_mcp.repositories.agent_repository import _sanitise_field

    ok, value = _sanitise_field(field_name, new_value)
    assert ok is expected_ok
    if expected_ok:
        assert value == expected_value
    else:
        assert value is None


def test_sanitise_field_updated_at_none_stamps_now():
    """A ``None`` ``updated_at`` is the one field whose normalized value
    isn't a pure function of the input — it stamps "now". Pinned
    separately from the table so the table stays exact-value comparable.
    """
    from agent_mcp.repositories.agent_repository import _sanitise_field

    ok, value = _sanitise_field("updated_at", None)
    assert ok is True
    # ISO-8601 timestamp, not the literal None that was passed in.
    assert isinstance(value, str)
    datetime.datetime.fromisoformat(value)


def test_sanitise_field_updated_at_explicit_value_passthrough():
    """A caller-supplied (non-``None``) ``updated_at`` passes through
    unchanged — only the ``None`` sentinel triggers the "stamp now" rule.
    """
    from agent_mcp.repositories.agent_repository import _sanitise_field

    ok, value = _sanitise_field("updated_at", "2020-01-01T00:00:00")
    assert ok is True
    assert value == "2020-01-01T00:00:00"


# --- SEC-A: auth-cache re-warm gated on non-terminal status --------------
#
# ``get_by_id`` and ``get_by_token`` gate their cache re-warm on
# ``row.get("status") not in TERMINAL_AGENT_STATUSES`` — caching a
# 'terminated' row would silently reactivate a revoked bearer, since
# ``app.main_app._bearer_is_active`` (the ``/mcp`` auth gate) is
# cache-only. ``advance_event_cursor`` and ``update_field``'s
# own-connection path independently re-read + re-warm the same
# ``state.active_agents`` cache but, before this fix, lacked the same
# gate: an agent mid-``wait_for_events`` long-poll that gets terminated
# out from under it can still resume and call ``advance_event_cursor``
# (via ``_write_last_event_seen_at``), which re-reads the now-terminated
# row and re-inserts it into the cache — undoing the termination's
# revocation. ``terminate`` can't fix this after the fact either: it
# short-circuits on an already-terminal row, so the re-poisoned entry is
# never re-evicted. The invariant pinned here: no ``AgentRepository``
# write path may leave a row with status in ``TERMINAL_AGENT_STATUSES``
# in ``state.active_agents``.


def test_advance_event_cursor_does_not_rewarm_terminal_row(
    project_dir, reset_globals,
):
    """A cursor advance for a just-terminated agent must not resurrect
    its bearer in the auth cache (simulates an in-flight
    ``wait_for_events`` waiter resuming after termination)."""
    with _make_client(project_dir):
        from agent_mcp.app.main_app import _bearer_is_active
        from agent_mcp.core import state
        from agent_mcp.repositories import agent_repo

        _seed_agent(
            "agent-cursor-term", token="tok-cursor-term", status="active",
            working_directory="/tmp/cursor-term",
        )
        agent_repo.get_by_token("tok-cursor-term")
        assert "tok-cursor-term" in state.active_agents

        assert agent_repo.terminate("agent-cursor-term") is True
        assert "tok-cursor-term" not in state.active_agents

        # In-flight waiter resumes post-termination and advances its
        # cursor. The row still exists (terminated, not deleted), so
        # the watermark UPDATE still matches a row and returns True —
        # only the cache WRITE must be suppressed.
        advanced = agent_repo.advance_event_cursor(
            "agent-cursor-term", "2026-01-01T00:00:00",
        )
        assert advanced is True

        assert "tok-cursor-term" not in state.active_agents, (
            "advance_event_cursor re-warmed the auth cache with a "
            "terminated row"
        )
        assert _bearer_is_active("tok-cursor-term") is False


def test_update_field_does_not_rewarm_terminal_row(project_dir, reset_globals):
    """``update_field``'s own-connection path (``connection=None``) must
    not resurrect a terminated agent's bearer in the auth cache."""
    with _make_client(project_dir):
        from agent_mcp.app.main_app import _bearer_is_active
        from agent_mcp.core import state
        from agent_mcp.repositories import agent_repo

        _seed_agent(
            "agent-upd-term", token="tok-upd-term", status="active",
            working_directory="/tmp/upd-term",
        )
        agent_repo.get_by_token("tok-upd-term")
        assert "tok-upd-term" in state.active_agents

        assert agent_repo.terminate("agent-upd-term") is True
        assert "tok-upd-term" not in state.active_agents

        result = agent_repo.update_field(
            "agent-upd-term", "auto_event_loop", 0,
        )
        assert result is not None

        assert "tok-upd-term" not in state.active_agents, (
            "update_field re-warmed the auth cache with a terminated row"
        )
        assert _bearer_is_active("tok-upd-term") is False


def test_rotate_token_does_not_rewarm_terminal_row(project_dir, reset_globals):
    """``rotate_token``'s cache re-key (own-connection path) must not
    insert the new token for a terminal row.

    ``rotate_token`` has zero live callers today (the admin-relaunch
    flow it exists for hasn't landed yet) but pins the same invariant
    now so a future caller inherits it for free. Seeds a stale cache
    entry under the OLD token to simulate a warm race that left a
    terminated row cached (the scenario the re-key would otherwise
    carry forward under the new token).
    """
    with _make_client(project_dir):
        from agent_mcp.core import state
        from agent_mcp.repositories import agent_repo

        _seed_agent(
            "agent-rot-term", token="tok-rot-old", status="terminated",
            working_directory="/tmp/rot-term",
        )
        state.active_agents["tok-rot-old"] = {
            "agent_id": "agent-rot-term",
            "token": "tok-rot-old",
            "status": "terminated",
        }

        assert agent_repo.rotate_token("agent-rot-term", "tok-rot-new") is True

        assert "tok-rot-old" not in state.active_agents
        assert "tok-rot-new" not in state.active_agents, (
            "rotate_token re-warmed the auth cache with a terminal row "
            "under the new token"
        )


# --- pentest R1-F4: upsert_cache + create gated on non-terminal status ---
#
# SEC-A (#454) pinned "no AgentRepository write path may leave a row
# with status in TERMINAL_AGENT_STATUSES in state.active_agents" and
# gated the five re-warm sites it enumerated (get_by_id, get_by_token,
# update_field, advance_event_cursor, rotate_token). A follow-up
# class-sweep of every ``state.active_agents[...] = `` writer in
# ``agent_mcp/`` found the invariant was incomplete: ``upsert_cache``
# writes a caller-supplied row straight into the cache with no status
# check at all, and ``create()`` has the identical shape — its own
# cache write is unconditional on the ``status`` parameter it accepts.
# Neither is exploitable via a live caller today (``upsert_cache``'s
# only caller is ``register_agent``'s post-commit warm, which always
# hands it ``status="created"``; ``create()``'s only caller passes the
# same hardcoded default) but both are public repository methods that
# a future caller could hand a terminal row to, silently reactivating
# a revoked bearer — the exact SEC-A bug class. Gated both the same
# way as the other five.


def test_upsert_cache_does_not_cache_terminal_row(project_dir, reset_globals):
    """``upsert_cache`` must not warm the auth cache with a row whose
    status is terminal."""
    with _make_client(project_dir):
        from agent_mcp.app.main_app import _bearer_is_active
        from agent_mcp.core import state
        from agent_mcp.repositories import agent_repo

        agent_repo.upsert_cache({
            "token": "tok-upsert-term",
            "agent_id": "agent-upsert-term",
            "status": "terminated",
            "working_directory": "/tmp/upsert-term",
        })

        assert "tok-upsert-term" not in state.active_agents, (
            "upsert_cache cached a row with status='terminated'"
        )
        assert _bearer_is_active("tok-upsert-term") is False


def test_upsert_cache_does_not_cache_tombstone_row(project_dir, reset_globals):
    """Same gate, other terminal status (BL-R31-3b tombstone rows)."""
    with _make_client(project_dir):
        from agent_mcp.core import state
        from agent_mcp.repositories import agent_repo

        agent_repo.upsert_cache({
            "token": "tok-upsert-tomb",
            "agent_id": "agent-upsert-tomb",
            "status": "tombstone",
            "working_directory": "/tmp/upsert-tomb",
        })

        assert "tok-upsert-tomb" not in state.active_agents, (
            "upsert_cache cached a row with status='tombstone'"
        )


def test_upsert_cache_still_caches_active_row(project_dir, reset_globals):
    """No regression: the real caller (``register_agent``'s post-commit
    warm, always ``status="created"``) must still land in the cache."""
    with _make_client(project_dir):
        from agent_mcp.core import state
        from agent_mcp.repositories import agent_repo

        agent_repo.upsert_cache({
            "token": "tok-upsert-live",
            "agent_id": "agent-upsert-live",
            "status": "created",
            "working_directory": "/tmp/upsert-live",
        })

        assert state.active_agents.get("tok-upsert-live", {}).get(
            "agent_id"
        ) == "agent-upsert-live"
        assert state.agent_working_dirs.get("agent-upsert-live") == (
            "/tmp/upsert-live"
        )


def test_create_does_not_cache_terminal_row(project_dir, reset_globals):
    """``create()`` accepts a ``status`` parameter (default 'created')
    and must not warm the auth cache when a caller hands it a terminal
    one. The row is still written to the DB (create's job); only the
    cache write is gated."""
    with _make_client(project_dir):
        from agent_mcp.app.main_app import _bearer_is_active
        from agent_mcp.core import state
        from agent_mcp.repositories import agent_repo

        entity = agent_repo.create(
            token="tok-create-term",
            agent_id="agent-create-term",
            status="terminated",
            working_directory="/tmp/create-term",
        )

        assert entity["agent_id"] == "agent-create-term"
        assert "tok-create-term" not in state.active_agents, (
            "create() cached a row with status='terminated'"
        )
        assert _bearer_is_active("tok-create-term") is False


def test_create_still_caches_non_terminal_row(project_dir, reset_globals):
    """No regression: the default/real-caller path (``status="created"``)
    still lands in both caches."""
    with _make_client(project_dir):
        from agent_mcp.core import state
        from agent_mcp.repositories import agent_repo

        agent_repo.create(
            token="tok-create-live",
            agent_id="agent-create-live",
            status="created",
            working_directory="/tmp/create-live",
        )

        assert "tok-create-live" in state.active_agents
        assert state.agent_working_dirs.get("agent-create-live") == (
            "/tmp/create-live"
        )


# --- class-sweep: EVERY active_agents writer refuses a terminal row ------
#
# Strengthens the individual SEC-A regression tests above into one
# parametrized guard that drives every known AgentRepository write path
# into a terminal-status scenario and asserts none of them leave the
# token cached. A future writer that forgets the gate fails loudly here
# instead of shipping silently past the individually-named tests.


def _sweep_create(agent_repo, state, token, agent_id):
    agent_repo.create(
        token=token, agent_id=agent_id, status="terminated",
        working_directory="/tmp/sweep",
    )
    return [token]


def _sweep_upsert_cache(agent_repo, state, token, agent_id):
    agent_repo.upsert_cache({
        "token": token, "agent_id": agent_id, "status": "terminated",
        "working_directory": "/tmp/sweep",
    })
    return [token]


def _sweep_get_by_id(agent_repo, state, token, agent_id):
    _seed_agent(
        agent_id, token=token, status="terminated",
        working_directory="/tmp/sweep",
    )
    agent_repo.get_by_id(agent_id)
    return [token]


def _sweep_get_by_token(agent_repo, state, token, agent_id):
    _seed_agent(
        agent_id, token=token, status="terminated",
        working_directory="/tmp/sweep",
    )
    agent_repo.get_by_token(token)
    return [token]


def _sweep_update_field(agent_repo, state, token, agent_id):
    _seed_agent(
        agent_id, token=token, status="active",
        working_directory="/tmp/sweep",
    )
    agent_repo.get_by_token(token)
    agent_repo.terminate(agent_id)
    agent_repo.update_field(agent_id, "auto_event_loop", 0)
    return [token]


def _sweep_advance_event_cursor(agent_repo, state, token, agent_id):
    _seed_agent(
        agent_id, token=token, status="active",
        working_directory="/tmp/sweep",
    )
    agent_repo.get_by_token(token)
    agent_repo.terminate(agent_id)
    agent_repo.advance_event_cursor(agent_id, "2026-01-01T00:00:00")
    return [token]


def _sweep_rotate_token(agent_repo, state, token, agent_id):
    _seed_agent(
        agent_id, token=token, status="terminated",
        working_directory="/tmp/sweep",
    )
    state.active_agents[token] = {
        "agent_id": agent_id, "token": token, "status": "terminated",
    }
    new_token = f"{token}-new"
    agent_repo.rotate_token(agent_id, new_token)
    return [token, new_token]


_ACTIVE_AGENTS_WRITER_SWEEP = [
    ("create", _sweep_create),
    ("upsert_cache", _sweep_upsert_cache),
    ("get_by_id", _sweep_get_by_id),
    ("get_by_token", _sweep_get_by_token),
    ("update_field", _sweep_update_field),
    ("advance_event_cursor", _sweep_advance_event_cursor),
    ("rotate_token", _sweep_rotate_token),
]


@pytest.mark.parametrize(
    "case_name,driver",
    _ACTIVE_AGENTS_WRITER_SWEEP,
    ids=[c[0] for c in _ACTIVE_AGENTS_WRITER_SWEEP],
)
def test_every_active_agents_writer_refuses_terminal_row(
    case_name, driver, project_dir, reset_globals,
):
    """Every write path that can populate ``state.active_agents`` must
    refuse to cache a row whose status is terminal. Covers all seven
    known writers (the five SEC-A gated + the two pentest R1-F4 gated)
    so the invariant is pinned against the complete writer set, not
    just the ones enumerated to date."""
    with _make_client(project_dir):
        from agent_mcp.core import state
        from agent_mcp.repositories import agent_repo

        token = f"tok-sweep-{case_name}"
        agent_id = f"agent-sweep-{case_name}"
        tokens_to_check = driver(agent_repo, state, token, agent_id)

        for t in tokens_to_check:
            assert t not in state.active_agents, (
                f"{case_name} left a terminal-status row cached in "
                f"state.active_agents under token {t!r}"
            )
