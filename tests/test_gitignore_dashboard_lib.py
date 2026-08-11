"""Regression test: the dashboard's TypeScript ``lib/`` directory must
not be swept up by Python's ``lib/`` build-output convention in
``.gitignore``.
"""

from __future__ import annotations

import subprocess


def test_dashboard_lib_is_not_gitignored() -> None:
    """Regression: tech debt — Python's lib/ convention bled into
    the JS subdir and required ``git add -f``. Three PRs needed the
    workaround before we fixed .gitignore."""
    result = subprocess.run(
        ["git", "ls-files", "agent_mcp/dashboard/lib/api/index.ts"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "agent_mcp/dashboard/lib/api/index.ts" in result.stdout, (
        "Expected agent_mcp/dashboard/lib/api/index.ts to be tracked. "
        "Check .gitignore for a stray `lib/` that re-shadows the "
        "JS dir."
    )

    # Also assert a *new* (untracked) file under that directory would
    # not be ignored — this is the workflow-breaking behavior the fix
    # targets, and `git ls-files` alone cannot catch it because the
    # existing files were force-added (and `git check-ignore` skips
    # tracked files unless ``--no-index`` is passed).
    #
    # ``git check-ignore --verbose`` exits 0 whenever a pattern matches
    # — even if that pattern is a negation. Per git-check-ignore(1):
    # "if the pattern begins with `!` then it is a negated pattern and
    # matching it means the path is NOT excluded." So we accept either
    # no match at all (exit 1) or a negation match (output line whose
    # pattern column starts with ``!``).
    check = subprocess.run(
        [
            "git",
            "check-ignore",
            "-v",
            "--no-index",
            "agent_mcp/dashboard/lib/api/index.ts",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if check.returncode == 0:
        # Verbose format: ``<source>:<linenum>:<pattern>\t<pathname>``.
        pattern = check.stdout.split(":", 2)[2].split("\t", 1)[0]
        assert pattern.startswith("!"), (
            "agent_mcp/dashboard/lib/api/index.ts is matched by a "
            f".gitignore rule: {check.stdout.strip()}. The Python "
            "`lib/` convention is shadowing the dashboard's JS lib "
            "directory; add a negation rule (e.g. "
            "`!agent_mcp/dashboard/lib/`) to .gitignore."
        )
