"""Messages resource router — ``/api/messages/...``.

Wave 8 PR 0 scaffold: the ``APIRouter`` is declared with the right
prefix + router-level ``Depends(require_operator_session)``; no
handlers attached yet. PR 1 mechanically moves the message handlers
out of ``app/routes.py`` (``list_messages``, ``list_participants``,
``create_message``, ``suggest_subject``, ``patch_message``) onto
this router.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ..deps import require_operator_session


router = APIRouter(
    prefix="/api/messages",
    dependencies=[Depends(require_operator_session)],
    tags=["messages"],
)
