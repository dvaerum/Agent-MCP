"""Security R4-F4 — ``ask_project_rag`` must not let a worker read a task
it could not read directly via ``view_tasks``.

FINDING (owner-authorized pentest): the RAG secret-detector
(``_value_has_embedded_secret``) is a denylist that can't catch every
low-entropy / natural-language / non-ASCII credential. The operator ruled
the real bug is that ``query_rag_system`` searched ALL tasks with NO
ownership filter — so a worker could pull content (secrets included) out
of another agent's task it could never see through ``view_tasks``.

Security rule (the contract): **search must not grant access the agent
didn't already have.** ``view_tasks`` (task_tools.py) scopes a
non-``tasks.assign`` caller to ``assigned_to == requesting_agent_id``
(exact match — unassigned/NULL tasks are NOT worker-visible). The RAG
task-retrieval paths must mirror that EXACT rule.

Two task-bearing retrieval paths are scoped:
  1. stage-2 live-task keyword SELECT — add the ownership WHERE clause.
  2. vector-search chunks with ``source_type == "task"`` — drop any whose
     ``source_ref`` (task_id) is not visible to the caller.

``tasks.assign`` holders (operator / manager / sysadmin) keep the
unscoped search. Project-wide sources (context / code / markdown) are NOT
task-owned and stay visible to everyone.

These tests were RED on the pre-fix tree: ``query_rag_system`` took no
caller-scope arguments and searched every task, so Worker B's task leaked
to Worker A through BOTH paths.
"""

from __future__ import annotations

import datetime

import pytest

from agent_mcp.features.rag import query as query_mod
from agent_mcp.features.rag.query import (
    query_rag_system,
    query_rag_system_with_model,
)
from agent_mcp.repositories import get_rag_repo
from agent_mcp.tools.rag_tools import ask_project_rag_tool_impl
from tests.harness import make_principal, mcp_session

# Distinctive natural-language markers — deliberately NOT credential-
# shaped, so the residual secret-detector can't be the thing that drops
# them. Only the OWNERSHIP scope can keep Worker B's marker from Worker A.
_MARKER_A = "ZZMARKER-worker-a-own-plan-ZZ"
_MARKER_B = "ZZMARKER-worker-b-private-plan-ZZ"


class _CapturingClient:
    provider = "mock"
    model = "mock"

    def __init__(self) -> None:
        self.messages = None

    async def chat(self, messages, temperature: float = 0.4) -> str:
        self.messages = messages
        return "SYNTHESISED-ANSWER"


class _StubEmbedder:
    def embed(self, texts):
        return [[0.0] * 8 for _ in texts]


def _user_content(cap: _CapturingClient) -> str:
    assert cap.messages is not None, (
        "LLM was never invoked — the assembled context was empty, so this "
        "test cannot prove the task was filtered (vs. simply absent)."
    )
    return next(m["content"] for m in cap.messages if m["role"] == "user")


def _wire_capture(monkeypatch, *, vss: bool = False) -> _CapturingClient:
    cap = _CapturingClient()
    monkeypatch.setattr(query_mod, "completion_client", lambda: cap)
    monkeypatch.setattr(query_mod, "embedding_client", lambda: _StubEmbedder())
    monkeypatch.setattr(query_mod, "is_vss_loadable", lambda: vss)
    return cap


def _seed_task(
    *, task_id: str, title: str, description: str, assigned_to: str
) -> None:
    from agent_mcp.db.connection import get_db_connection

    now = datetime.datetime.now().isoformat()
    conn = get_db_connection()
    try:
        conn.execute(
            "INSERT INTO tasks (task_id, title, description, assigned_to, "
            "created_by, status, priority, created_at, updated_at, "
            "parent_task, child_tasks, depends_on_tasks, notes, "
            "required_capabilities) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                title,
                description,
                assigned_to,
                "admin",
                "in_progress",
                "medium",
                now,
                now,
                None,
                "[]",
                "[]",
                "[]",
                "[]",
            ),
        )
        conn.commit()
    finally:
        conn.close()


# ── (1) stage-2 live-task keyword search — cross-worker leak ──────────


@pytest.mark.asyncio
async def test_live_task_stage_hides_other_workers_task(
    tmp_path, monkeypatch
) -> None:
    """Worker A queries; Worker B's matching task must NOT reach the LLM,
    while Worker A's OWN matching task still does (feature intact)."""
    async with mcp_session(tmp_path):
        _seed_task(
            task_id="task-a-own",
            title="deploy pipeline alpha",
            description=f"my own plan {_MARKER_A}",
            assigned_to="worker-a",
        )
        _seed_task(
            task_id="task-b-private",
            title="deploy pipeline beta",
            description=f"worker B private {_MARKER_B}",
            assigned_to="worker-b",
        )
        cap = _wire_capture(monkeypatch)

        await query_rag_system(
            "deploy pipeline",
            requesting_agent_id="worker-a",
            can_view_all_tasks=False,
        )

        user = _user_content(cap)
        assert _MARKER_B not in user, (
            "Worker B's task leaked to Worker A via the live-task stage — "
            "search granted access view_tasks would deny (R4-F4)"
        )
        assert _MARKER_A in user, (
            "Worker A's own matching task was over-dropped from the context"
        )


@pytest.mark.asyncio
async def test_live_task_stage_excludes_unassigned_for_worker(
    tmp_path, monkeypatch
) -> None:
    """Mirror view_tasks exactly: an UNASSIGNED (assigned_to IS NULL) task
    is NOT worker-visible, so the RAG live-task stage must exclude it too.
    Worker A's own task keeps the LLM invoked."""
    async with mcp_session(tmp_path):
        _seed_task(
            task_id="task-a-own2",
            title="deploy pipeline gamma",
            description=f"own {_MARKER_A}",
            assigned_to="worker-a",
        )
        # Unassigned task — NULL owner. view_tasks' exact-match filter
        # drops it for a worker; the RAG scope must match.
        from agent_mcp.db.connection import get_db_connection

        now = datetime.datetime.now().isoformat()
        conn = get_db_connection()
        try:
            conn.execute(
                "INSERT INTO tasks (task_id, title, description, assigned_to, "
                "created_by, status, priority, created_at, updated_at, "
                "parent_task, child_tasks, depends_on_tasks, notes, "
                "required_capabilities) VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "task-unassigned",
                    "deploy pipeline delta",
                    f"pool task {_MARKER_B}",
                    None,
                    "admin",
                    "pending",
                    "medium",
                    now,
                    now,
                    None,
                    "[]",
                    "[]",
                    "[]",
                    "[]",
                ),
            )
            conn.commit()
        finally:
            conn.close()
        cap = _wire_capture(monkeypatch)

        await query_rag_system(
            "deploy pipeline",
            requesting_agent_id="worker-a",
            can_view_all_tasks=False,
        )

        user = _user_content(cap)
        assert _MARKER_B not in user, (
            "unassigned-pool task leaked to a worker — view_tasks exact-match "
            "filter would NOT surface it"
        )
        assert _MARKER_A in user


@pytest.mark.asyncio
async def test_live_task_stage_privileged_sees_all(
    tmp_path, monkeypatch
) -> None:
    """A ``tasks.assign`` holder (operator/manager) keeps the unscoped
    search — both workers' tasks are visible (no over-restriction)."""
    async with mcp_session(tmp_path):
        _seed_task(
            task_id="task-a-own3",
            title="deploy pipeline alpha",
            description=f"own {_MARKER_A}",
            assigned_to="worker-a",
        )
        _seed_task(
            task_id="task-b-private3",
            title="deploy pipeline beta",
            description=f"private {_MARKER_B}",
            assigned_to="worker-b",
        )
        cap = _wire_capture(monkeypatch)

        await query_rag_system(
            "deploy pipeline",
            requesting_agent_id="manager-1",
            can_view_all_tasks=True,
        )

        user = _user_content(cap)
        assert _MARKER_A in user
        assert _MARKER_B in user, (
            "a tasks.assign caller was wrongly restricted from another "
            "agent's task"
        )


# ── (2) vector-search task chunks — cross-worker leak ────────────────


def _wire_search(monkeypatch, chunks) -> None:
    """Force the vector path to return crafted chunks so the task-chunk
    ownership filter is exercised deterministically (no real embeddings)."""
    monkeypatch.setattr(
        get_rag_repo(), "search_similar", lambda *a, **k: list(chunks)
    )


@pytest.mark.asyncio
async def test_vector_task_chunk_hides_other_workers_task(
    tmp_path, monkeypatch
) -> None:
    """A ``source_type == 'task'`` chunk for Worker B's task must be
    dropped for Worker A, while Worker A's own task chunk AND a
    project-wide code chunk are kept."""
    async with mcp_session(tmp_path):
        _seed_task(
            task_id="task-a-chunk",
            title="alpha",
            description="own",
            assigned_to="worker-a",
        )
        _seed_task(
            task_id="task-b-chunk",
            title="beta",
            description="private",
            assigned_to="worker-b",
        )
        chunks = [
            {
                "chunk_text": f"Worker B task body {_MARKER_B}",
                "source_type": "task",
                "source_ref": "task-b-chunk",
                "metadata": {},
                "distance": 0.1,
            },
            {
                "chunk_text": f"Worker A task body {_MARKER_A}",
                "source_type": "task",
                "source_ref": "task-a-chunk",
                "metadata": {},
                "distance": 0.2,
            },
            {
                "chunk_text": "def project_helper(): return 42",
                "source_type": "code",
                "source_ref": "helpers.py",
                "metadata": {},
                "distance": 0.3,
            },
        ]
        cap = _wire_capture(monkeypatch, vss=True)
        _wire_search(monkeypatch, chunks)

        await query_rag_system(
            "unrelated keyword query",
            requesting_agent_id="worker-a",
            can_view_all_tasks=False,
        )

        user = _user_content(cap)
        assert _MARKER_B not in user, (
            "Worker B's task CHUNK leaked to Worker A via vector search "
            "(R4-F4)"
        )
        assert _MARKER_A in user, "Worker A's own task chunk was over-dropped"
        assert "project_helper" in user, (
            "a project-wide code chunk was wrongly dropped — only task-source "
            "chunks are ownership-scoped"
        )


@pytest.mark.asyncio
async def test_vector_task_chunk_privileged_sees_all(
    tmp_path, monkeypatch
) -> None:
    """A ``tasks.assign`` caller keeps every task chunk."""
    async with mcp_session(tmp_path):
        _seed_task(
            task_id="task-b-chunk2",
            title="beta",
            description="private",
            assigned_to="worker-b",
        )
        chunks = [
            {
                "chunk_text": f"Worker B task body {_MARKER_B}",
                "source_type": "task",
                "source_ref": "task-b-chunk2",
                "metadata": {},
                "distance": 0.1,
            },
        ]
        cap = _wire_capture(monkeypatch, vss=True)
        _wire_search(monkeypatch, chunks)

        await query_rag_system(
            "unrelated keyword query",
            requesting_agent_id="manager-2",
            can_view_all_tasks=True,
        )

        user = _user_content(cap)
        assert _MARKER_B in user, (
            "a tasks.assign caller was wrongly restricted from a task chunk"
        )


# ── (3) end-to-end through ask_project_rag (principal threading) ─────


@pytest.mark.asyncio
async def test_ask_project_rag_threads_worker_scope(
    tmp_path, monkeypatch
) -> None:
    """The whole point: a Worker A principal calling ``ask_project_rag``
    must NOT get Worker B's task back. Proves rag_tools threads the
    caller's agent_id + tasks.assign into query_rag_system."""
    async with mcp_session(tmp_path):
        _seed_task(
            task_id="task-a-e2e",
            title="deploy pipeline alpha",
            description=f"own {_MARKER_A}",
            assigned_to="worker-a",
        )
        _seed_task(
            task_id="task-b-e2e",
            title="deploy pipeline beta",
            description=f"private {_MARKER_B}",
            assigned_to="worker-b",
        )
        cap = _wire_capture(monkeypatch)
        worker_a = make_principal(
            kind="agent_bearer", agent_id="worker-a", agent_role="worker"
        )
        assert worker_a.has_capability("rag.query")
        assert not worker_a.has_capability("tasks.assign")

        await ask_project_rag_tool_impl(
            {"query": "deploy pipeline"}, principal=worker_a
        )

        user = _user_content(cap)
        assert _MARKER_B not in user, (
            "Worker B's task leaked to Worker A end-to-end through "
            "ask_project_rag (R4-F4)"
        )
        assert _MARKER_A in user


@pytest.mark.asyncio
async def test_ask_project_rag_manager_sees_other_agents_task(
    tmp_path, monkeypatch
) -> None:
    """No over-restriction: a MANAGER principal (agent_bearer whose role
    bundle carries ``tasks.assign``) calling ``ask_project_rag`` still
    surfaces another agent's task — a manager assigns work to workers so
    it legitimately sees all tasks, exactly like ``view_tasks``. Scope is
    keyed PURELY on the ``tasks.assign`` capability, not the role name."""
    async with mcp_session(tmp_path):
        _seed_task(
            task_id="task-b-mgr",
            title="deploy pipeline beta",
            description=f"worker B task {_MARKER_B}",
            assigned_to="worker-b",
        )
        cap = _wire_capture(monkeypatch)
        manager = make_principal(
            kind="agent_bearer", agent_id="manager-1", agent_role="manager"
        )
        assert manager.has_capability("tasks.assign")
        assert manager.has_capability("rag.query")

        await ask_project_rag_tool_impl(
            {"query": "deploy pipeline"}, principal=manager
        )

        user = _user_content(cap)
        assert _MARKER_B in user, (
            "manager's ask_project_rag was wrongly restricted from another "
            "agent's task — tasks.assign holders keep the unscoped search"
        )


# ── (4) pure-unit — the ownership predicate / chunk filter ───────────


# ── (5) R5-F1 — the TWIN entry point query_rag_system_with_model ─────
#
# R4-F4 (#472) scoped query_rag_system (the ask_project_rag entry), but
# its twin RAG entry point query_rag_system_with_model — reached by the
# WORKER create_self_task path via validate_task_placement — took NO
# caller scope and searched ALL pending/in_progress tasks. So a worker
# learned another agent's task metadata (id, title, description) via the
# placement-validator duplicate check. These mirror the query_rag_system
# cases on the twin so the two entry points scope tasks IDENTICALLY.


@pytest.mark.asyncio
async def test_with_model_live_task_stage_hides_other_workers_task(
    tmp_path, monkeypatch
) -> None:
    """RED before R5-F1: query_rag_system_with_model's stage-2 live-task
    SELECT (``WHERE status IN ('pending','in_progress')``) fetched every
    task with no ownership filter. Worker A's placement analysis must NOT
    surface Worker B's task, while Worker A's OWN task stays (feature
    intact — the validator still detects duplicates among the caller's own
    tasks)."""
    async with mcp_session(tmp_path):
        _seed_task(
            task_id="wm-task-a-own",
            title="deploy pipeline alpha",
            description=f"my own plan {_MARKER_A}",
            assigned_to="worker-a",
        )
        _seed_task(
            task_id="wm-task-b-private",
            title="deploy pipeline beta",
            description=f"worker B private {_MARKER_B}",
            assigned_to="worker-b",
        )
        cap = _wire_capture(monkeypatch)

        await query_rag_system_with_model(
            "deploy pipeline",
            requesting_agent_id="worker-a",
            can_view_all_tasks=False,
        )

        user = _user_content(cap)
        assert _MARKER_B not in user, (
            "Worker B's task leaked to Worker A via query_rag_system_with_"
            "model's live-task stage — the twin RAG entry point was unscoped "
            "(R5-F1)"
        )
        assert _MARKER_A in user, (
            "Worker A's own task was over-dropped — the placement validator "
            "must still see the caller's own tasks to detect duplicates"
        )


@pytest.mark.asyncio
async def test_with_model_live_task_stage_privileged_sees_all(
    tmp_path, monkeypatch
) -> None:
    """A ``tasks.assign`` caller keeps the unscoped search through the twin
    entry point too (no over-restriction)."""
    async with mcp_session(tmp_path):
        _seed_task(
            task_id="wm-task-a-own2",
            title="deploy pipeline alpha",
            description=f"own {_MARKER_A}",
            assigned_to="worker-a",
        )
        _seed_task(
            task_id="wm-task-b-private2",
            title="deploy pipeline beta",
            description=f"private {_MARKER_B}",
            assigned_to="worker-b",
        )
        cap = _wire_capture(monkeypatch)

        await query_rag_system_with_model(
            "deploy pipeline",
            requesting_agent_id="manager-1",
            can_view_all_tasks=True,
        )

        user = _user_content(cap)
        assert _MARKER_A in user
        assert _MARKER_B in user, (
            "a tasks.assign caller was wrongly restricted from another "
            "agent's task via query_rag_system_with_model"
        )


@pytest.mark.asyncio
async def test_with_model_vector_task_chunk_hides_other_workers_task(
    tmp_path, monkeypatch
) -> None:
    """The twin's vector-search results must drop a Worker B task chunk for
    Worker A, keeping Worker A's own task chunk and a project-wide code
    chunk."""
    async with mcp_session(tmp_path):
        _seed_task(
            task_id="wm-task-a-chunk",
            title="alpha",
            description="own",
            assigned_to="worker-a",
        )
        _seed_task(
            task_id="wm-task-b-chunk",
            title="beta",
            description="private",
            assigned_to="worker-b",
        )
        chunks = [
            {
                "chunk_text": f"Worker B task body {_MARKER_B}",
                "source_type": "task",
                "source_ref": "wm-task-b-chunk",
                "metadata": {},
                "distance": 0.1,
            },
            {
                "chunk_text": f"Worker A task body {_MARKER_A}",
                "source_type": "task",
                "source_ref": "wm-task-a-chunk",
                "metadata": {},
                "distance": 0.2,
            },
            {
                "chunk_text": "def project_helper(): return 42",
                "source_type": "code",
                "source_ref": "helpers.py",
                "metadata": {},
                "distance": 0.3,
            },
        ]
        cap = _wire_capture(monkeypatch, vss=True)
        _wire_search(monkeypatch, chunks)

        await query_rag_system_with_model(
            "unrelated keyword query",
            requesting_agent_id="worker-a",
            can_view_all_tasks=False,
        )

        user = _user_content(cap)
        assert _MARKER_B not in user, (
            "Worker B's task CHUNK leaked to Worker A via query_rag_system_"
            "with_model vector search (R5-F1)"
        )
        assert _MARKER_A in user, "Worker A's own task chunk was over-dropped"
        assert "project_helper" in user, (
            "a project-wide code chunk was wrongly dropped — only task-source "
            "chunks are ownership-scoped"
        )


@pytest.mark.asyncio
async def test_validate_task_placement_threads_worker_scope(
    tmp_path, monkeypatch
) -> None:
    """End-to-end through the real worker path: validate_task_placement
    (called on ``create_self_task``) must thread the worker's scope into
    query_rag_system_with_model, so another agent's task never reaches the
    assembled placement-analysis context. Assert at the context-assembly
    layer (the LLM prompt) — the in-VM LLM's prose is unreliable."""
    async with mcp_session(tmp_path):
        _seed_task(
            task_id="vp-task-a-own",
            title="deploy pipeline alpha",
            description=f"my own plan {_MARKER_A}",
            assigned_to="worker-a",
        )
        _seed_task(
            task_id="vp-task-b-private",
            title="deploy pipeline beta",
            description=f"worker B private {_MARKER_B}",
            assigned_to="worker-b",
        )
        cap = _wire_capture(monkeypatch)

        from agent_mcp.features.task_placement.validator import (
            validate_task_placement,
        )

        await validate_task_placement(
            title="new deploy task",
            description="a fresh task to place",
            parent_task_id=None,
            depends_on_tasks=None,
            created_by="worker-a",
            can_view_all_tasks=False,
        )

        user = _user_content(cap)
        assert _MARKER_B not in user, (
            "validate_task_placement leaked Worker B's task into Worker A's "
            "placement analysis — the create_self_task RAG path was unscoped "
            "(R5-F1)"
        )
        assert _MARKER_A in user, (
            "the validator over-dropped Worker A's own task — duplicate "
            "detection among the caller's own tasks must stay intact"
        )


@pytest.mark.asyncio
async def test_validate_task_placement_privileged_sees_all(
    tmp_path, monkeypatch
) -> None:
    """A ``tasks.assign`` caller's placement analysis keeps the unscoped
    task view (a supervisor legitimately sees all tasks)."""
    async with mcp_session(tmp_path):
        _seed_task(
            task_id="vp-task-b-mgr",
            title="deploy pipeline beta",
            description=f"worker B task {_MARKER_B}",
            assigned_to="worker-b",
        )
        cap = _wire_capture(monkeypatch)

        from agent_mcp.features.task_placement.validator import (
            validate_task_placement,
        )

        await validate_task_placement(
            title="new deploy task",
            description="a fresh task to place",
            parent_task_id=None,
            depends_on_tasks=None,
            created_by="manager-1",
            can_view_all_tasks=True,
        )

        user = _user_content(cap)
        assert _MARKER_B in user, (
            "a tasks.assign caller's placement analysis was wrongly "
            "restricted from another agent's task"
        )


@pytest.mark.asyncio
async def test_validate_task_placement_scopes_to_caller_not_author(
    tmp_path, monkeypatch
) -> None:
    """R5-F1 decoupling: the ``assign_task`` path authors tasks as
    ``created_by="admin"`` while the real caller may be a fell-through
    worker (Mode 0). The RAG scope MUST key on the caller
    (``requesting_agent_id``), NOT the authorship field — else scoping to
    ``"admin"`` for a non-privileged worker would EXPOSE admin's tasks.
    Here: author="admin", caller=worker-a, unprivileged. Worker A must see
    only its own task, never the admin-owned one."""
    async with mcp_session(tmp_path):
        _seed_task(
            task_id="dc-task-a-own",
            title="deploy pipeline alpha",
            description=f"worker A own {_MARKER_A}",
            assigned_to="worker-a",
        )
        _seed_task(
            task_id="dc-task-admin",
            title="deploy pipeline admin",
            description=f"admin-owned {_MARKER_B}",
            assigned_to="admin",
        )
        cap = _wire_capture(monkeypatch)

        from agent_mcp.features.task_placement.validator import (
            validate_task_placement,
        )

        await validate_task_placement(
            title="new deploy task",
            description="a fresh task to place",
            parent_task_id=None,
            depends_on_tasks=None,
            created_by="admin",
            requesting_agent_id="worker-a",
            can_view_all_tasks=False,
        )

        user = _user_content(cap)
        assert _MARKER_B not in user, (
            "admin-owned task leaked to a fell-through worker — the RAG "
            "scope wrongly keyed on created_by='admin' instead of the real "
            "caller (R5-F1 decoupling regression)"
        )
        assert _MARKER_A in user, (
            "worker A's own task was over-dropped from the placement analysis"
        )


def test_drop_unowned_task_chunks_keeps_non_task_sources(
    tmp_path,
) -> None:
    """The chunk filter only scopes ``source_type == 'task'``; context /
    code / markdown chunks are project-wide and always kept."""
    from agent_mcp.features.rag.query import _drop_unowned_task_chunks

    async def _run():
        async with mcp_session(tmp_path):
            _seed_task(
                task_id="own-1",
                title="t",
                description="d",
                assigned_to="worker-a",
            )
            _seed_task(
                task_id="other-1",
                title="t",
                description="d",
                assigned_to="worker-b",
            )
            from agent_mcp.db.connection import get_db_connection

            conn = get_db_connection()
            try:
                cur = conn.cursor()
                results = [
                    {"source_type": "task", "source_ref": "own-1"},
                    {"source_type": "task", "source_ref": "other-1"},
                    {"source_type": "code", "source_ref": "x.py"},
                    {"source_type": "context", "source_ref": "some_key"},
                ]
                kept = _drop_unowned_task_chunks(
                    results,
                    cursor=cur,
                    requesting_agent_id="worker-a",
                    can_view_all_tasks=False,
                )
                refs = {(r["source_type"], r["source_ref"]) for r in kept}
                assert ("task", "own-1") in refs
                assert ("task", "other-1") not in refs
                assert ("code", "x.py") in refs
                assert ("context", "some_key") in refs
            finally:
                conn.close()

    import asyncio

    asyncio.run(_run())
