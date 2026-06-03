# Agent-MCP/agent_mcp/db/models/__init__.py
"""SQLAlchemy ORM models for agent-mcp.

Phase 7a starts with `ProjectContext` only; subsequent phases
(7g–7m per the roadmap) will add the remaining tables one by one.
Until then, the raw-SQL surface in `agent_mcp.db.connection` keeps
serving the unmigrated tables.

Adding a new model:

1. Drop a file in this package (e.g. `agents.py`).
2. Subclass `agent_mcp.db.engine.Base`.
3. Re-export it here so `from agent_mcp.db.models import Foo` works.
4. Generate an Alembic revision under `migrations/versions/`.
"""

from .agent import Agent
from .project_context import ProjectContext
from .task import Task

__all__ = ["Agent", "ProjectContext", "Task"]
