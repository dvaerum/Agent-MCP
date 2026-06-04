"""The dashboard API client MUST NOT retry mutating HTTP methods.

The shared `request<T>()` helper in `agent_mcp/dashboard/lib/api.ts`
implements a transparent retry-on-5xx loop intended to absorb the
~10-15s cold-start latency of a lazily-spawned backend (router proxy
returns 502/503/504 while the UDS comes up). The retry was added in
the Candidate-C refactor (architecture review 2026-06-01) as a
universal wrapper — it does not branch on HTTP method.

The bug: ``request()`` is reused for every API call, including
``createAgent`` (POST /api/agents), ``createTask`` (POST /api/tasks),
``terminateAgent`` (POST /api/terminate-agent), ``editAgent``
(POST /api/agents/<id>/edit), ``updateTask`` (POST), and
``purgeAgent`` (DELETE with body). When the backend processes a
mutation, commits the side-effect, and then crashes/disconnects
returning 502 on the response phase, the retry re-issues the same
mutation. Two real outcomes:

  * createAgent → "agent_id already exists" 4xx (caught) on retry,
    but the first creation succeeded — the user sees an error and
    assumes nothing happened.
  * createTask → creates two tasks with identical title/description
    silently (task_id is server-generated so there's no uniqueness
    collision to catch the retry).
  * sendMessage / mark-as-read etc. — duplicate fan-out events.

The fix: retries are safe ONLY for idempotent reads (GET / HEAD).
Mutations must surface the 5xx to the caller's catch handler so the
operator can see what happened and decide whether to retry manually.

This test pins the contract by source-grep on api.ts (same pattern
as ``test_dashboard_no_auto_cleanup.py`` / ``test_router_no_legacy_redirects.py``).
"""

from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
API_FILE = (
    REPO_ROOT / "agent_mcp" / "dashboard" / "lib" / "api.ts"
)


def test_api_file_exists() -> None:
    """Sanity: a rename of api.ts invalidates the other assertions."""
    assert API_FILE.exists(), (
        f"Expected dashboard API client at {API_FILE}; either it was "
        f"moved/renamed (update this test) or the repo layout changed."
    )


def test_request_retry_loop_gates_on_method() -> None:
    """The retry loop in ``request<T>()`` must check the request method
    before deciding to retry on 5xx.

    We look for the ``for (let attempt = 0; attempt < 3; attempt++)``
    loop body (or its successor — the cap of 3 may change) and assert
    the body references ``method`` somehow. A naive future contributor
    who deletes the method check (returning the universal-retry bug)
    will trip this test.

    Specifically, the retry-eligibility condition should reference
    either the literal string ``'GET'`` (the safe-method allowlist)
    or ``method`` (referencing the request method variable). The
    original buggy implementation checked only ``response.status``,
    no method involvement at all.
    """
    source = API_FILE.read_text(encoding="utf-8")

    # Locate the request<T>() method body. Anchor on the
    # `private async request<T>(` signature; grab until the matching
    # outer `}` at two-space indent (the class member end).
    fn_match = re.search(
        r"private\s+async\s+request<T>\([^)]*\)[^{]*\{(.*?)\n\s{2}\}",
        source,
        re.DOTALL,
    )
    assert fn_match, (
        f"Couldn't locate the `request<T>()` method body in "
        f"{API_FILE.name}; it may have been renamed. Update this test."
    )
    body = fn_match.group(1)

    # The retry loop must exist (we don't want someone to "fix" the
    # bug by deleting the whole retry — the cold-start absorption is
    # still useful for GETs).
    assert "attempt" in body, (
        f"`request<T>()` no longer has an `attempt` retry loop. The "
        f"transparent cold-start retry is intentional for GET — "
        f"removing it would resurface the boundary-level useEffect "
        f"retry loop the Candidate-C refactor replaced. If the loop "
        f"was renamed, update this test; if it was deleted, restore "
        f"it but keep the method gate."
    )

    # The retry-eligibility check must reference the method somehow.
    # We accept either:
    #   (a) the literal 'GET' or "GET" string appearing in the function
    #       body (idempotent-method allowlist), or
    #   (b) a `method` identifier appearing in the body, used in the
    #       retry-eligibility branch.
    # Either way the body must NOT decide retry-eligibility purely
    # from `response.status` without any method involvement (the
    # buggy original).
    has_get_literal = "'GET'" in body or '"GET"' in body
    has_method_ref = re.search(r"\bmethod\b", body) is not None
    assert has_get_literal or has_method_ref, (
        f"The `request<T>()` retry loop in {API_FILE.name} does not "
        f"reference the HTTP method anywhere. Universal-retry-on-5xx "
        f"double-fires mutations (POST createAgent, createTask, "
        f"terminateAgent etc.) when the backend processes the "
        f"mutation and then returns 502 on the response phase. "
        f"Retries must be gated to idempotent methods (GET / HEAD). "
        f"See the docstring for the bug shape."
    )
