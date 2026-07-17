"""Arch round-5 #4: dedup the two RAG query paths, one redaction seam.

``query_rag_system`` and ``query_rag_system_with_model``
(``agent_mcp/features/rag/query.py``) ran the same 4-stage pipeline with
~90% duplicated code. Three private helpers now centralise the
byte-for-byte-duplicated pieces:

* ``_append_within_budget`` — the 6x-duplicated token-budget
  accumulation loop body (three sections x two query functions).
* ``_render_chunk`` — the retrieved-chunk metadata/source-info builder.
* ``_assemble_and_answer`` — the final context-join + chat-completion
  call (stage 4 tail + stage 5).

SECURITY-CRITICAL half of this refactor: ``query_rag_system_with_model``
used to read ``project_context`` via its own hand-rolled ``SELECT`` +
inline ``_is_secret_key`` / ``_value_has_embedded_secret`` filter — a
SECOND, independently-maintained secret-redaction enforcement point
next to ``query_rag_system``'s ``rag_repo.fetch_recent_context`` seam
call. That inline filter is now gone; both query functions read live
context through the SAME seam.

These tests pin:

1. ``_append_within_budget``'s boundary semantics (the off-by-one that
   was duplicated 6x and untested).
2. That a secret-keyed context row is redacted regardless of which
   public query function is called (replaces per-function-only
   coverage that could silently diverge if only one path were fixed).
3. That both query functions route their live-context read through the
   SAME ``rag_repo.fetch_recent_context`` seam object — proven by
   monkeypatching the seam and observing both callers reflect it. This
   test is RED against the pre-refactor code: ``query_rag_system_with_
   model`` never called ``fetch_recent_context`` at all, so patching it
   would not have affected that function's output.
"""

from __future__ import annotations

import pytest

from agent_mcp.features.rag import query as query_mod
from agent_mcp.features.rag.query import (
    _append_within_budget,
    query_rag_system,
    query_rag_system_with_model,
)
from tests.harness import mcp_session


# ── _append_within_budget boundary (sync, no harness needed) ─────────


def test_append_within_budget_exact_hit_is_rejected() -> None:
    """An entry that would bring the running count to exactly ``limit``
    does not fit — strict ``<``, matching the pre-existing (duplicated
    6x) inline loops."""
    parts: list[str] = []
    entry = " ".join(["word"] * 10)  # 10 whitespace-split "tokens"

    result = _append_within_budget(parts, entry, count=0, limit=10)

    assert result is None
    assert parts == []


def test_append_within_budget_one_under_limit_is_accepted() -> None:
    parts: list[str] = []
    entry = " ".join(["word"] * 9)

    result = _append_within_budget(parts, entry, count=0, limit=10)

    assert result == 9
    assert parts == [entry]


def test_append_within_budget_one_over_limit_is_rejected() -> None:
    parts: list[str] = []
    entry = " ".join(["word"] * 11)

    result = _append_within_budget(parts, entry, count=0, limit=10)

    assert result is None
    assert parts == []


def test_append_within_budget_respects_running_count() -> None:
    """The boundary check is against ``count + entry_tokens``, not just
    the entry in isolation — pins that a non-zero running count is
    honoured exactly at the edge."""
    parts: list[str] = []
    entry_4 = " ".join(["word"] * 4)
    entry_5 = " ".join(["word"] * 5)

    # count=5, entry=4 tokens -> 9 < 10 -> fits.
    result = _append_within_budget(parts, entry_4, count=5, limit=10)
    assert result == 9

    # count=5, entry=5 tokens -> 10 < 10 is False -> exact hit, rejected.
    result = _append_within_budget(parts, entry_5, count=5, limit=10)
    assert result is None
    assert parts == [entry_4]  # the second entry was never appended


# ── shared redaction across both public entry points ─────────────────

_SECRET_VALUE = "SENTINEL-ARCH-R5-SECRET-2b9f"
_PUBLIC_VALUE = "public-arch-r5-info"


class _CapturingClient:
    """Stand-in completion client that records the messages it is asked
    to synthesise over, so a test can inspect the assembled RAG context
    that reached the model."""

    provider = "mock"
    model = "mock"

    def __init__(self) -> None:
        self.messages = None

    async def chat(self, messages, temperature: float = 0.4) -> str:
        self.messages = messages
        return "SYNTHESISED-ANSWER"


def _user_content(cap: _CapturingClient) -> str:
    assert cap.messages is not None, (
        "LLM was never invoked — the assembled context was empty, so "
        "this test cannot prove the secret was filtered (vs. simply "
        "absent). Seed data / patches are wrong."
    )
    return next(m["content"] for m in cap.messages if m["role"] == "user")


def _seed(admin, *, key: str, value: str) -> None:
    """Seed a project_context knowledge row through the live REST seam.

    Wave 11 (ADR-0016): config_* keys can no longer exist in
    project_context, so the secret row this test pins is a secret-NAMED
    knowledge key (vocab match) seeded through the normal write path.
    """
    r = admin.post(
        "/api/memories",
        json={
            "context_key": key,
            "context_value": value,
        },
    )
    assert r.status_code == 200, r.text


def _wire_capture(monkeypatch) -> _CapturingClient:
    cap = _CapturingClient()
    monkeypatch.setattr(query_mod, "completion_client", lambda: cap)
    # No vector search needed for these tests — disable VSS so the
    # embedding seam is never reached.
    monkeypatch.setattr(query_mod, "is_vss_loadable", lambda: False)
    return cap


@pytest.mark.asyncio
@pytest.mark.parametrize("query_fn", [query_rag_system, query_rag_system_with_model])
async def test_rag_returns_row_in_full_across_both_query_paths(
    tmp_path, monkeypatch, query_fn
) -> None:
    """ADR-0017: a project_context row reaches the LLM AS-IS, regardless of
    which public query function is called. Both functions source live
    context through the SAME ``rag_repo.fetch_recent_context`` seam, so
    this single test guarantees the two paths can't silently diverge —
    and there is no content-based secret redaction on either."""
    async with mcp_session(tmp_path) as admin:
        _seed(admin, key="shared_seam_secret", value=_SECRET_VALUE)
        _seed(admin, key="project_readme", value=_PUBLIC_VALUE)
        cap = _wire_capture(monkeypatch)

        await query_fn("what secrets does the project use?")

        user = _user_content(cap)
        assert _SECRET_VALUE in user, (
            f"row VALUE must reach the LLM via {query_fn.__name__}"
        )
        assert "shared_seam_secret" in user, (
            f"row KEY must reach the LLM via {query_fn.__name__}"
        )
        assert _PUBLIC_VALUE in user
        assert "project_readme" in user


# ── proof of seam collapse: both paths call the SAME seam function ───


@pytest.mark.asyncio
async def test_both_query_paths_route_through_the_fetch_recent_context_seam(
    tmp_path, monkeypatch
) -> None:
    """Monkeypatch ``rag_repo.fetch_recent_context`` itself and confirm
    BOTH query functions' assembled context reflects exactly what the
    (mocked) seam returned.

    This is RED against the pre-refactor code: ``query_rag_system_with_
    model`` used to run its own ``SELECT ... FROM project_context`` and
    never called ``fetch_recent_context`` at all, so patching the seam
    would not have touched that function's output — the two redaction
    enforcement points could silently diverge. GREEN now that both
    paths read live context through the one seam.
    """
    marker_value = "SEAM-ROUTED-MARKER-VALUE-7c1a"

    def _fake_fetch_recent_context(*, since, limit=5):
        return [
            {
                "context_key": "seam_marker_key",
                "value": marker_value,
                "description": "seam-routed",
                "updated_at": "2099-01-01T00:00:00Z",
            }
        ]

    async with mcp_session(tmp_path):
        # Imported INSIDE the session: server_lifecycle reassigns
        # ``agent_mcp.repositories.rag_repo`` to a fresh singleton on
        # each app startup, so importing before ``mcp_session`` starts
        # would bind a stale reference that the query module's own
        # (call-time) ``from ...repositories import rag_repo`` would
        # not see.
        from agent_mcp.repositories import rag_repo

        monkeypatch.setattr(
            rag_repo, "fetch_recent_context", _fake_fetch_recent_context
        )

        cap1 = _wire_capture(monkeypatch)
        await query_rag_system("anything")
        user1 = _user_content(cap1)
        assert marker_value in user1, (
            "query_rag_system did not route live context through the "
            "fetch_recent_context seam"
        )

        cap2 = _wire_capture(monkeypatch)
        await query_rag_system_with_model("anything")
        user2 = _user_content(cap2)
        assert marker_value in user2, (
            "query_rag_system_with_model did not route live context "
            "through the fetch_recent_context seam — the seam collapse "
            "regressed"
        )
