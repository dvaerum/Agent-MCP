"""Regression: the dashboard MUST NOT auto-terminate "idle" agents.

The dashboard used to run a `setInterval(handleTerminateAgent, 2min)`
loop over the `getIdleAgentsForCleanup` selector — any agent with no
`current_task` and older than 10 minutes was destroyed behind the
operator's back. Production symptom (washing-brothers, 2026-06-04):
worker bearers (`backend-dev`, `ios-app-dev`) authenticated fine
immediately post-restart, then started returning 401 every ~2 minutes
once the agents crossed the 10-minute age threshold and the next
dashboard poll fired.

The fix (PR #117) deletes the `setInterval` cleanup loop entirely.
This test is a structural regression guard: a future PR that
re-introduces the loop (or sneaks in a different `setInterval` →
`handleTerminateAgent` chain) will flip this from green to red.

We can't easily run the React component in this test process — the
test suite is Python-only. Pin the contract by reading the source file
and asserting:

  * The string `'setInterval(' .* handleTerminateAgent'` (loosely)
    is absent.
  * The phrase `Automatic agent cleanup` (the old loop's identifying
    comment) is either absent or replaced by an explicit "removed in
    PR #117" sentinel.

This is the same pin-by-source-grep pattern
`tests/test_router_no_legacy_redirects.py` uses for the URL-redesign
removals.
"""

from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_FILE = (
    REPO_ROOT
    / "agent_mcp"
    / "dashboard"
    / "components"
    / "dashboard"
    / "agents-dashboard.tsx"
)


def test_dashboard_file_exists() -> None:
    """Sanity: refactors that move the file will invalidate the other
    tests' assumptions, so fail loudly + early."""
    assert DASHBOARD_FILE.exists(), (
        f"Expected dashboard component at {DASHBOARD_FILE}; "
        f"either it was moved/renamed (update this test) or the "
        f"repo layout changed."
    )


def test_no_setinterval_auto_terminate_loop() -> None:
    """No `setInterval` may schedule `handleTerminateAgent`.

    The original bug: `setInterval(async () => { ... await
    handleTerminateAgent(agent.agent_id) ... }, 2 * 60 * 1000)`
    inside `agents-dashboard.tsx`. We assert by regex that no
    `setInterval(` call body anywhere in the file references
    `handleTerminateAgent`.

    We tolerate `handleTerminateAgent` everywhere else (it's still
    bound to per-row Terminate buttons and the confirm dialog). What
    we don't tolerate is its appearance INSIDE a `setInterval(...)`
    body.
    """
    source = DASHBOARD_FILE.read_text(encoding="utf-8")

    # Match `setInterval(` followed by anything up to a matching close.
    # tsx doesn't have C-style braces around `setInterval` arg, so we
    # match across newlines until the function arg's closing brace +
    # the `, <interval>)`. Conservative: match `setInterval(` up to
    # the next `)` at the END of a line containing `, <number>` or
    # `, <expression> * <number>`. False-positives here are fine — we
    # OR with the per-line check below.
    si_pattern = re.compile(
        r"setInterval\s*\(.*?handleTerminateAgent",
        re.DOTALL,
    )
    matches = si_pattern.findall(source)
    assert not matches, (
        f"Found {len(matches)} setInterval(...) body referencing "
        f"handleTerminateAgent in {DASHBOARD_FILE.name}. The dashboard "
        f"auto-cleanup loop was deleted in PR #117 (it killed "
        f"long-lived workers like backend-dev / ios-app-dev every "
        f"~2 minutes once they crossed the 10-minute idle threshold, "
        f"surfacing as the worker-auth-401 production incident). "
        f"Termination must be an explicit admin action; do NOT "
        f"re-introduce a timer-driven destructive loop. If a cleanup "
        f"workflow is needed, surface an explicit dashboard button "
        f"with confirmation."
    )


def test_no_legacy_automatic_agent_cleanup_block() -> None:
    """The old `// Automatic agent cleanup - check every 2 minutes`
    comment + its body must be gone.

    A bare `setInterval(...) → handleTerminateAgent` re-introduction
    would already trip the test above; this is the belt-and-suspenders
    check that the EXACT prior block didn't survive a rebase or get
    accidentally restored via a "revert that PR" reflex.
    """
    source = DASHBOARD_FILE.read_text(encoding="utf-8")
    # Match the old comment text. Allow whitespace variation but pin
    # the substantive phrase.
    bad_comment_pattern = re.compile(
        r"//\s*Automatic\s+agent\s+cleanup\s*-\s*check\s+every\s+2\s+minutes",
        re.IGNORECASE,
    )
    matches = bad_comment_pattern.findall(source)
    assert not matches, (
        f"Found the legacy 'Automatic agent cleanup - check every 2 "
        f"minutes' comment block in {DASHBOARD_FILE.name}. The block "
        f"was deleted in PR #117 — see the comment block in the "
        f"current code referencing #117 for context. If this test "
        f"trips because someone restored that loop verbatim, the "
        f"production worker-auth-401 incident is about to re-occur."
    )


def test_pr117_removal_sentinel_present() -> None:
    """The replacement comment must mention PR #117 so a future
    bisect-by-grep lands here without spelunking git blame.

    Future architects will see a deleted-block-shaped hole + the
    sentinel and immediately find the rationale + production-incident
    context in the PR description.
    """
    source = DASHBOARD_FILE.read_text(encoding="utf-8")
    assert "PR #117" in source, (
        f"Expected 'PR #117' sentinel marker in {DASHBOARD_FILE.name} "
        f"near the location where the auto-cleanup loop used to live. "
        f"This sentinel makes future debugging (Why did this go away? "
        f"Should I add it back?) a single grep instead of a git "
        f"archaeology session."
    )
