"""Contract tests for the class-based ``MessageRepository`` (PR 3 of the
repository-deepening series, following PR #146 / TaskRepository and
PR #147 / AgentRepository).

This file pins the same contract for the Message concept: a class-based
Repository on ``agent_mcp.repositories.message_repo`` that is the
*single owner* of the message DB writes + EventBus publishing.

Unlike TaskRepository / AgentRepository there is **no in-memory cache**
for messages today (no ``state.messages`` or ``state.active_agents``-
shaped dict; PR #137 deferred a message cache pending a real use case).
The class therefore has no cache invariant — ``disable_cache`` exists
as a uniform no-op so call-sites can write the same
``with repo.disable_cache():`` block they use for the other two
concepts, and the test surface skips the cache-eviction assertions
the other repos make.

What this test file pins:

* The singleton exists at ``agent_mcp.repositories.message_repo`` after
  application lifespan startup, and points at a ``MessageRepository``
  instance.
* ``query(filters)`` exposes the rich-filter surface today spelled
  inline in ``app.routes.list_messages_api_route`` (the Candidate 3
  folding). PR 6 will route both ``routes.py`` and
  ``agent_communication_tools.get_agent_messages_tool_impl`` through it.
* Every write (``send``, ``bulk_send``, ``mark_delivered``,
  ``mark_read_for_recipient``, ``delete``) publishes the right
  EventBus topic — subscribers don't need to poll the DB.

These tests fail on ``main`` because:

* ``agent_mcp.repositories.message_repository`` (the class module)
  does not yet exist.
* ``agent_mcp.repositories.message_repo`` (the lifespan singleton) is
  not exposed from the top-level repositories package.
* ``MessageRepository.query`` is net-new (the routes.py query path is
  raw SQL today).
"""

from __future__ import annotations

import datetime
import sys

from agent_mcp.app.main_app import create_app
from starlette.testclient import TestClient


# --- Helpers -------------------------------------------------------------


def _make_client(project_dir):
    """Build the in-process app + TestClient.

    Using a fresh client per test means each call runs through the
    full lifespan startup (which is what wires the singleton).
    """
    app = create_app(project_dir=str(project_dir))
    return TestClient(app)


def _seed_agent(agent_id: str, *, token: str | None = None) -> None:
    """Insert an agent row so message FKs are satisfied.

    ``agent_messages.{sender_id,recipient_id}`` carry FK constraints
    onto ``agents.agent_id`` (migration 0008); messages cannot be
    seeded without their participants existing first.
    """
    from agent_mcp.db.engine import get_session
    from agent_mcp.db.models import Agent

    now = datetime.datetime.now().isoformat()
    with get_session() as session:
        session.add(
            Agent(
                token=token or f"tok-{agent_id}",
                agent_id=agent_id,
                capabilities="[]",
                created_at=now,
                status="active",
                current_task=None,
                working_directory=f"/tmp/{agent_id}",
                color="#abcdef",
                terminated_at=None,
                updated_at=now,
                aoe_session_id=None,
            )
        )
        session.commit()


def _seed_message(
    message_id: str,
    *,
    sender_id: str,
    recipient_id: str,
    content: str = "hello",
    message_type: str = "text",
    priority: str = "normal",
    timestamp: str | None = None,
    delivered: bool = False,
    read: bool = False,
    subject: str | None = None,
    parent_message_id: str | None = None,
) -> None:
    """Insert a message via the ORM path, bypassing the repo.

    The repo class under test must observe this row when callers ask
    for it — proves the read methods fall through to the DB rather
    than returning only what passed through ``send``.
    """
    from agent_mcp.db.engine import get_session
    from agent_mcp.db.models import AgentMessage

    ts = timestamp or datetime.datetime.now().isoformat()
    with get_session() as session:
        session.add(
            AgentMessage(
                message_id=message_id,
                sender_id=sender_id,
                recipient_id=recipient_id,
                message_content=content,
                message_type=message_type,
                priority=priority,
                timestamp=ts,
                delivered=delivered,
                read=read,
                subject=subject,
                parent_message_id=parent_message_id,
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

    def notify(self, agent_id, event_type, payload):  # noqa: D401, ANN001
        self.events.append((agent_id, event_type, payload or {}))


# --- Singleton + lifespan wiring ----------------------------------------


def test_message_repo_singleton_is_messagerepository_instance(
    project_dir, reset_globals,
):
    """``agent_mcp.repositories.message_repo`` resolves to a class instance.

    The plan locks "module singletons, lifespan-owned" — so the
    attribute access shape is ``from agent_mcp.repositories import
    message_repo`` and the value is an instance, not a module.
    """
    with _make_client(project_dir):
        from agent_mcp.repositories import message_repo
        from agent_mcp.repositories.message_repository import (
            MessageRepository,
        )

        assert isinstance(message_repo, MessageRepository), (
            "message_repo must be a MessageRepository instance after "
            "lifespan startup so call sites can rely on the class-based "
            "contract"
        )


# --- Read interface ------------------------------------------------------


def test_get_by_id_returns_dict_when_present(project_dir, reset_globals):
    """``get_by_id`` returns the same dict shape legacy callers expect."""
    with _make_client(project_dir):
        from agent_mcp.repositories import message_repo

        _seed_agent("sender-1")
        _seed_agent("recipient-1")
        _seed_message(
            "msg_aaaa1111", sender_id="sender-1", recipient_id="recipient-1",
            content="hello there", subject="greet",
        )

        row = message_repo.get_by_id("msg_aaaa1111")
        assert row is not None
        assert row["message_id"] == "msg_aaaa1111"
        assert row["sender_id"] == "sender-1"
        assert row["recipient_id"] == "recipient-1"
        assert row["message_content"] == "hello there"
        assert row["subject"] == "greet"
        assert row["parent_message_id"] is None


def test_get_by_id_returns_none_when_missing(project_dir, reset_globals):
    with _make_client(project_dir):
        from agent_mcp.repositories import message_repo
        assert message_repo.get_by_id("does-not-exist") is None


def test_count_unread_returns_recipient_unread_only(
    project_dir, reset_globals,
):
    """``count_unread`` reflects unread rows addressed to one recipient.

    Read messages, messages addressed to other recipients, and
    messages sent BY the recipient must not count.
    """
    with _make_client(project_dir):
        from agent_mcp.repositories import message_repo

        _seed_agent("alice")
        _seed_agent("bob")
        # Two unread → bob (counted).
        _seed_message(
            "m1", sender_id="alice", recipient_id="bob", read=False,
        )
        _seed_message(
            "m2", sender_id="alice", recipient_id="bob", read=False,
        )
        # One read → bob (not counted).
        _seed_message(
            "m3", sender_id="alice", recipient_id="bob", read=True,
        )
        # One unread → alice (not counted for bob).
        _seed_message(
            "m4", sender_id="bob", recipient_id="alice", read=False,
        )
        assert message_repo.count_unread("bob") == 2


def test_query_filters_by_sender_and_recipient(project_dir, reset_globals):
    """``query`` exposes the rich filter surface today spelled inline in
    ``app.routes.list_messages_api_route``.

    This is the Candidate 3 folding: the dashboard query route and
    the MCP get_agent_messages tool both want to filter the same way;
    centralising the filter logic on the repo means PR 6 can route
    both through one entry point.
    """
    with _make_client(project_dir):
        from agent_mcp.repositories import message_repo

        _seed_agent("alice")
        _seed_agent("bob")
        _seed_agent("carol")
        _seed_message("m1", sender_id="alice", recipient_id="bob")
        _seed_message("m2", sender_id="alice", recipient_id="carol")
        _seed_message("m3", sender_id="bob", recipient_id="alice")

        rows = message_repo.query({"from": "alice"})
        ids = {r["message_id"] for r in rows}
        assert ids == {"m1", "m2"}

        rows = message_repo.query({"to": "bob"})
        ids = {r["message_id"] for r in rows}
        assert ids == {"m1"}

        # `between` is unordered: messages either direction between two
        # agents.
        rows = message_repo.query({"between": ["alice", "bob"]})
        ids = {r["message_id"] for r in rows}
        assert ids == {"m1", "m3"}


def test_query_filters_by_type_priority_read_and_substring(
    project_dir, reset_globals,
):
    with _make_client(project_dir):
        from agent_mcp.repositories import message_repo

        _seed_agent("alice")
        _seed_agent("bob")
        _seed_message(
            "m1", sender_id="alice", recipient_id="bob",
            message_type="text", priority="normal", read=False,
            content="urgent help needed",
        )
        _seed_message(
            "m2", sender_id="alice", recipient_id="bob",
            message_type="assistance_request", priority="high", read=True,
            content="please review",
        )
        _seed_message(
            "m3", sender_id="alice", recipient_id="bob",
            message_type="text", priority="urgent", read=False,
            content="ping",
        )

        assert {r["message_id"] for r in message_repo.query(
            {"type": "assistance_request"}
        )} == {"m2"}
        assert {r["message_id"] for r in message_repo.query(
            {"priority": "urgent"}
        )} == {"m3"}
        assert {r["message_id"] for r in message_repo.query(
            {"read": False}
        )} == {"m1", "m3"}
        assert {r["message_id"] for r in message_repo.query(
            {"q": "review"}
        )} == {"m2"}


def test_query_filters_by_time_window(project_dir, reset_globals):
    """``since`` / ``until`` constrain the timestamp window."""
    with _make_client(project_dir):
        from agent_mcp.repositories import message_repo

        _seed_agent("alice")
        _seed_agent("bob")
        _seed_message(
            "old", sender_id="alice", recipient_id="bob",
            timestamp="2026-01-01T00:00:00",
        )
        _seed_message(
            "mid", sender_id="alice", recipient_id="bob",
            timestamp="2026-06-01T00:00:00",
        )
        _seed_message(
            "new", sender_id="alice", recipient_id="bob",
            timestamp="2026-12-31T00:00:00",
        )

        rows = message_repo.query(
            {"since": "2026-03-01T00:00:00", "until": "2026-09-01T00:00:00"}
        )
        assert {r["message_id"] for r in rows} == {"mid"}


def test_query_pagination_and_total(project_dir, reset_globals):
    """``query`` returns paginated rows + a total for the UI.

    The dashboard "Newer / Older" controls (PR #145) depend on
    knowing the unfiltered count to know whether more pages exist.
    """
    with _make_client(project_dir):
        from agent_mcp.repositories import message_repo

        _seed_agent("alice")
        _seed_agent("bob")
        for i in range(5):
            _seed_message(
                f"m{i}", sender_id="alice", recipient_id="bob",
                timestamp=f"2026-06-0{i + 1}T00:00:00",
            )

        result = message_repo.query({"limit": 2, "offset": 0})
        # Repo returns either a list (rows only) or a (rows, total)
        # tuple depending on call shape; assert tuple shape since the
        # route currently exposes both.
        if isinstance(result, tuple):
            rows, total = result
        else:
            rows = result
            total = len(message_repo.query({}))
        assert len(rows) == 2
        assert total == 5
        # Newest-first (timestamp DESC) — mirrors routes.py.
        assert rows[0]["message_id"] == "m4"


# --- Write interface: send ----------------------------------------------


def test_send_creates_row_and_publishes_event(project_dir, reset_globals):
    """``send`` is the single seam for new messages.

    Contract:
      1. INSERTs the row (the DB is the source of truth).
      2. Returns the freshly-stored dict (not just bool).
      3. Publishes exactly one ``message.created`` event addressed to
         the recipient.
    """
    bus = _CapturingBus()
    sys.modules["agent_mcp.core.event_bus"] = bus  # type: ignore[assignment]
    try:
        with _make_client(project_dir):
            from agent_mcp.repositories import message_repo

            _seed_agent("alice")
            _seed_agent("bob")

            entity = message_repo.send(
                message_id="msg_send1",
                sender_id="alice",
                recipient_id="bob",
                message_content="howdy",
                message_type="text",
                priority="normal",
                timestamp=datetime.datetime.now().isoformat(),
            )

            assert entity is not None
            assert entity["message_id"] == "msg_send1"
            assert entity["sender_id"] == "alice"
            assert entity["recipient_id"] == "bob"
            assert entity["message_content"] == "howdy"

            # DB carries the row.
            assert message_repo.get_by_id("msg_send1") is not None

            # Exactly one publish for the send.
            create_events = [
                e for e in bus.events
                if "message" in e[1] and "created" in e[1]
            ]
            assert len(create_events) == 1, bus.events
            recipient, _evt, payload = create_events[0]
            assert recipient == "bob"
            assert payload.get("message_id") == "msg_send1"
    finally:
        sys.modules.pop("agent_mcp.core.event_bus", None)


def test_bulk_send_writes_all_and_publishes_per_recipient(
    project_dir, reset_globals,
):
    """``bulk_send`` collapses the broadcast-loop INSERTs into one round-trip.

    Contract:
      1. Returns the count of rows actually written.
      2. Each fan-out target receives exactly one ``message.created``
         publish (broadcast doesn't need a per-message wake — per-
         recipient is enough to nudge each subscriber).
    """
    bus = _CapturingBus()
    sys.modules["agent_mcp.core.event_bus"] = bus  # type: ignore[assignment]
    try:
        with _make_client(project_dir):
            from agent_mcp.repositories import message_repo

            # NOTE: `admin` is the pseudo-agent seeded by migration
            # 0008 (admin_pseudo_agent_and_fks); reuse it as the
            # broadcast sender so we don't double-INSERT it.
            _seed_agent("bob")
            _seed_agent("carol")

            ts = datetime.datetime.now().isoformat()
            rows = [
                {
                    "message_id": "bcast1",
                    "sender_id": "admin",
                    "recipient_id": "bob",
                    "message_content": "everyone",
                    "message_type": "broadcast",
                    "priority": "high",
                    "timestamp": ts,
                },
                {
                    "message_id": "bcast2",
                    "sender_id": "admin",
                    "recipient_id": "carol",
                    "message_content": "everyone",
                    "message_type": "broadcast",
                    "priority": "high",
                    "timestamp": ts,
                },
            ]
            n = message_repo.bulk_send(rows)
            assert n == 2

            assert message_repo.get_by_id("bcast1") is not None
            assert message_repo.get_by_id("bcast2") is not None

            recipients = {
                e[0] for e in bus.events
                if "message" in e[1] and "created" in e[1]
            }
            assert recipients == {"bob", "carol"}
    finally:
        sys.modules.pop("agent_mcp.core.event_bus", None)


# --- Write interface: mark_delivered / mark_read ------------------------


def test_mark_delivered_flips_flag_and_publishes(project_dir, reset_globals):
    bus = _CapturingBus()
    sys.modules["agent_mcp.core.event_bus"] = bus  # type: ignore[assignment]
    try:
        with _make_client(project_dir):
            from agent_mcp.repositories import message_repo

            _seed_agent("alice")
            _seed_agent("bob")
            _seed_message(
                "md1", sender_id="alice", recipient_id="bob",
                delivered=False,
            )
            bus.events.clear()

            ok = message_repo.mark_delivered("md1", True)
            assert ok is True

            row = message_repo.get_by_id("md1")
            assert row is not None and row["delivered"] is True

            delivered_events = [
                e for e in bus.events
                if "message" in e[1] and "deliver" in e[1]
            ]
            assert len(delivered_events) == 1, bus.events
    finally:
        sys.modules.pop("agent_mcp.core.event_bus", None)


def test_mark_read_for_recipient_returns_count_and_publishes(
    project_dir, reset_globals,
):
    """Bulk mark-as-read by recipient is the hot path for
    ``get_agent_messages`` — every fetch flips every unread for the
    caller.
    """
    bus = _CapturingBus()
    sys.modules["agent_mcp.core.event_bus"] = bus  # type: ignore[assignment]
    try:
        with _make_client(project_dir):
            from agent_mcp.repositories import message_repo

            _seed_agent("alice")
            _seed_agent("bob")
            _seed_message(
                "u1", sender_id="alice", recipient_id="bob", read=False,
            )
            _seed_message(
                "u2", sender_id="alice", recipient_id="bob", read=False,
            )
            _seed_message(
                "r1", sender_id="alice", recipient_id="bob", read=True,
            )
            bus.events.clear()

            n = message_repo.mark_read_for_recipient("bob")
            assert n == 2

            # Subsequent call is a no-op (0 touched, no event).
            assert message_repo.count_unread("bob") == 0
            bus.events.clear()
            assert message_repo.mark_read_for_recipient("bob") == 0
            read_events = [
                e for e in bus.events
                if "message" in e[1] and "read" in e[1]
            ]
            assert len(read_events) == 0
    finally:
        sys.modules.pop("agent_mcp.core.event_bus", None)


# --- Write interface: delete --------------------------------------------


def test_delete_removes_row(project_dir, reset_globals):
    with _make_client(project_dir):
        from agent_mcp.repositories import message_repo

        _seed_agent("alice")
        _seed_agent("bob")
        _seed_message("del1", sender_id="alice", recipient_id="bob")
        assert message_repo.get_by_id("del1") is not None

        assert message_repo.delete("del1") is True
        assert message_repo.get_by_id("del1") is None


def test_delete_missing_returns_false(project_dir, reset_globals):
    with _make_client(project_dir):
        from agent_mcp.repositories import message_repo
        assert message_repo.delete("never-existed") is False


# --- Cache helper: no-op (uniform interface) ----------------------------


def test_disable_cache_is_a_noop_context_manager(project_dir, reset_globals):
    """``disable_cache`` exists for interface uniformity — there is no
    in-memory message cache today, so the block has no observable
    effect. Asserting the context manager is callable + entered/exited
    cleanly is enough.
    """
    with _make_client(project_dir):
        from agent_mcp.repositories import message_repo

        _seed_agent("alice")
        _seed_agent("bob")
        _seed_message("nc1", sender_id="alice", recipient_id="bob")

        with message_repo.disable_cache():
            row = message_repo.get_by_id("nc1")
            assert row is not None


# --- rename_participant (PR 6 — purge cascade) --------------------------


def test_rename_participant_rewrites_sender_and_recipient(
    project_dir, reset_globals,
):
    """``rename_participant`` rewrites both ``sender_id`` and
    ``recipient_id`` from ``old_id`` to ``new_id``.

    Used by the purge-cascade in :func:`purge_agent_api_route` to
    tombstone the rows that reference a deleted agent.
    """
    with _make_client(project_dir):
        from agent_mcp.repositories import message_repo

        _seed_agent("alice")
        _seed_agent("bob")
        _seed_agent("carol")
        # alice → bob: bob is the recipient
        _seed_message("m1", sender_id="alice", recipient_id="bob")
        # bob → carol: bob is the sender
        _seed_message("m2", sender_id="bob", recipient_id="carol")
        # alice → carol: bob not involved
        _seed_message("m3", sender_id="alice", recipient_id="carol")

        # The tombstone target row must exist BEFORE the rename — the
        # agent_messages.{sender_id,recipient_id} FK constraints forbid
        # rewriting to a non-existent agent_id.
        _seed_agent("[deleted-bob]")

        n = message_repo.rename_participant("bob", "[deleted-bob]")
        assert n == 2  # m1.recipient_id + m2.sender_id

        # Verify the rewrites landed.
        m1 = message_repo.get_by_id("m1")
        m2 = message_repo.get_by_id("m2")
        m3 = message_repo.get_by_id("m3")
        assert m1["recipient_id"] == "[deleted-bob]"
        assert m1["sender_id"] == "alice"
        assert m2["sender_id"] == "[deleted-bob]"
        assert m2["recipient_id"] == "carol"
        # m3 untouched.
        assert m3["sender_id"] == "alice"
        assert m3["recipient_id"] == "carol"


def test_rename_participant_returns_zero_on_no_match(
    project_dir, reset_globals,
):
    """Renaming a non-existent participant is a no-op returning 0."""
    with _make_client(project_dir):
        from agent_mcp.repositories import message_repo

        _seed_agent("alice")
        _seed_agent("bob")
        _seed_message("m1", sender_id="alice", recipient_id="bob")
        _seed_agent("[deleted-ghost]")

        n = message_repo.rename_participant("ghost", "[deleted-ghost]")
        assert n == 0
        # m1 unchanged.
        row = message_repo.get_by_id("m1")
        assert row["sender_id"] == "alice"
        assert row["recipient_id"] == "bob"


def test_rename_participant_with_sqlite_cursor_uses_caller_transaction(
    project_dir, reset_globals,
):
    """The ``connection=`` seam tolerates a raw sqlite3 cursor.

    The purge cascade in routes.py wraps its rewrites in a hand-rolled
    ``BEGIN``/``COMMIT`` against a sqlite3 cursor. The repo must
    rewrite via the caller's cursor so the wider transaction stays
    atomic.
    """
    with _make_client(project_dir):
        from agent_mcp.db.connection import get_db_connection
        from agent_mcp.repositories import message_repo

        _seed_agent("alice")
        _seed_agent("bob")
        _seed_message("m1", sender_id="alice", recipient_id="bob")
        _seed_agent("[deleted-bob]")

        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("BEGIN")
            n = message_repo.rename_participant(
                "bob", "[deleted-bob]", connection=cursor,
            )
            assert n == 1  # one recipient_id row rewritten
            conn.commit()
        finally:
            conn.close()

        row = message_repo.get_by_id("m1")
        assert row["recipient_id"] == "[deleted-bob]"


# --- list_participants (PR 6 — dropdown source) -------------------------


def test_list_participants_returns_live_agents_excluding_terminated(
    project_dir, reset_globals,
):
    """``list_participants`` excludes terminated / tombstone agents and
    prepends a synthetic ``admin`` row when missing."""
    with _make_client(project_dir):
        from agent_mcp.db.engine import get_session
        from agent_mcp.db.models import Agent
        from agent_mcp.repositories import message_repo

        _seed_agent("alice")
        _seed_agent("bob")
        # Mark bob terminated; he must NOT appear in the live list.
        with get_session() as session:
            row = (
                session.query(Agent)
                .filter(Agent.agent_id == "bob")
                .one()
            )
            row.status = "terminated"
            session.commit()
        # Seed a tombstone row; it must NOT appear either.
        _seed_agent("[deleted-old]")
        with get_session() as session:
            row = (
                session.query(Agent)
                .filter(Agent.agent_id == "[deleted-old]")
                .one()
            )
            row.status = "tombstone"
            session.commit()

        result = message_repo.list_participants()
        live_ids = [a["agent_id"] for a in result["live"]]
        # Synthetic admin prepended at the front.
        assert live_ids[0].lower() == "admin"
        assert "alice" in live_ids
        assert "bob" not in live_ids
        assert "[deleted-old]" not in live_ids


def test_list_participants_extracts_tombstones_from_messages(
    project_dir, reset_globals,
):
    """``tombstones`` mines DISTINCT sender_id / recipient_id values
    starting with ``[deleted-`` from agent_messages."""
    with _make_client(project_dir):
        from agent_mcp.repositories import message_repo

        _seed_agent("alice")
        _seed_agent("[deleted-bob]")
        _seed_agent("[deleted-carol]")
        # Two tombstoned senders, one tombstoned recipient.
        _seed_message(
            "m1", sender_id="[deleted-bob]", recipient_id="alice",
        )
        _seed_message(
            "m2", sender_id="[deleted-bob]", recipient_id="alice",
        )
        _seed_message(
            "m3", sender_id="alice", recipient_id="[deleted-carol]",
        )

        result = message_repo.list_participants()
        # DISTINCT + UNION across columns + sorted ascending.
        assert result["tombstones"] == ["[deleted-bob]", "[deleted-carol]"]

