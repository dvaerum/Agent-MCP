"""Memories resource router — ``/api/memories/...``.

Wave 8 PR 0 scaffold: the ``APIRouter`` is declared with the right
prefix + router-level ``Depends(require_operator_session)``; no
handlers attached yet. PR 1 mechanically moves the memory handlers
out of ``app/routes.py`` (``context_data``, ``create_memory``,
``update_memory``, ``delete_memory``) onto this router.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ..deps import require_operator_session


router = APIRouter(
    prefix="/api/memories",
    dependencies=[Depends(require_operator_session)],
    tags=["memories"],
)
