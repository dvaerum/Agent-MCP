"""sanitize non-conforming memory keys to the allowed charset

Revision ID: 0017_sanitize_memory_keys
Revises: 0016_move_config_to_project_settings
Create Date: 2026-07-15

Memory keys (``project_context.context_key``) are constrained to
``^[A-Za-z0-9._/-]+$`` — letters, digits, and ``. _ / -`` (see
``string_utils.MEMORY_KEY_RE``). ``/`` is allowed (the namespacing
convention: ``ios/repo``, ``backend-dev/status``) and the REST routes
accept it via ``{context_key:path}``.

The create/update surfaces now REJECT a disallowed key up front, so no new
non-conforming key can be written. This migration cleans up any LEGACY key
(created before the allowlist existed) by replacing each disallowed
character with ``_``.

A scan of every project at authoring time found ZERO non-conforming keys,
so on current data this is a **no-op safety net**. It is written to be
correct if a legacy DB does contain one.

Safety / semantics:

* Idempotent — a re-run sees already-sanitized (now conforming) keys and
  skips them.
* Collision-safe — ``context_key`` is the PRIMARY KEY. If a sanitized key
  would collide with an existing key, a ``_2`` / ``_3`` … suffix is
  appended until unique, so the UPDATE never violates the PK.
* The regex is INLINED (not imported from app code) so this migration's
  behaviour is frozen at authoring time regardless of later app changes.
* Renamed keys are logged. A renamed key's stale RAG vectors (keyed on the
  old ``source_ref``) re-index under the new name on the next indexing
  cycle; given zero current offenders this is not exercised in practice.
* downgrade() is a no-op — the rename is lossy (the original disallowed
  characters are not recoverable), and production migrations are
  forward-only.
"""

from __future__ import annotations

import re
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0017_sanitize_memory_keys"
down_revision: Union[str, None] = "0016_move_config_to_project_settings"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Frozen copy of the allowed charset (mirrors string_utils.MEMORY_KEY_RE).
_ALLOWED_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
_DISALLOWED_RE = re.compile(r"[^A-Za-z0-9._/-]")


def _table_exists(bind, name: str) -> bool:
    inspector = sa.inspect(bind)
    return name in inspector.get_table_names()


def _sanitize_context_keys(bind) -> list[tuple[str, str]]:
    """Rename every non-conforming ``project_context`` key to the allowed
    charset (disallowed chars → ``_``), collision-safe. Returns the list of
    ``(old_key, new_key)`` renames performed (empty when nothing to do).

    Split out from :func:`upgrade` so it is unit-testable against a plain
    sqlite connection without an Alembic op context.
    """
    if not _table_exists(bind, "project_context"):
        return []

    rows = bind.execute(
        sa.text("SELECT context_key FROM project_context")
    ).fetchall()
    existing = {r[0] for r in rows if r[0] is not None}
    renames: list[tuple[str, str]] = []

    for (key,) in rows:
        if key is None or _ALLOWED_RE.match(key):
            continue
        base = _DISALLOWED_RE.sub("_", key)
        new_key = base
        n = 2
        # Avoid colliding with any OTHER existing key (the row's own key is
        # about to be freed, so it is not a real collision).
        while new_key in existing and new_key != key:
            new_key = f"{base}_{n}"
            n += 1
        if new_key == key:  # pragma: no cover — key was already conforming
            continue
        bind.execute(
            sa.text(
                "UPDATE project_context SET context_key = :new "
                "WHERE context_key = :old"
            ),
            {"new": new_key, "old": key},
        )
        existing.discard(key)
        existing.add(new_key)
        renames.append((key, new_key))

    return renames


def upgrade() -> None:
    bind = op.get_bind()
    renames = _sanitize_context_keys(bind)
    for old, new in renames:
        print(f"0017_sanitize_memory_keys: renamed {old!r} -> {new!r}")


def downgrade() -> None:
    # Forward-only: the sanitization is lossy (original disallowed chars are
    # gone). No-op.
    pass
