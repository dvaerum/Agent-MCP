# Agent-MCP/agent_mcp/utils/capability_normalization.py
"""Single source of truth for normalizing agent/task capability labels.

Per the event-coord plan locked-decisions table: capability labels are
free-text strings, lowercase-normalized at write time, dedupe-on-first-
occurrence. The same shape applies to both `agents.capabilities`
(written by the agent-create tool) and `tasks.required_capabilities`
(written by the assign_task / create-task tools).

Normalization happens once at write time — read paths do NOT
re-normalize. Anything stored in the column is by definition already
lower-cased + stripped + deduped. If a legacy row (pre-PR-1) carries
mixed-case capabilities, the data-migration window for that is the
moment the row is rewritten via the edit-agent endpoint (which runs
through this helper).
"""

from __future__ import annotations

from typing import Iterable, List, Optional


def normalize_capabilities(caps: Optional[Iterable[object]]) -> List[str]:
    """Return a stripped + lowercased + deduped list of capabilities.

    Empty / whitespace-only entries are dropped. Non-string entries are
    coerced via `str()` (callers occasionally hand us int / bool because
    JSON deserialisation is loose); coercion happens before strip/lower.

    Order of first occurrence is preserved so dashboards / logs render
    capabilities in the order the operator typed them (less surprising
    than alphabetical sort).
    """
    if caps is None:
        return []

    seen: set[str] = set()
    out: List[str] = []
    for raw in caps:
        # str() handles bool/int/etc. — empty string stays empty.
        s = str(raw).strip().lower()
        if not s:
            continue
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out
