#!/usr/bin/env bash
# Regression check: the dashboard's TypeScript `lib/` directory must not
# be swept up by a generic build-output `lib/` convention in
# `.gitignore`.
#
# Tech debt history: a bare `lib/` convention bled into the JS subdir
# and required `git add -f`. Three PRs needed the workaround before
# `.gitignore` was fixed with an explicit negation.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$REPO_ROOT"

TARGET="agent_mcp/dashboard/lib/api/index.ts"

TRACKED="$(git ls-files "$TARGET")"
if [ "$TRACKED" != "$TARGET" ]; then
    echo "FAIL: expected $TARGET to be tracked. Check .gitignore for a stray" >&2
    echo "      'lib/' entry that re-shadows the JS dir." >&2
    exit 1
fi

# Also assert a *new* (untracked) file under that directory would not be
# ignored -- this is the workflow-breaking behavior the fix targets, and
# `git ls-files` alone cannot catch it because the existing files were
# force-added (and `git check-ignore` skips tracked files unless
# `--no-index` is passed).
#
# `git check-ignore --verbose` exits 0 whenever a pattern matches -- even
# if that pattern is a negation. Per git-check-ignore(1): "if the pattern
# begins with `!` then it is a negated pattern and matching it means the
# path is NOT excluded." So we accept either no match at all (exit 1) or
# a negation match (output line whose pattern column starts with `!`).
set +e
CHECK_OUTPUT="$(git check-ignore -v --no-index "$TARGET" 2>/dev/null)"
CHECK_STATUS=$?
set -e

if [ "$CHECK_STATUS" -eq 0 ]; then
    # Verbose format: <source>:<linenum>:<pattern><TAB><pathname>
    PATTERN_COL="${CHECK_OUTPUT#*:*:}"
    PATTERN="${PATTERN_COL%%$'\t'*}"
    case "$PATTERN" in
        '!'*) ;; # negation match -- not actually excluded, fine
        *)
            echo "FAIL: $TARGET is matched by a .gitignore rule: $CHECK_OUTPUT" >&2
            echo "      A generic 'lib/' convention is shadowing the dashboard's" >&2
            echo "      JS lib directory; add a negation rule (e.g." >&2
            echo "      '!agent_mcp/dashboard/lib/') to .gitignore." >&2
            exit 1
            ;;
    esac
fi

echo "ok: gitignore-dashboard-lib"
