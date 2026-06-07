#!/usr/bin/env python3
"""Backfill `agent_messages.subject` for legacy root rows.

Migration 0012 (v5.0.22) introduces `agent_messages.subject` as a
nullable column. Every row that existed before the migration ran
ends up with `subject = NULL`. The live send path computes the
effective subject for new rows (Ollama-suggested or truncated body);
this script does the same retroactively for the backlog.

By design:

* Only **root messages** (`parent_message_id IS NULL`) are touched.
  Replies stay `subject = NULL` per the schema contract.
* Rows that already have a `subject` are skipped. The script is
  idempotent — running it twice changes nothing.
* `AGENT_MCP_SUBJECT_MODEL` gates the Ollama call. When unset, the
  script falls back to the truncated-body shape, matching what the
  live send path does. So a backfill on a host without Ollama
  produces deterministic content[:50] + "..." subjects and you can
  re-run later with Ollama configured to upgrade them (the script
  will skip them because they're non-NULL — re-run with `--force`
  to re-suggest).

Rate limiting
-------------
`--rate-limit N` caps the number of Ollama calls per second
(default 5). Each call sleeps `max(0, 1/N - elapsed)` after the
suggest_subject coroutine returns. The backlog is processed
sequentially — we don't need batched concurrency for the scale of
realistic deployments (washing-brothers has < 10k messages).

Usage
-----
    # Dry-run (no DB writes) against a project DB.
    python scripts/backfill_message_subjects.py \\
        --project washing-brothers --dry-run

    # Real run, 5 Ollama calls/sec ceiling.
    python scripts/backfill_message_subjects.py \\
        --project washing-brothers --rate-limit 5

    # Cap to the first 100 rows (smoke test).
    python scripts/backfill_message_subjects.py \\
        --project washing-brothers --limit 100 --dry-run

Exit codes
----------
* 0 on success (including --dry-run).
* 1 on any DB/IO error or a missing project DB.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Iterable, Optional, Tuple


# Path resolution. ~/.agent-mcp/<project>/.agent/mcp_state.db is the
# canonical layout produced by the agent-mcp daemon. Mirrors the
# resolution in agent_mcp.core.config.get_db_path() but without
# depending on the process environment variable / running daemon.
_DEFAULT_PROJECTS_HOME = Path.home() / ".agent-mcp"


def _resolve_db_path(
    project: str, projects_home: Path = _DEFAULT_PROJECTS_HOME,
) -> Path:
    return projects_home / project / ".agent" / "mcp_state.db"


def _iter_root_rows_without_subject(
    conn: sqlite3.Connection, limit: Optional[int], force: bool,
) -> Iterable[Tuple[str, str]]:
    """Yield (message_id, message_content) for every root row that
    needs (or, with --force, that we want to re-do) a subject."""
    cur = conn.cursor()
    if force:
        sql = (
            "SELECT message_id, message_content FROM agent_messages "
            "WHERE parent_message_id IS NULL "
            "ORDER BY timestamp ASC"
        )
    else:
        sql = (
            "SELECT message_id, message_content FROM agent_messages "
            "WHERE parent_message_id IS NULL AND subject IS NULL "
            "ORDER BY timestamp ASC"
        )
    if limit is not None and limit > 0:
        sql += f" LIMIT {int(limit)}"
    cur.execute(sql)
    for row in cur:
        yield row[0], row[1]


def _fallback_subject(content: str) -> str:
    """Truncated-body fallback. Identical to send_agent_message_tool_impl."""
    if len(content) > 50:
        return content[:50] + "..."
    return content


async def _suggest_or_fallback(content: str) -> Tuple[str, bool]:
    """Return (subject, used_ollama). `used_ollama` lets the summary
    distinguish the two paths."""
    if not os.environ.get("AGENT_MCP_SUBJECT_MODEL", "").strip():
        return _fallback_subject(content), False
    # Local import so the script can run on hosts without the agent_mcp
    # package installed via PYTHONPATH; lazy import yields a cleaner
    # error than an import-time NameError.
    try:
        from agent_mcp.features.message_suggestions import suggest_subject
    except ImportError as e:
        print(
            f"WARNING: agent_mcp.features.message_suggestions unavailable: {e}; "
            "falling back to truncated body.",
            file=sys.stderr,
        )
        return _fallback_subject(content), False

    try:
        suggested = await suggest_subject(content)
    except Exception as e:  # pragma: no cover — defensive
        print(
            f"WARNING: suggest_subject raised {type(e).__name__}: {e}; "
            "falling back to truncated body.",
            file=sys.stderr,
        )
        return _fallback_subject(content), False

    if suggested:
        return suggested, True
    return _fallback_subject(content), False


async def _backfill(
    db_path: Path,
    limit: Optional[int],
    dry_run: bool,
    rate_limit: float,
    force: bool,
) -> int:
    """Returns 0 on success, 1 on error."""
    if not db_path.exists():
        print(f"ERROR: DB not found: {db_path}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = list(_iter_root_rows_without_subject(conn, limit, force))
    except sqlite3.OperationalError as e:
        print(
            f"ERROR: schema query failed (is the DB migrated to 0012?): {e}",
            file=sys.stderr,
        )
        return 1

    total = len(rows)
    if total == 0:
        print("No root messages need a subject.")
        return 0

    print(
        f"Backfilling {total} root message(s) in {db_path} "
        f"(dry_run={dry_run}, rate_limit={rate_limit}/sec, force={force})"
    )

    min_interval = 1.0 / rate_limit if rate_limit > 0 else 0.0
    ollama_count = 0
    fallback_count = 0
    write_count = 0

    cur = conn.cursor()
    for idx, (msg_id, content) in enumerate(rows, start=1):
        started = time.monotonic()
        subject, used_ollama = await _suggest_or_fallback(content or "")
        if used_ollama:
            ollama_count += 1
        else:
            fallback_count += 1

        if dry_run:
            print(f"  [{idx}/{total}] {msg_id}  →  {subject!r}  (dry-run)")
        else:
            cur.execute(
                "UPDATE agent_messages SET subject = ? WHERE message_id = ?",
                (subject, msg_id),
            )
            write_count += 1
            if idx % 50 == 0:
                conn.commit()
                print(f"  [{idx}/{total}] committed batch ending at {msg_id}")

        # Rate-limit on Ollama calls only — fallback rows are CPU-only
        # and shouldn't slow down the script. (The min_interval check
        # also covers the case where Ollama returns nearly instantly
        # and we'd otherwise drown the daemon.)
        if used_ollama and min_interval > 0:
            elapsed = time.monotonic() - started
            sleep_for = min_interval - elapsed
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)

    if not dry_run:
        conn.commit()
        print(f"Wrote {write_count} row(s).")
    print(
        f"Done. ollama_subjects={ollama_count}, "
        f"fallback_subjects={fallback_count}, total={total}"
    )
    conn.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project",
        required=True,
        help="Project name (resolves to ~/.agent-mcp/<project>/.agent/mcp_state.db)",
    )
    parser.add_argument(
        "--db-path",
        default=None,
        help="Override the resolved DB path (rarely needed).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print would-be subjects without writing to the DB.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap the number of root rows processed (default: all).",
    )
    parser.add_argument(
        "--rate-limit",
        type=float,
        default=5.0,
        help="Maximum Ollama calls per second (default 5). 0 = no limit.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Re-suggest subjects even for rows that already have one. "
            "Useful when upgrading a host from no-Ollama (truncated "
            "body) to Ollama-configured."
        ),
    )
    args = parser.parse_args()

    db_path = (
        Path(args.db_path).expanduser().resolve()
        if args.db_path
        else _resolve_db_path(args.project)
    )

    try:
        return asyncio.run(
            _backfill(
                db_path=db_path,
                limit=args.limit,
                dry_run=args.dry_run,
                rate_limit=args.rate_limit,
                force=args.force,
            )
        )
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
