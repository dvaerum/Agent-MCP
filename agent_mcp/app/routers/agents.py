"""Agents resource router — ``/api/{project}/agents/...``.

Wave 8 PR 0 scaffold: the ``APIRouter`` is declared with the right
prefix + router-level ``Depends(require_operator_session)``; no
handlers attached yet. PR 1 mechanically moves the agent handlers
out of ``app/routes.py`` (``agents_list``, ``register_agent``,
``terminate_agent``, ``restore_agent``, ``edit_agent``,
``purge_preview``, ``purge_agent``) and decorates them on this
router — bodies unchanged except for the per-handler
``Depends(require_operator_session)`` parameter, which the
router-level dependency replaces.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ..deps import require_operator_session


router = APIRouter(
    prefix="/api/{project}/agents",
    dependencies=[Depends(require_operator_session)],
    tags=["agents"],
)
