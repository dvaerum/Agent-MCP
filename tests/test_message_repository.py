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

    def notify(self, agent_id, event_type, payload):
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


def test_query_q_matches_subject_and_sender_not_just_content(
    project_dir, reset_globals,
):
    """``q`` broadens beyond content to subject + sender + recipient.

    Regression guard for the Messages-UX search broadening: a search
    term must match by the subject line and by the sender/recipient
    ids, not only the message body.
    """
    with _make_client(project_dir):
        from agent_mcp.repositories import message_repo

        _seed_agent("alice")
        _seed_agent("bob")
        _seed_agent("charlie")
        # Body has no match; subject does.
        _seed_message(
            "s1", sender_id="alice", recipient_id="bob",
            content="body text with nothing special",
            subject="Quarterly deployment plan",
        )
        # Body + subject have no match; sender does.
        _seed_message(
            "s2", sender_id="charlie", recipient_id="bob",
            content="unrelated body", subject="misc",
        )
        # Nothing matches either term.
        _seed_message(
            "s3", sender_id="alice", recipient_id="bob",
            content="just chatting", subject="hi",
        )

        # Match by subject (not present in any body).
        assert {r["message_id"] for r in message_repo.query(
            {"q": "deployment"}
        )} == {"s1"}
        # Match by sender id.
        assert {r["message_id"] for r in message_repo.query(
            {"q": "charlie"}
        )} == {"s2"}


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


def test_query_offset_pagination_survives_concurrent_read_flag_change(
    project_dir, reset_globals,
):
    """R17-F2 class-sweep sibling: ``MessageRepository.query`` (backs
    ``POST /api/messages/query``) re-filters the live ``agent_messages``
    table on every call, exactly like the ``view_tasks`` case. 5 unread
    messages, paginate ``limit=2``; between page 1 and page 2 the
    newest message (m4) gets marked read and drops out of the
    ``read=False`` filter — a message that was unread the ENTIRE time
    (m2) must not be silently skipped.
    """
    with _make_client(project_dir):
        from agent_mcp.repositories import message_repo
        from agent_mcp.db.engine import get_session
        from agent_mcp.db.models import AgentMessage

        _seed_agent("alice")
        _seed_agent("bob")
        for i in range(5):
            _seed_message(
                f"pgm{i}", sender_id="alice", recipient_id="bob",
                timestamp=f"2026-07-0{i + 1}T00:00:00", read=False,
            )

        filters = {"to": "bob", "read": False, "limit": 2}
        page1 = message_repo.query({**filters, "offset": 0})
        assert [m["message_id"] for m in page1] == ["pgm4", "pgm3"]

        # Ordinary concurrent activity between the two page requests.
        with get_session() as session:
            session.query(AgentMessage).filter(
                AgentMessage.message_id == "pgm4"
            ).update({"read": True})
            session.commit()

        page2 = message_repo.query({**filters, "offset": 2})

        seen_ids = {m["message_id"] for m in page1} | {
            m["message_id"] for m in page2
        }
        assert "pgm2" in seen_ids, (
            "pgm2 was unread for the entire window and must not be "
            f"silently skipped; page1={page1!r} page2={page2!r}"
        )
        assert [m["message_id"] for m in page2] == ["pgm2", "pgm1"]


def test_count_query_offset_pagination_survives_concurrent_read_flag_change(
    project_dir, reset_globals,
):
    """Sibling of the ``query`` test above for ``count_query``: the
    "Total: N" figure a paginated caller sees must stay consistent with
    the anchored window ``query`` returned, not drift mid-sweep from a
    fresh unconditional COUNT.
    """
    with _make_client(project_dir):
        from agent_mcp.repositories import message_repo
        from agent_mcp.db.engine import get_session
        from agent_mcp.db.models import AgentMessage

        _seed_agent("carol")
        _seed_agent("dave")
        for i in range(5):
            _seed_message(
                f"cqm{i}", sender_id="carol", recipient_id="dave",
                timestamp=f"2026-07-1{i + 1}T00:00:00", read=False,
            )

        filters = {"to": "dave", "read": False, "limit": 2}
        message_repo.query({**filters, "offset": 0})
        first_total = message_repo.count_query({**filters, "offset": 0})
        assert first_total == 5

        with get_session() as session:
            session.query(AgentMessage).filter(
                AgentMessage.message_id == "cqm4"
            ).update({"read": True})
            session.commit()

        second_total = message_repo.count_query({**filters, "offset": 2})
        assert second_total == 5, (
            "total must stay anchored to the sweep started at offset=0, "
            f"not drift to a fresh live count mid-sweep: {second_total}"
        )


def test_count_query_total_excludes_message_deleted_mid_sweep(
    project_dir, reset_globals,
):
    """R21-F3: ``count_query`` must subtract anchored ids that no
    longer resolve to a live row by read time -- not just report the
    raw anchor length.

    7 messages anchored at offset=0 -> total 7. One NOT-yet-fetched
    anchored message is hard-deleted (``message_repo.delete``). Paging
    through offset=2,4,6 (limit=2 each, same filter) must report
    total=6 on every remaining page -- ``query``'s window already
    correctly omits the deleted row -- and the rows actually
    delivered across the whole sweep must sum to 6.
    """
    with _make_client(project_dir):
        from agent_mcp.repositories import message_repo

        _seed_agent("erin")
        _seed_agent("frank")
        for i in range(7):
            _seed_message(
                f"tcm{i}", sender_id="erin", recipient_id="frank",
                timestamp=f"2026-08-0{i + 1}T00:00:00", read=False,
            )

        filters = {"to": "frank", "read": False, "limit": 2}
        page1 = message_repo.query({**filters, "offset": 0})
        total1 = message_repo.count_query({**filters, "offset": 0})
        assert total1 == 7
        delivered = list(page1)

        # tcm4 (anchored, third-ranked -- DESC by timestamp -- not yet
        # fetched) is deleted outright.
        assert message_repo.delete("tcm4") is True

        for offset in (2, 4, 6):
            page = message_repo.query({**filters, "offset": offset})
            total = message_repo.count_query({**filters, "offset": offset})
            assert total == 6, (
                f"total must exclude the anchored-but-deleted message; "
                f"offset={offset} reported {total}"
            )
            delivered.extend(page)

        assert len(delivered) == 6, (
            f"rows actually delivered across the full sweep must equal "
            f"the reported total; delivered="
            f"{[m['message_id'] for m in delivered]!r}"
        )


def test_count_query_does_not_clobber_query_anchor_with_unordered_ids(
    project_dir, reset_globals,
):
    """R18-F2: ``list_messages_api_route`` calls ``query()`` then
    ``count_query()`` back-to-back with IDENTICAL filters on every
    request (see ``agent_mcp/app/routers/messages.py``). Both share
    ONE ``StableOrderCache`` entry (keyed by filter shape), and
    ``get_or_anchor`` always recomputes + OVERWRITES that entry on
    ``offset == 0``. If ``count_query``'s ``compute_ordered_ids``
    closure sorts differently than ``query``'s (or not at all), calling
    it right after ``query()`` on the SAME page-1 request clobbers the
    correct anchor with the wrong one -- corrupting every subsequent
    ``offset > 0`` page of the sweep, with no concurrency or mutation
    needed at all.

    6 messages, page1 (offset=0, limit=10, mirroring the route calling
    query() then count_query()) must anchor the DESC ordering; page2
    (offset=2, same filters, no writes in between) must then replay
    that SAME DESC ordering, not an unordered/ASC one.

    Filters deliberately carry no ``to``/``from`` (the dashboard's
    default, unfiltered "all messages" Messages-tab view) so
    ``compute_ordered_ids`` falls back to a plain unindexed table
    scan -- rowid/insertion order -- rather than an index seek on
    ``recipient_id``/``sender_id`` that can incidentally happen to
    come back in descending order and mask the missing ``ORDER BY``.
    """
    with _make_client(project_dir):
        from agent_mcp.repositories import message_repo

        _seed_agent("eve")
        _seed_agent("frank")
        for i in range(1, 7):
            _seed_message(
                f"m{i}", sender_id="eve", recipient_id="frank",
                timestamp=f"2026-08-0{i}T00:00:00",
            )

        filters = {"limit": 10}

        # Mirrors list_messages_api_route: query() then count_query(),
        # identical filters, both at offset=0.
        page1 = message_repo.query({**filters, "offset": 0})
        assert [m["message_id"] for m in page1] == [
            "m6", "m5", "m4", "m3", "m2", "m1",
        ], page1
        total = message_repo.count_query({**filters, "offset": 0})
        assert total == 6, total

        # No writes in between -- a second page must replay the exact
        # same anchored DESC ordering, sliced by offset.
        page2 = message_repo.query({**filters, "offset": 2})
        assert [m["message_id"] for m in page2] == [
            "m4", "m3", "m2", "m1",
        ], (
            "count_query() must not clobber query()'s DESC-ordered "
            f"anchor with an unordered list; got {page2!r}"
        )


def test_query_and_count_query_anchor_the_identical_ordered_ids(
    project_dir, reset_globals,
):
    """Class-of-bug regression guard (not just this one instance):
    ``query()`` and ``count_query()`` must compute the SAME ordered id
    sequence for the identical filter shape, so their shared
    ``StableOrderCache`` entry can never drift depending on which one
    last wrote it. Asserts full-sweep equality directly rather than
    re-deriving the R18-F2 scenario, so any FUTURE drift between the
    two closures (not just an ordering difference) fails loudly here.

    No ``to``/``from`` filter, for the same reason as the sibling test
    above: an unindexed table scan is what actually exposes a missing
    ``ORDER BY`` deterministically (an index seek on a `to`/`from`
    equality filter can incidentally come back sorted either way).
    """
    with _make_client(project_dir):
        from agent_mcp.repositories import message_repo

        _seed_agent("gina")
        _seed_agent("hank")
        for i in range(1, 6):
            _seed_message(
                f"n{i}", sender_id="gina", recipient_id="hank",
                timestamp=f"2026-08-1{i}T00:00:00",
            )

        filters = {"limit": 100}

        message_repo.query({**filters, "offset": 0})
        anchor_after_query = list(
            next(iter(message_repo._pagination_cache._store.values()))[1]
        )

        message_repo.count_query({**filters, "offset": 0})
        anchor_after_count = list(
            next(iter(message_repo._pagination_cache._store.values()))[1]
        )

        assert anchor_after_query == ["n5", "n4", "n3", "n2", "n1"]
        assert anchor_after_count == anchor_after_query, (
            "query() and count_query() must compute the IDENTICAL "
            "ordered id sequence for the same filter shape -- "
            f"query()={anchor_after_query!r} "
            f"count_query()={anchor_after_count!r}"
        )


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

            # NOTE: `admin` is the system bearer's actor label
            # (returned by ``get_agent_id(g.system_token)``). Wave 4
            # dropped the FK constraint that previously required a
            # corresponding agents-table row, so we can use it as a
            # sender_id without any seeding.
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


# --- Transaction-aware seam on send / mark_delivered / delete (PR #152) ---


def test_send_with_sqlite_cursor_lands_in_caller_transaction(
    project_dir, reset_globals,
):
    """``send(connection=cursor)`` writes the message row through the
    caller's cursor so the message INSERT is atomic with the audit-log
    INSERT in the send_agent_message_tool_impl pattern."""
    with _make_client(project_dir):
        from agent_mcp.db.connection import get_db_connection
        from agent_mcp.repositories import message_repo

        _seed_agent("alice")
        _seed_agent("bob")

        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("BEGIN")
            fresh = message_repo.send(
                message_id="msg_seam_1",
                sender_id="alice",
                recipient_id="bob",
                message_content="hello via cursor",
                message_type="text",
                priority="normal",
                timestamp="2026-06-11T12:00:00",
                subject="seam test",
                connection=cursor,
            )
            assert fresh is not None
            assert fresh["subject"] == "seam test"
            conn.commit()
        finally:
            conn.close()

        row = message_repo.get_by_id("msg_seam_1")
        assert row is not None
        assert row["message_content"] == "hello via cursor"


def test_send_with_sqlite_cursor_rolls_back_with_outer_transaction(
    project_dir, reset_globals,
):
    """Rollback of the caller's tx must drop the message — proves the
    INSERT runs in the caller's transaction, not a hidden session."""
    with _make_client(project_dir):
        from agent_mcp.db.connection import get_db_connection
        from agent_mcp.repositories import message_repo

        _seed_agent("alice")
        _seed_agent("bob")

        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("BEGIN")
            message_repo.send(
                message_id="msg_seam_rollback",
                sender_id="alice",
                recipient_id="bob",
                message_content="should not persist",
                message_type="text",
                priority="normal",
                timestamp="2026-06-11T12:00:00",
                connection=cursor,
            )
            conn.rollback()
        finally:
            conn.close()

        assert message_repo.get_by_id("msg_seam_rollback") is None


def test_mark_delivered_with_sqlite_cursor_uses_caller_transaction(
    project_dir, reset_globals,
):
    """``mark_delivered(connection=cursor)`` flips the flag through
    the caller's cursor, returns True on success."""
    with _make_client(project_dir):
        from agent_mcp.db.connection import get_db_connection
        from agent_mcp.repositories import message_repo

        _seed_agent("alice")
        _seed_agent("bob")
        _seed_message("m1", sender_id="alice", recipient_id="bob",
                      delivered=False)

        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("BEGIN")
            ok = message_repo.mark_delivered("m1", connection=cursor)
            assert ok is True
            conn.commit()
        finally:
            conn.close()

        row = message_repo.get_by_id("m1")
        assert row["delivered"] is True


def test_delete_with_sqlite_cursor_uses_caller_transaction(
    project_dir, reset_globals,
):
    """``delete(connection=cursor)`` removes the row through the
    caller's cursor — supports the patch_message_api_route DELETE
    + audit-log atomic write."""
    with _make_client(project_dir):
        from agent_mcp.db.connection import get_db_connection
        from agent_mcp.repositories import message_repo

        _seed_agent("alice")
        _seed_agent("bob")
        _seed_message("m1", sender_id="alice", recipient_id="bob")

        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("BEGIN")
            ok = message_repo.delete("m1", connection=cursor)
            assert ok is True
            conn.commit()
        finally:
            conn.close()

        assert message_repo.get_by_id("m1") is None


# --- Write interface: recipient existence enforcement -------------------
#
# VM e2e on 2026-06-16 surfaced that ``send_agent_message`` to a
# typo'd recipient succeeded with response "Message sent to
# nonexistent. Message stored; recipient has no active session." —
# the FK from PR #138 was being silently bypassed somewhere on the
# tool path.
#
# Locked design (Dennis, 2026-06-16): the Repository rejects with a
# clear error when ``recipient_id`` isn't a known agent (live agents,
# the synthetic ``admin`` pseudo-agent seeded by migration 0008, and
# tombstone rows ``[deleted-<id>]`` are all valid agent rows so a
# single "exists in agents table" check covers all three legitimate
# cases). Typos / non-existent IDs are rejected.


def test_send_to_live_recipient_succeeds(project_dir, reset_globals):
    """``send`` accepts a recipient that exists in the agents table."""
    with _make_client(project_dir):
        from agent_mcp.repositories import message_repo

        _seed_agent("alice")
        _seed_agent("backend-dev")

        entity = message_repo.send(
            message_id="msg_live",
            sender_id="alice",
            recipient_id="backend-dev",
            message_content="hi",
            message_type="text",
            priority="normal",
            timestamp=datetime.datetime.now().isoformat(),
        )
        assert entity is not None
        assert message_repo.get_by_id("msg_live") is not None


def test_send_to_admin_recipient_succeeds(project_dir, reset_globals):
    """``send`` accepts ``recipient_id='admin'`` — worker→admin
    handoffs remain a legitimate capability post-Wave-4. The recipient
    column is now a free-form label (no FK constraint), so the message
    lands without any agents-table parent."""
    with _make_client(project_dir):
        from agent_mcp.repositories import message_repo

        _seed_agent("worker-1")

        entity = message_repo.send(
            message_id="msg_admin",
            sender_id="worker-1",
            recipient_id="admin",
            message_content="help",
            message_type="text",
            priority="normal",
            timestamp=datetime.datetime.now().isoformat(),
        )
        assert entity is not None
        assert message_repo.get_by_id("msg_admin") is not None


def test_send_to_tombstone_recipient_succeeds(project_dir, reset_globals):
    """``send`` accepts a tombstone recipient ``[deleted-<id>]``.

    Tombstones live as ``agents`` rows with ``status='tombstone'``
    (seeded by :meth:`AgentRepository.insert_tombstone` on purge).
    Audit messages to / about purged agents are legitimate; only
    *typo'd* nonexistent IDs should be rejected.
    """
    with _make_client(project_dir):
        from agent_mcp.repositories import agent_repo, message_repo

        _seed_agent("alice")
        # Create a tombstone agent row via the standard repo seam.
        agent_repo.insert_tombstone(
            token="__tombstone_old-worker",
            tombstone_agent_id="[deleted-old-worker]",
        )

        entity = message_repo.send(
            message_id="msg_tomb",
            sender_id="alice",
            recipient_id="[deleted-old-worker]",
            message_content="audit",
            message_type="text",
            priority="normal",
            timestamp=datetime.datetime.now().isoformat(),
        )
        assert entity is not None
        assert message_repo.get_by_id("msg_tomb") is not None


def test_send_to_unknown_recipient_raises(project_dir, reset_globals):
    """``send`` raises ``LookupError`` for an unknown recipient_id.

    Repo is the single owner of this invariant — every caller (MCP
    tool, REST, CLI) hits the same check, and the bypass observed in
    VM e2e on 2026-06-16 cannot recur.
    """
    import pytest

    with _make_client(project_dir):
        from agent_mcp.repositories import message_repo

        _seed_agent("alice")

        with pytest.raises(LookupError):
            message_repo.send(
                message_id="msg_unknown",
                sender_id="alice",
                recipient_id="nonexistent-xyz",
                message_content="oops",
                message_type="text",
                priority="normal",
                timestamp=datetime.datetime.now().isoformat(),
            )

        # And the row was NOT inserted — rejection happens BEFORE
        # any DB write.
        assert message_repo.get_by_id("msg_unknown") is None


def test_send_with_cursor_to_unknown_recipient_raises(
    project_dir, reset_globals,
):
    """The ``connection=`` (sqlite3 cursor) overload of ``send`` must
    enforce the same recipient-exists check. ``send_agent_message_
    tool_impl`` is the production caller; rejecting at the repo seam
    before the INSERT fires is what protects the wider transaction.
    """
    import pytest

    from agent_mcp.db.connection import get_db_connection

    with _make_client(project_dir):
        from agent_mcp.repositories import message_repo

        _seed_agent("alice")

        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("BEGIN")
            with pytest.raises(LookupError):
                message_repo.send(
                    message_id="msg_cursor_unknown",
                    sender_id="alice",
                    recipient_id="nonexistent-xyz",
                    message_content="oops",
                    message_type="text",
                    priority="normal",
                    timestamp=datetime.datetime.now().isoformat(),
                    connection=cursor,
                )
            conn.rollback()
        finally:
            conn.close()

        assert message_repo.get_by_id("msg_cursor_unknown") is None
