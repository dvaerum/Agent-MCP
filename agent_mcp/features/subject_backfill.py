# Agent-MCP/agent_mcp/features/subject_backfill.py
"""Deferred, batched backfill of NULL message subjects (Phase 2).

Phase 1 (PR #554) moved subject generation OFF the synchronous send path:
a root message sent without an explicit subject stores ``subject = NULL``
(the marker for "no real subject set"), and read paths compute a 50-char
body preview + a ``subject_is_placeholder`` flag on the fly. Phase 2 is the
asynchronous other half — a background sweep that titles the NULL-subject
backlog *later*, so the (RAM-hungry, socket-activated + idle-stopped
llama-cpp) model work is decoupled from message sends and BATCHED: one model
activation titles many messages, amortising the load/offload cost.

How it works
------------
``backfill_null_subjects(batch_limit)`` runs one sweep:

* Fetch up to ``batch_limit`` ROOT messages needing a subject
  (``parent_message_id IS NULL AND subject IS NULL``), oldest-first — the
  reply carve-out is enforced in the repo query, replies are never titled.
* For each, ``await suggest_subject(content)``:
    - non-empty subject -> ``set_message_subject`` (NULL -> real).
    - ``None`` (model unavailable / failed) -> leave NULL, retried next sweep.

``run_subject_backfill_periodically(interval_seconds)`` is the long-running
loop (mirrors ``message_retention.run_message_retention_periodically``): it
waits for ``startup_complete_event``, then sweeps every ``interval_seconds``
(default ~2 min) sleeping in ≤60s slices so it reacts to shutdown promptly.
The interval delay IS the "deferred ~2 min" behaviour: a message gets a real
subject within ~2 min of the next sweep.

Async / thread interleave
-------------------------
``suggest_subject`` is an awaitable (an HTTP call to the local model), so it
runs on the event loop. The raw-sqlite fetch/update run via
``asyncio.to_thread`` so they don't block the loop — and, crucially, we never
``await`` inside a ``to_thread`` (the model calls stay on the loop, the DB
calls stay on the thread).

Gating
------
Only active when ``AGENT_MCP_SUBJECT_MODEL`` is set — no model configured
means nothing to generate with, so the sweep short-circuits to a no-op and
the lifecycle doesn't even start the loop. ``suggest_subject`` itself also
returns ``None`` when the model is unset; the early check here just avoids a
pointless DB round-trip every sweep.
"""

from __future__ import annotations

import asyncio
import os
from typing import NoReturn

from ..core.config import logger
from ..core import globals as g
from ..repositories import message_repo
from . import message_suggestions


# Loop interval (~2 min). The deferral is the point: model work is batched
# and amortised, not raced against each send. Settable via env for ops/tests.
DEFAULT_INTERVAL_SECONDS = 120

# Per-sweep batch cap. One sweep loads the model once and titles at most this
# many messages, then releases it so the idle-stop can offload. Env-overridable.
DEFAULT_BATCH_LIMIT = 25


def _subject_model_configured() -> bool:
    """True when AGENT_MCP_SUBJECT_MODEL is set (non-empty)."""
    return bool(os.environ.get("AGENT_MCP_SUBJECT_MODEL", "").strip())


def _batch_limit() -> int:
    """Batch cap for the periodic loop (env-overridable, clamped positive)."""
    try:
        limit = int(
            os.environ.get(
                "MCP_SUBJECT_BACKFILL_BATCH_LIMIT", str(DEFAULT_BATCH_LIMIT)
            )
        )
    except (TypeError, ValueError):
        return DEFAULT_BATCH_LIMIT
    return limit if limit > 0 else DEFAULT_BATCH_LIMIT


async def backfill_null_subjects(
    batch_limit: int = DEFAULT_BATCH_LIMIT,
) -> int:
    """Title up to ``batch_limit`` NULL-subject root messages.

    Returns the number of rows titled this sweep. No-op (returns 0) when
    ``AGENT_MCP_SUBJECT_MODEL`` is unset — nothing to generate with.

    The sqlite fetch/update run off the event loop via ``asyncio.to_thread``;
    the awaitable ``suggest_subject`` model calls run ON the loop between them.
    """
    if not _subject_model_configured():
        return 0

    roots = await asyncio.to_thread(
        message_repo.fetch_null_subject_roots, batch_limit
    )
    if not roots:
        return 0

    titled = 0
    for root in roots:
        subject = await message_suggestions.suggest_subject(
            root["message_content"]
        )
        if not subject:
            # Model unavailable / empty completion — leave NULL, retry next
            # sweep. Don't burn the rest of the batch on a dead model.
            continue
        ok = await asyncio.to_thread(
            message_repo.set_message_subject, root["message_id"], subject
        )
        if ok:
            titled += 1
            # Release any held skinny message event: the message now has a
            # real title, so wake the recipient's parked wait_for_events so
            # the event fires promptly instead of on the next poll.
            try:
                g.notify_agent_inbox(root["recipient_id"])
            except Exception:  # pragma: no cover — best-effort wake
                pass

    if titled:
        logger.info(
            "Subject backfill: titled %d/%d null-subject root messages",
            titled, len(roots),
        )
    return titled


async def run_subject_backfill_periodically(
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS, *, task_status=None
) -> NoReturn:
    """Background task: run backfill_null_subjects() every `interval_seconds`.

    Sleeps in ≤60s slices so the task reacts to shutdown / cancel promptly.
    Mirrors ``run_message_retention_periodically``.
    """
    if task_status is not None:
        task_status.started()

    logger.info(
        "Subject backfill sweep started (interval=%ds, batch=%d)",
        interval_seconds, _batch_limit(),
    )

    # Defer the first cycle until lifespan startup finishes — same rationale
    # as the retention pruner and session-registry pruner: every DB-touching
    # bg task shares the same startup contract.
    await g.startup_complete_event.wait()

    while g.server_running:
        try:
            await backfill_null_subjects(_batch_limit())
        except Exception as e:
            logger.error("Subject backfill cycle failed: %s", e, exc_info=True)

        # Sleep in 60-second slices so we honor server_running quickly.
        remaining = interval_seconds
        while remaining > 0 and g.server_running:
            slice_seconds = min(60, remaining)
            await asyncio.sleep(slice_seconds)
            remaining -= slice_seconds
