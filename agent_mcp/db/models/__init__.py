# Agent-MCP/agent_mcp/db/models/__init__.py
"""SQLAlchemy ORM models for agent-mcp.

Post PR-W3 (ORM big-bang) every persistent table in the project DB
has a model here, with two exceptions called out explicitly:

* `rag_embeddings` — sqlite-vec `vec0` virtual table; ORM/DDL via
  `init_database()` only.
* `alembic_version` — managed by Alembic itself.

Importing this module registers every model class against the shared
`Base.metadata`, which is what `init_database()` and the Alembic
env consult to emit DDL.

Adding a new model:

1. Drop a file in this package (e.g. `widgets.py`).
2. Subclass `agent_mcp.db.engine.Base`.
3. Re-export it here so `from agent_mcp.db.models import Widget`
   works and the metadata picks it up at import time.
4. Add a Pydantic mirror in `agent_mcp.db.pydantic_mirrors`.
5. Generate an Alembic revision under `migrations/versions/`.
"""

from .agent import Agent
from .agent_action import AgentAction
from .agent_message import AgentMessage
from .claude_code_session import ClaudeCodeSession
from .file_metadata import FileMetadata
from .mcp_session import McpSession
from .project_context import ProjectContext
from .project_settings import ProjectSettings
from .rag_chunk import RagChunk
from .rag_meta import RagMeta
from .scheduled_directive import ScheduledDirective
from .task import Task
from .task_note import TaskNote

__all__ = [
    "Agent",
    "AgentAction",
    "AgentMessage",
    "ClaudeCodeSession",
    "FileMetadata",
    "McpSession",
    "ProjectContext",
    "ProjectSettings",
    "RagChunk",
    "RagMeta",
    "ScheduledDirective",
    "Task",
    "TaskNote",
]
