"""Unit tests for the consolidated task-ownership predicate.

``agent_mcp.core.task_ownership`` is the single source of truth for "does
this requester own this task" — the rule that used to be reimplemented
independently at 4+ call sites (task_tools.py's write-path gates,
task_comments_tools.py's add-comment gate, task_queries.py's view_tasks
read-scoping, rag/query.py's RAG-retrieval scoping). See
``docs/adr/`` / the refactor PR description for the consolidation
rationale; this file pins the RULE itself so a future edit to any call
site can't silently drift the semantics again.
"""

from agent_mcp.core.task_ownership import can_access_task, is_unassigned, sql_fragment

# --- can_access_task: the baseline exact-match rule --------------------


def test_exact_owner_is_allowed():
    task = {"assigned_to": "agent-a", "created_by": "agent-b"}
    assert can_access_task(
        task, requester_id="agent-a", can_view_all_tasks=False
    )


def test_foreign_owner_is_denied():
    task = {"assigned_to": "agent-a", "created_by": "agent-b"}
    assert not can_access_task(
        task, requester_id="agent-c", can_view_all_tasks=False
    )


def test_unassigned_task_is_denied_by_default():
    task = {"assigned_to": None, "created_by": "agent-b"}
    assert not can_access_task(
        task, requester_id="agent-c", can_view_all_tasks=False
    )


def test_admin_bypass_grants_access_to_a_foreign_task():
    task = {"assigned_to": "agent-a"}
    assert can_access_task(
        task, requester_id="agent-c", can_view_all_tasks=True
    )


def test_admin_bypass_grants_access_even_with_no_requester():
    task = {"assigned_to": "agent-a"}
    assert can_access_task(
        task, requester_id=None, can_view_all_tasks=True
    )


def test_missing_requester_id_is_denied_without_admin():
    task = {"assigned_to": None}
    assert not can_access_task(
        task, requester_id=None, can_view_all_tasks=False
    )


# --- include_created_by widening (task_comments_tools.py's rule) -------


def test_created_by_match_allowed_when_widened():
    task = {"assigned_to": "agent-a", "created_by": "agent-b"}
    assert can_access_task(
        task,
        requester_id="agent-b",
        can_view_all_tasks=False,
        include_created_by=True,
    )


def test_created_by_match_denied_when_not_widened():
    task = {"assigned_to": "agent-a", "created_by": "agent-b"}
    assert not can_access_task(
        task, requester_id="agent-b", can_view_all_tasks=False
    )


def test_neither_assignee_nor_creator_denied_even_when_widened():
    task = {"assigned_to": "agent-a", "created_by": "agent-b"}
    assert not can_access_task(
        task,
        requester_id="agent-c",
        can_view_all_tasks=False,
        include_created_by=True,
    )


# --- include_unassigned widening (the write-path "claim it first" rule) -


def test_unassigned_task_allowed_when_widened():
    for sentinel in (None, ""):
        task = {"assigned_to": sentinel}
        assert can_access_task(
            task,
            requester_id="agent-c",
            can_view_all_tasks=False,
            include_unassigned=True,
        )


def test_foreign_owned_task_still_denied_when_unassigned_widened():
    task = {"assigned_to": "agent-a"}
    assert not can_access_task(
        task,
        requester_id="agent-c",
        can_view_all_tasks=False,
        include_unassigned=True,
    )


def test_whitespace_only_assignee_counts_as_unassigned():
    task = {"assigned_to": "   "}
    assert is_unassigned(task)


def test_real_assignee_is_not_unassigned():
    assert not is_unassigned({"assigned_to": "agent-a"})


# --- include_foreign widening (config_allow_worker_view_foreign_tasks / --
# --- config_allow_worker_comment_foreign_tasks, default-on cross-agent --
# --- task access) -------------------------------------------------------


def test_foreign_owned_task_allowed_when_foreign_widened():
    task = {"assigned_to": "agent-a"}
    assert can_access_task(
        task,
        requester_id="agent-c",
        can_view_all_tasks=False,
        include_foreign=True,
    )


def test_foreign_widening_does_not_grant_the_unassigned_pool():
    # "foreign" means "assigned to someone ELSE" — an unassigned task
    # has no owner at all, so it stays gated behind include_unassigned,
    # not include_foreign (a caller wanting both passes both flags).
    for sentinel in (None, ""):
        task = {"assigned_to": sentinel}
        assert not can_access_task(
            task,
            requester_id="agent-c",
            can_view_all_tasks=False,
            include_foreign=True,
        )


def test_own_task_still_allowed_when_foreign_widened():
    task = {"assigned_to": "agent-a"}
    assert can_access_task(
        task,
        requester_id="agent-a",
        can_view_all_tasks=False,
        include_foreign=True,
    )


def test_sql_fragment_foreign_scopes_to_any_assigned_task():
    frag, params = sql_fragment(
        "agent-a", can_view_all_tasks=False, include_foreign=True
    )
    assert params == []
    assert "assigned_to" in frag


def test_sql_fragment_agrees_with_can_access_task_foreign_widening():
    """Same property check as the exact-match one above, but for
    include_foreign — the SQL fragment used by rag/query.py's
    pre-vector-search task fetch must never disagree with the dict
    predicate used by search_tasks / _drop_unowned_task_chunks.
    """
    import sqlite3

    tasks = [
        {"task_id": "t1", "assigned_to": "agent-a"},
        {"task_id": "t2", "assigned_to": "agent-b"},
        {"task_id": "t3", "assigned_to": None},
        {"task_id": "t4", "assigned_to": ""},
    ]
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE tasks (task_id TEXT, assigned_to TEXT)")
    conn.executemany(
        "INSERT INTO tasks VALUES (?, ?)",
        [(t["task_id"], t["assigned_to"]) for t in tasks],
    )

    for requester_id in ("agent-a", "agent-b", "agent-z", None):
        frag, params = sql_fragment(
            requester_id, can_view_all_tasks=False, include_foreign=True
        )
        rows = conn.execute(
            f"SELECT task_id FROM tasks WHERE 1=1{frag}", params
        ).fetchall()
        sql_visible = {r[0] for r in rows}
        dict_visible = {
            t["task_id"]
            for t in tasks
            if can_access_task(
                t,
                requester_id=requester_id,
                can_view_all_tasks=False,
                include_foreign=True,
            )
        }
        assert sql_visible == dict_visible, (
            f"disagreement for requester_id={requester_id!r}: "
            f"sql={sql_visible} dict={dict_visible}"
        )
    conn.close()


# --- sql_fragment: SQL-layer equivalent for callers that scope at the --
# --- DB layer (rag/query.py) instead of filtering an already-fetched --
# --- dict (task_tools.py, task_comments_tools.py, task_queries.py) ----


def test_sql_fragment_unscoped_for_admin():
    frag, params = sql_fragment("agent-a", can_view_all_tasks=True)
    assert frag == ""
    assert params == []


def test_sql_fragment_scopes_to_requester():
    frag, params = sql_fragment("agent-a", can_view_all_tasks=False)
    assert "assigned_to" in frag
    assert params == ["agent-a"]


def test_sql_fragment_degrades_closed_on_missing_requester():
    # A missing agent_id must scope to an UNSATISFIABLE fragment (never
    # fall through to unscoped, and never to "assigned_to = ''" which
    # would accidentally match a real row whose assigned_to is itself
    # the empty string — see test_sql_fragment_agrees_with_can_access_
    # task_exact_match for the regression this guards).
    frag, params = sql_fragment(None, can_view_all_tasks=False)
    assert params == []
    assert "1=0" in frag


def test_sql_fragment_agrees_with_can_access_task_exact_match():
    """Property check: for the plain exact-match rule (no widening,
    which sql_fragment doesn't support), the SQL fragment and the dict
    predicate must never disagree, across every requester/admin/task
    combination — this is what makes them ONE rule instead of two
    hand-synced copies.
    """
    import sqlite3

    tasks = [
        {"task_id": "t1", "assigned_to": "agent-a"},
        {"task_id": "t2", "assigned_to": "agent-b"},
        {"task_id": "t3", "assigned_to": None},
        {"task_id": "t4", "assigned_to": ""},
    ]
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE tasks (task_id TEXT, assigned_to TEXT)")
    conn.executemany(
        "INSERT INTO tasks VALUES (?, ?)",
        [(t["task_id"], t["assigned_to"]) for t in tasks],
    )

    for requester_id in ("agent-a", "agent-b", "agent-z", None):
        for can_view_all_tasks in (True, False):
            frag, params = sql_fragment(requester_id, can_view_all_tasks)
            rows = conn.execute(
                f"SELECT task_id FROM tasks WHERE 1=1{frag}", params
            ).fetchall()
            sql_visible = {r[0] for r in rows}
            dict_visible = {
                t["task_id"]
                for t in tasks
                if can_access_task(
                    t,
                    requester_id=requester_id,
                    can_view_all_tasks=can_view_all_tasks,
                )
            }
            assert sql_visible == dict_visible, (
                f"disagreement for requester_id={requester_id!r} "
                f"can_view_all_tasks={can_view_all_tasks}: "
                f"sql={sql_visible} dict={dict_visible}"
            )
    conn.close()
