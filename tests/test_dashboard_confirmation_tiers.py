"""Regression guards for the destructive-action confirmation tiers.

The model (canonical write-up lives in
``agent_mcp/dashboard/components/dashboard/modals/confirm-action-modal.tsx``
— do not restate it here, reference it):

  * tier 0 — no dialog, success toast
  * tier 1 — ``<ConfirmActionModal>``: simple confirm that NAMES the target
  * tier 2 — ``<DeleteConfirmModal>``: type ``DELETE``
  * tier 3 — ``<DeleteConfirmModal requiredWord={name} matchCase>``: type
    the entity's own name/id, case-sensitively

Two properties are worth pinning in text, because both are the kind of
thing a later "let's make this consistent" refactor silently undoes:

1. **Polymorphism.** Users type the username, groups the group name,
   projects the project name, purge the agent id. Four different strings
   means the gesture cannot become a reflex. A uniform ``DELETE`` across
   those pages would be ONE muscle-memory sequence that opens every
   tier-3 gate in the product.
2. **Tier 1 stays cheap.** Memories / Schedules / Terminate / leaf-task
   delete must NOT carry a type-to-confirm gate. Habituation is what
   makes the expensive gates worthless, so the cheap actions have to stay
   cheap for the expensive ones to keep working.

Text-parse guards, per the ``test_dashboard_*`` convention in this repo
(behaviour is covered by the vitest suites next to each component).
"""

from __future__ import annotations

import re
from pathlib import Path

DASHBOARD = Path("agent_mcp/dashboard/components/dashboard")

TIER1_MODAL = DASHBOARD / "modals/confirm-action-modal.tsx"
TIER23_MODAL = DASHBOARD / "modals/delete-confirm-modal.tsx"

# Every page/dialog that must render the TIER-1 simple confirm.
TIER1_CALL_SITES = {
    "memories": DASHBOARD / "memories-dashboard.tsx",
    "schedules": DASHBOARD / "schedules-dashboard.tsx",
    "terminate": DASHBOARD / "agents/terminate-agent-dialog.tsx",
    "task-delete": DASHBOARD / "tasks/delete-task-dialog.tsx",
}

# Tier-3 confirm words, per page: file -> the expression that must be
# passed as ``requiredWord``. Deliberately FOUR DIFFERENT values.
TIER3_REQUIRED_WORDS = {
    DASHBOARD / "agents/purge-agent-dialog.tsx": "agentId",
    DASHBOARD / "users-dashboard.tsx": "username",
    DASHBOARD / "groups-dashboard.tsx": "name",
}


# Comments in these files legitimately DISCUSS the other tier (the tier
# table lives in a docstring), so the audits look at code only.
_BLOCK_COMMENT = re.compile(r"/\*[\s\S]*?\*/")
_LINE_COMMENT = re.compile(r"^\s*//.*$", re.MULTILINE)
_JSX_COMMENT = re.compile(r"\{/\*[\s\S]*?\*/\}")


def _read(p: Path) -> str:
    """File contents with comments stripped."""
    src = p.read_text()
    src = _JSX_COMMENT.sub("", src)
    src = _BLOCK_COMMENT.sub("", src)
    return _LINE_COMMENT.sub("", src)


def test_tier1_modal_has_no_type_to_confirm_gate() -> None:
    """<ConfirmActionModal> is tier 1 BY CONSTRUCTION: if it ever grew a
    confirmation input, every tier-1 call site would silently become
    tier 2 and the habituation argument would collapse."""
    src = _read(TIER1_MODAL)
    assert "requiredWord" not in src, (
        "ConfirmActionModal must not grow a type-to-confirm word — that "
        "is what <DeleteConfirmModal> is for"
    )
    assert "<Input" not in src, (
        "ConfirmActionModal must not render a text input; tier 1 is a "
        "single click"
    )


def test_tier1_call_sites_use_the_shared_modal() -> None:
    """No hand-rolled simple confirms. Tasks / Schedules / Terminate each
    used to carry their own copy of the same {busy, error} +
    Cancel/destructive-confirm state machine (architecture review
    Class 5)."""
    failures = []
    for slug, path in TIER1_CALL_SITES.items():
        src = _read(path)
        if "<ConfirmActionModal" not in src:
            failures.append(f"{slug} ({path}) does not render <ConfirmActionModal>")
    assert not failures, "\n  ".join(["tier-1 call sites drifted:", *failures])


def test_memory_delete_is_tier_1() -> None:
    """Memory delete was DOWNGRADED from type-DELETE to a simple confirm:
    single row, bounded cascade (one RAG source), value visible in the
    modal's details slot, and the keys that are genuinely unrecoverable
    are gated server-side by ``force_delete`` in
    ``project_context_tools.py``. It is also the highest-frequency delete
    in the product, which is exactly where habituation is bought."""
    src = _read(DASHBOARD / "memories-dashboard.tsx")
    assert "<ConfirmActionModal" in src
    assert "DeleteConfirmModal" not in src, (
        "memories must not re-acquire the type-DELETE gate"
    )
    # It must still name the key it is about to delete.
    assert "context_key" in src


def test_task_delete_escalates_only_on_a_real_cascade() -> None:
    """Per-invocation escalation: the tier follows the blast radius of
    THIS click, not the entity type."""
    src = _read(DASHBOARD / "tasks/delete-task-dialog.tsx")
    assert "<ConfirmActionModal" in src and "<DeleteConfirmModal" in src, (
        "the task delete dialog must be able to render BOTH tiers"
    )
    assert "requires_force" in src, (
        "the tier must be chosen from the server's blast-radius preview"
    )
    assert "getTaskDeletePreview" in src


def test_purge_is_tier_3_on_the_agent_id() -> None:
    """Purge has no un-purge endpoint and agent ids are visually
    near-identical (``agent-a959a84c…`` / ``agent-a92d2d9ef…``) while
    terminated rows render interleaved with active ones. Typing
    ``DELETE`` proves intent but not TARGET."""
    src = _read(DASHBOARD / "agents/purge-agent-dialog.tsx")
    assert re.search(r"requiredWord=\{agentId", src), (
        "purge must require the agent id, not a generic word"
    )
    assert re.search(r"^\s*matchCase\s*$", src, re.MULTILINE), (
        "purge's confirm word must be case-sensitive"
    )


def test_tier3_confirm_words_stay_polymorphic() -> None:
    """The four tier-3 gates must demand FOUR DIFFERENT strings.

    This is the test that fails if someone "unifies" them on ``DELETE``.
    """
    words = []
    failures = []
    for path, expected in TIER3_REQUIRED_WORDS.items():
        src = _read(path)
        m = re.search(r"requiredWord=\{([A-Za-z0-9_.?\s]+?)[\}\s]", src)
        if m is None:
            failures.append(f"{path} passes no requiredWord expression")
            continue
        expr = m.group(1).strip()
        if expected not in expr:
            failures.append(
                f"{path} confirms on {expr!r}, expected something derived "
                f"from {expected!r}"
            )
        if re.search(r'requiredWord="DELETE"', src):
            failures.append(
                f"{path} collapsed its confirm word to the uniform "
                f'"DELETE" — see the polymorphism note in this module'
            )
        words.append(expr)
    assert not failures, "\n  ".join(["tier-3 polymorphism broken:", *failures])
    assert len(set(words)) == len(words), (
        f"tier-3 confirm words must all differ, got {words}"
    )
