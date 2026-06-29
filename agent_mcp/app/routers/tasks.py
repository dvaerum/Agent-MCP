"""Tasks resource router — ``/api/tasks/...``.

Wave 8 PR 0 scaffold: the ``APIRouter`` is declared with the right
prefix + router-level ``Depends(require_operator_session)``; no
handlers attached yet. PR 1 mechanically moves the task handlers
out of ``app/routes.py`` (``all_tasks``, ``create_task``,
``update_task_details``, ``delete_task``) onto this router.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ..deps import require_operator_session


router = APIRouter(
    prefix="/api/tasks",
    dependencies=[Depends(require_operator_session)],
    tags=["tasks"],
)
