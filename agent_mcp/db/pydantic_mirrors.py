# Agent-MCP/agent_mcp/db/pydantic_mirrors.py
"""Pydantic v2 mirrors of every ORM model (PR-W3, ORM big-bang).

Each Pydantic mirror declares one field per column on the
corresponding SQLAlchemy ORM model, with types chosen to round-trip
cleanly with the column's `python_type`:

* TEXT columns → `str` (nullable → `Optional[str]`)
* INTEGER columns → `int` (nullable → `Optional[int]`)
* BOOLEAN columns → `bool` (nullable → `Optional[bool]`)

The mirrors are deliberately permissive (`model_config =
ConfigDict(extra='ignore')`) so adding a column to the ORM does not
silently break the REST surface — the column is just ignored on
parse until the mirror gets the matching field.

A few columns carry a Python attribute name that differs from the
SQL column name because SQLAlchemy reserves `metadata` on
Declarative bases:

* `FileMetadata.metadata_` → SQL column `metadata`
* `RagChunk.metadata_` → SQL column `metadata`
* `ClaudeCodeSession.metadata_` → SQL column `metadata`

The Pydantic mirrors expose the SQL name — `metadata` — via
`Field(alias=...)` and `populate_by_name=True`, so REST callers see
the canonical column name on the wire while ORM round-tripping
still works.

The `MIRRORS: dict[table_name, mirror_class]` map at the bottom is
what the invariant test in `tests/test_orm_is_source_of_truth.py`
consults and what `scripts/generate_ts_types.py` walks. Keeping the
map in one place means adding a new model means updating exactly
two registries: `db.models.__init__` and `MIRRORS` here.

Why hand-written, not generated:

We considered auto-deriving Pydantic mirrors from SQLAlchemy
columns via a metaclass walk. We chose hand-written for two reasons:

1. The result is easier to read and review (column types are right
   next to the column names, no metaprogramming detour).
2. The four columns named `metadata` and the boolean defaults need
   per-field tweaks the auto-walk would have to special-case
   anyway.

The invariant test enforces parity, so any drift between ORM and
mirror still surfaces in CI.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict

# ---------------------------------------------------------------------------
# Per-table mirrors. Each class:
#
# * declares fields in the same order as the ORM model;
# * uses Optional[T] for every nullable column;
# * uses ConfigDict(extra='ignore') so future ORM columns don't
#   silently break old callers.
# ---------------------------------------------------------------------------


class AgentMirror(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    token: str
    agent_id: str
    created_at: str
    status: str
    current_task: Optional[str] = None
    working_directory: str
    color: Optional[str] = None
    terminated_at: Optional[str] = None
    updated_at: Optional[str] = None
    aoe_session_id: Optional[str] = None
    auto_event_loop: bool = True
    last_event_seen_at: Optional[str] = None
    agent_role: str = "worker"
    # Agent self-service profiles (migration 0018).
    profile: Optional[str] = None
    profile_updated_at: Optional[str] = None
    profile_reviewed_at: Optional[str] = None
    profile_updated_by: Optional[str] = None


class AgentActionMirror(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    action_id: int
    agent_id: str
    action_type: str
    task_id: Optional[str] = None
    timestamp: str
    details: Optional[str] = None


class AgentMessageMirror(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    message_id: str
    sender_id: str
    recipient_id: str
    message_content: str
    message_type: str
    priority: str
    timestamp: str
    delivered: bool
    read: bool
    # v5.0.22 — message threads + subjects (migration 0012).
    subject: Optional[str] = None
    parent_message_id: Optional[str] = None


class ClaudeCodeSessionMirror(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    session_id: str
    pid: int
    parent_pid: int
    first_detected: str
    last_activity: str
    working_directory: Optional[str] = None
    agent_id: Optional[str] = None
    status: Optional[str] = None
    git_commits: Optional[str] = None
    # SQL column name is `metadata`; SQLAlchemy reserves that on
    # Declarative so the ORM attribute is `metadata_`. The Pydantic
    # mirror uses `metadata` directly so REST callers see the
    # canonical wire name.
    metadata: Optional[str] = None


class FileMetadataMirror(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    filepath: str
    metadata: str  # NOT NULL on the column
    last_updated: str
    updated_by: str
    content_hash: Optional[str] = None


class McpSessionMirror(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    session_id: str
    agent_id: str
    opened_at: str
    last_seen_at: str
    bearer_token_hash: str
    alias_used: Optional[str] = None


class ProjectContextMirror(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    context_key: str
    value: str
    description: Optional[str] = None
    created_at: Optional[str] = None
    created_by: Optional[str] = None
    updated_at: str
    updated_by: str


class ProjectSettingsMirror(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    context_key: str
    value: str
    description: Optional[str] = None
    created_at: Optional[str] = None
    created_by: Optional[str] = None
    updated_at: str
    updated_by: str


class RagChunkMirror(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    chunk_id: int
    source_type: str
    source_ref: str
    chunk_text: str
    indexed_at: str
    metadata: Optional[str] = None


class RagMetaMirror(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    meta_key: str
    meta_value: Optional[str] = None


class TaskMirror(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    task_id: str
    title: str
    description: Optional[str] = None
    assigned_to: Optional[str] = None
    created_by: str
    status: str
    priority: str
    created_at: str
    updated_at: str
    parent_task: Optional[str] = None
    child_tasks: Optional[str] = None
    depends_on_tasks: Optional[str] = None
    notes: Optional[str] = None


class TaskNoteMirror(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    note_id: int
    task_id: str
    author: Optional[str] = None
    timestamp: str
    text: str


# ---------------------------------------------------------------------------
# Registry. Tests + the TS generator consult `MIRRORS[table_name]`.
# Keep this map alphabetised by table_name to make diffs reviewable.
# ---------------------------------------------------------------------------


MIRRORS: dict[str, type[BaseModel]] = {
    "agent_actions": AgentActionMirror,
    "agent_messages": AgentMessageMirror,
    "agents": AgentMirror,
    "claude_code_sessions": ClaudeCodeSessionMirror,
    "file_metadata": FileMetadataMirror,
    "mcp_sessions": McpSessionMirror,
    "project_context": ProjectContextMirror,
    "project_settings": ProjectSettingsMirror,
    "rag_chunks": RagChunkMirror,
    "rag_meta": RagMetaMirror,
    "task_notes": TaskNoteMirror,
    "tasks": TaskMirror,
}


__all__ = [
    "AgentActionMirror",
    "AgentMessageMirror",
    "AgentMirror",
    "ClaudeCodeSessionMirror",
    "FileMetadataMirror",
    "McpSessionMirror",
    "MIRRORS",
    "ProjectContextMirror",
    "ProjectSettingsMirror",
    "RagChunkMirror",
    "RagMetaMirror",
    "TaskMirror",
    "TaskNoteMirror",
]
