#!/usr/bin/env bash
# Aggregate entry point: run every check script in this directory
# (except this one) and report a PASS/FAIL line per check. Exits
# non-zero if any check failed.
#
# Deliberately no `-e`: a single check's failure must not stop the
# rest from running -- we want a full report of everything that's
# broken in one pass, not just the first failure.
set -uo pipefail

CHECKS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SELF="$(basename "${BASH_SOURCE[0]}")"

overall_status=0
ran_any=0

for script in "$CHECKS_DIR"/*.sh; do
  name="$(basename "$script")"
  if [ "$name" = "$SELF" ]; then
    continue
  fi
  ran_any=1
  echo "=== running $name ==="
  if bash "$script"; then
    echo "PASS  $name"
  else
    echo "FAIL  $name"
    overall_status=1
  fi
  echo
done

if [ "$ran_any" -eq 0 ]; then
  echo "no check scripts found in $CHECKS_DIR" >&2
  exit 1
fi

exit "$overall_status"
