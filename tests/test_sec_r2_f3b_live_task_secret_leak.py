"""Security R2-F3b — the ``ask_project_rag`` context assembler's SECOND
retrieval path (stage 2: live-task keyword search) must scan for embedded
secrets, and the final assembly seam must scrub them by-construction.

FINDING (owner-authorized pentest, live RE_VERIFY, HIGH): #463 closed the
``rag_chunks`` ingest + ``search_similar`` retrieval paths for embedded
credentials, but ``query_rag_system`` assembles its LLM context from FOUR
stages and the class-sweep missed one:

  1. Fetch Live Context  → ``fetch_recent_context``      (scanned, #463)
  2. Fetch Live Tasks    → raw ``SELECT ... FROM tasks`` (NOT scanned) ← leak
  3. Vector Search       → ``_drop_secret_chunks``       (scanned, #463)
  4. Combine → ``_assemble_and_answer``                   (no final scrub)

Live repro on the rebuilt VM: a task whose description contained
``ghp_R2ReVerify1111closedCHECKzzzz9999`` was returned verbatim by
``ask_project_rag`` — the token flowed through stage 2, bypassing every
seam #463 hardened. ``_value_has_embedded_secret`` DOES match the token;
the stage-2 fetch just never called it.

Fix (root cause + by-construction):
  * ``_drop_secret_tasks`` drops any live-task row whose title/description
    embeds a credential — mirrors stages 1 & 3.
  * ``_scrub_secret_parts`` (called inside ``_assemble_and_answer``) scrubs
    every assembled context part at the single choke-point every stage —
    and any future 5th source — flows through.

These tests were RED on the pre-fix tree (stage-2 tasks were unscanned,
the assembly seam had no scrub, and the new symbols did not exist).
"""

from __future__ import annotations

import datetime

import pytest

from agent_mcp.features.rag import query as query_mod
from agent_mcp.features.rag.query import (
    query_rag_system,
    query_rag_system_with_model,
)
from tests.harness import mcp_session


# The exact token the pentester exfiltrated in the live RE_VERIFY
# (matches the ``gh[pousr]_`` well-known-shape pattern in the scanner).
_GH_SECRET = "ghp_R2ReVerify1111closedCHECKzzzz9999"


class _CapturingClient:
    """Records the messages handed to the model so a test can inspect the
    exact assembled RAG context that reached the LLM."""

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
        "test cannot prove the secret was filtered (vs. simply absent)."
    )
    return next(m["content"] for m in cap.messages if m["role"] == "user")


def _wire_capture(monkeypatch, *, vss: bool = False) -> _CapturingClient:
    cap = _CapturingClient()
    monkeypatch.setattr(query_mod, "completion_client", lambda: cap)
    monkeypatch.setattr(query_mod, "embedding_client", lambda: _StubEmbedder())
    monkeypatch.setattr(query_mod, "is_vss_loadable", lambda: vss)
    return cap


def _seed_task(*, task_id: str, title: str, description: str) -> None:
    """Insert a task row directly so stage-2's keyword LIKE picks it up."""
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
                None,
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


# ── (1) stage-2 live-task leak — the RE_VERIFY re-leak path ───────────


@pytest.mark.asyncio
async def test_live_task_description_secret_dropped(
    tmp_path, monkeypatch
) -> None:
    """A task whose DESCRIPTION embeds a credential must not reach the LLM
    through ``query_rag_system`` stage 2 (live-task keyword search). A
    benign sibling matches the same keywords so the LLM IS invoked — this
    proves the secret was FILTERED, not merely absent."""
    async with mcp_session(tmp_path):
        _seed_task(
            task_id="task-leak-desc",
            title="Configure deployment pipeline",
            description=f"Use the GitHub deploy token {_GH_SECRET} for CI.",
        )
        _seed_task(
            task_id="task-benign-sibling",
            title="Document deployment pipeline",
            description="Write the deployment pipeline runbook.",
        )
        cap = _wire_capture(monkeypatch)

        await query_rag_system("how do we configure the deployment pipeline?")

        user = _user_content(cap)
        assert _GH_SECRET not in user, (
            "secret in a live-task DESCRIPTION leaked to the LLM (stage 2)"
        )
        assert "task-benign-sibling" in user, (
            "benign sibling missing — context assembly did not run as expected"
        )


@pytest.mark.asyncio
async def test_live_task_title_secret_dropped(tmp_path, monkeypatch) -> None:
    """A credential in the task TITLE is filtered too (title is scanned)."""
    async with mcp_session(tmp_path):
        _seed_task(
            task_id="task-leak-title",
            title=f"Rotate deployment credential {_GH_SECRET}",
            description="Follow the standard rotation runbook.",
        )
        _seed_task(
            task_id="task-benign-rotate",
            title="Schedule deployment credential rotation",
            description="Add the rotation to the runbook.",
        )
        cap = _wire_capture(monkeypatch)

        await query_rag_system("what is the deployment credential rotation?")

        user = _user_content(cap)
        assert _GH_SECRET not in user, (
            "secret in a live-task TITLE leaked to the LLM (stage 2)"
        )
        assert "task-benign-rotate" in user


@pytest.mark.asyncio
async def test_benign_task_still_included(tmp_path, monkeypatch) -> None:
    """No over-drop: a benign matching task still flows into the context."""
    async with mcp_session(tmp_path):
        _seed_task(
            task_id="task-benign",
            title="Implement deployment pipeline validation",
            description="Add checks for the deployment pipeline config.",
        )
        cap = _wire_capture(monkeypatch)

        await query_rag_system("how do we validate the deployment pipeline?")

        user = _user_content(cap)
        assert "task-benign" in user, (
            "benign live task was over-dropped from the RAG context"
        )


@pytest.mark.asyncio
async def test_task_analysis_path_drops_secret_task(
    tmp_path, monkeypatch
) -> None:
    """The ``_with_model`` (task-analysis) sibling fetches live tasks the
    same raw way and must apply the same drop. It reads ALL
    pending/in_progress tasks (no keyword filter), so a benign task is
    always present to keep the context non-empty."""
    async with mcp_session(tmp_path):
        _seed_task(
            task_id="task-analysis-leak",
            title="Deploy service",
            description=f"token {_GH_SECRET} goes in the secret manager",
        )
        _seed_task(
            task_id="task-analysis-benign",
            title="Write deploy docs",
            description="Document the deploy steps.",
        )
        cap = _wire_capture(monkeypatch)

        await query_rag_system_with_model("analyse the deploy service tasks")

        user = _user_content(cap)
        assert _GH_SECRET not in user, (
            "secret in a live-task leaked via the task-analysis RAG path"
        )
        assert "task-analysis-benign" in user


# ── (2) pure-filter units — _drop_secret_tasks ───────────────────────


def test_drop_secret_tasks_filters_by_title_and_description():
    from agent_mcp.features.rag.query import _drop_secret_tasks

    tasks = [
        {"task_id": "t1", "title": "benign", "description": "no secret here"},
        {"task_id": "t2", "title": f"leak {_GH_SECRET}", "description": "x"},
        {"task_id": "t3", "title": "ok", "description": f"desc {_GH_SECRET}"},
    ]
    kept = _drop_secret_tasks(tasks)
    assert [t["task_id"] for t in kept] == ["t1"]


def test_drop_secret_tasks_tolerates_missing_fields():
    """A row missing title/description must not raise (defensive)."""
    from agent_mcp.features.rag.query import _drop_secret_tasks

    kept = _drop_secret_tasks([{"task_id": "t1"}])
    assert [t["task_id"] for t in kept] == ["t1"]


# ── (3) assembly-seam scrub — the future-source guarantee ────────────


def test_scrub_secret_parts_drops_secret_bearing_part():
    """The final choke-point drops any assembled part embedding a secret —
    even one that arrived from a source with no upstream filter (the
    by-construction guarantee for a future 5th stage)."""
    from agent_mcp.features.rag.query import _scrub_secret_parts

    parts = [
        "--- Section Header ---",
        f"Task ID: future-source\nDescription: token {_GH_SECRET}\n",
        "Retrieved Chunk 1:\nContent:\ndef ok(): pass\n",
    ]
    kept = _scrub_secret_parts(parts)
    assert all(_GH_SECRET not in p for p in kept)
    assert "--- Section Header ---" in kept
    assert any("def ok()" in p for p in kept)


@pytest.mark.asyncio
async def test_assembly_seam_scrubs_injected_secret_part(monkeypatch):
    """End-to-end at the seam: a secret injected directly into
    ``context_parts`` is scrubbed before the prompt reaches the LLM."""
    from agent_mcp.features.rag.query import _assemble_and_answer

    cap = _CapturingClient()
    monkeypatch.setattr(query_mod, "completion_client", lambda: cap)

    answer = await _assemble_and_answer(
        [
            "--- Header ---",
            f"leaked part with {_GH_SECRET}",
            "benign trailing part",
        ],
        current_token_count=0,
        query_text="q",
        system_prompt="sys",
        answer_instruction="ans",
        log_label="test",
    )
    assert answer == "SYNTHESISED-ANSWER"
    user = _user_content(cap)
    assert _GH_SECRET not in user
    assert "benign trailing part" in user
