"""Security R3-F2 — the shared RAG embedded-secret detector is a denylist
with FORMAT GAPS that a lowest-privilege worker exploited to exfiltrate
real credentials VERBATIM through ``ask_project_rag``.

FINDING (owner-authorized pentest, live RE_VERIFY, HIGH): every RAG
consumer routes through ONE detector, ``_value_has_embedded_secret``
(``agent_mcp/features/rag/indexing.py``). #463/#467 made secret redaction
"by-construction" — but only as strong as that one regex table. The
pentester found formats the table simply does not match and exfiltrated
two live secrets:

  * a DB connection URL — ``postgres://appuser:Zf7deployPass@db...`` —
    (also ``postgresql://`` / ``mysql://`` / ``redis://``); the detector
    had no URL-embedded-credential pattern.
  * a Stripe secret key — ``sk_live_...`` (underscore); the existing
    regex is ``sk-[A-Za-z0-9_-]{16,}`` (hyphen, OpenAI-style) → MISS.

Plus: base32 TOTP seeds, short (<40 char) hex keys, HTTP basic-auth URLs,
and ``key: value`` credential lines — all slipped the 40-char, must-mix-
letters-and-digits high-entropy guard.

Fix (make the denylist AGGRESSIVE — over-redaction is the safe failure
mode; over-redacting a RAG answer is harmless, leaking a credential is
not): expand the ONE shared detector so round-4 can't trivially find
another format. These tests were RED on the pre-fix denylist (each leaked
format returned False) and are GREEN once the detector over-redacts.
"""

from __future__ import annotations

import datetime

import pytest

from agent_mcp.features.rag import query as query_mod
from agent_mcp.features.rag.indexing import _value_has_embedded_secret
from agent_mcp.features.rag.query import query_rag_system
from tests.harness import mcp_session


# The two live-exfil'd secrets from the RE_VERIFY, plus the sibling
# formats the pentester enumerated as gaps.
_POSTGRES_URL = "postgres://appuser:Zf7deployPass@db.prod.internal:5432/maindb"
_STRIPE_LIVE = "sk_live_51ZfDeployAbCdEfGhIjKl"


# ── (1) the detector must flag EVERY leaked format ───────────────────


@pytest.mark.parametrize(
    "text",
    [
        # DB connection URLs — verbatim live exfil + scheme siblings.
        _POSTGRES_URL,
        "postgresql://u:Sup3rSecret@db.internal:5432/app",
        "mysql://root:hunter2pass@127.0.0.1:3306/prod",
        "redis://:R3disPassw0rd@cache.internal:6379/0",
        # HTTP basic-auth in a URL.
        "clone https://alice:s3cr3tPatValue@github.com/org/repo.git",
        # Stripe & prefixed keys (underscore, not the OpenAI hyphen).
        _STRIPE_LIVE,
        "sk_test_51ZfDeployAbCdEfGhIjKl",
        "pk_live_51ZfDeployAbCdEfGhIjKl",
        "rk_live_51ZfDeployAbCdEfGhIjKl",
        # base32 TOTP seed.
        "JBSWY3DPEHPK3PXP",
        # short (<40 char) hex API key — under the old 40-char floor.
        "9f86d081884c7d659a2feaa0c55ad015",
        # generic key: value credential line.
        "db_password: Tr0ub4dor",
        "API_KEY = A1b2C3d4E5f6",
        "auth_token=abcdef123456",
    ],
)
def test_leaked_formats_are_flagged(text: str) -> None:
    assert _value_has_embedded_secret(text), (
        f"detector missed a live-leaked credential format: {text!r}"
    )


# ── (2) the existing well-known shapes must STILL flag (no regress) ──


@pytest.mark.parametrize(
    "text",
    [
        "here is sk-abcdef0123456789ABCD",  # OpenAI hyphen key
        "ghp_0123456789abcdefghijABCDEFG",  # GitHub PAT
        "AKIAIOSFODNN7EXAMPLE",  # AWS access key id
        "xoxb-1234567890-abcdefghij",  # Slack token
        "eyJhbGciOi.eyJzdWIiOi.SflKxwRJSM",  # JWT
    ],
)
def test_existing_patterns_still_flag(text: str) -> None:
    assert _value_has_embedded_secret(text)


# ── (3) NON-over-redaction sanity — ordinary text stays clean ────────


@pytest.mark.parametrize(
    "text",
    [
        "the quick brown fox jumps",
        "see runbook",
        "https://api.example.com",  # URL with NO userinfo credential
        "https://docs.example.com:8080/guide",  # host:port, no @ userinfo
        "deploydb",  # ordinary 8-char lowercase word
        "Refactor the authentication middleware for testability.",
        "The password is stored in the vault.",  # 'password' w/o key:value
    ],
)
def test_benign_text_not_over_redacted(text: str) -> None:
    assert not _value_has_embedded_secret(text), (
        f"benign text over-redacted (detector too aggressive): {text!r}"
    )


def test_commit_sha_over_redaction_is_tolerated() -> None:
    """A 40-char commit SHA WILL trip the lowered high-entropy floor.
    That over-redaction is ACCEPTED (safe failure mode) — this test pins
    that we tolerate it rather than special-casing hashes back into a leak
    vector. Asserting True documents the deliberate trade-off."""
    assert _value_has_embedded_secret("a" * 8 + "0123456789abcdef" * 2)


def test_none_and_empty_are_safe() -> None:
    assert not _value_has_embedded_secret(None, "")


# ── (4) end-to-end through the assembly seams (query.py) ─────────────


def test_drop_secret_tasks_drops_url_and_stripe_secrets() -> None:
    from agent_mcp.features.rag.query import _drop_secret_tasks

    tasks = [
        {"task_id": "t-benign", "title": "ok", "description": "no secret"},
        {
            "task_id": "t-pg",
            "title": "deploy",
            "description": f"conn string {_POSTGRES_URL}",
        },
        {
            "task_id": "t-stripe",
            "title": f"key {_STRIPE_LIVE}",
            "description": "x",
        },
    ]
    kept = _drop_secret_tasks(tasks)
    assert [t["task_id"] for t in kept] == ["t-benign"]


def test_scrub_secret_parts_drops_url_and_stripe_parts() -> None:
    from agent_mcp.features.rag.query import _scrub_secret_parts

    parts = [
        "--- Section Header ---",
        f"Task: deploy\nDescription: {_POSTGRES_URL}\n",
        f"Retrieved Chunk 1:\nContent:\nstripe key {_STRIPE_LIVE}\n",
        "def ok(): pass",
    ]
    kept = _scrub_secret_parts(parts)
    assert all(_POSTGRES_URL not in p for p in kept)
    assert all(_STRIPE_LIVE not in p for p in kept)
    assert "--- Section Header ---" in kept
    assert "def ok(): pass" in kept


# ── (5) live pipeline — the actual ask_project_rag exfil path ────────


class _CapturingClient:
    provider = "mock"
    model = "mock"

    def __init__(self) -> None:
        self.messages = None

    async def chat(self, messages, temperature: float = 0.4) -> str:
        self.messages = messages
        return "SYNTHESISED-ANSWER"


class _StubEmbedder:
    def embed(self, texts):
        return [[0.0] * 8 for _ in texts]


def _wire_capture(monkeypatch) -> _CapturingClient:
    cap = _CapturingClient()
    monkeypatch.setattr(query_mod, "completion_client", lambda: cap)
    monkeypatch.setattr(query_mod, "embedding_client", lambda: _StubEmbedder())
    monkeypatch.setattr(query_mod, "is_vss_loadable", lambda: False)
    return cap


def _seed_task(*, task_id: str, title: str, description: str) -> None:
    from agent_mcp.db.connection import get_db_connection

    now = datetime.datetime.now().isoformat()
    conn = get_db_connection()
    try:
        conn.execute(
            "INSERT INTO tasks (task_id, title, description, assigned_to, "
            "created_by, status, priority, created_at, updated_at, "
            "parent_task, child_tasks, depends_on_tasks, notes, "
            "required_capabilities) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                title,
                description,
                None,
                "admin",
                "in_progress",
                "medium",
                now,
                now,
                None,
                "[]",
                "[]",
                "[]",
                "[]",
            ),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_ask_project_rag_does_not_leak_postgres_url(
    tmp_path, monkeypatch
) -> None:
    """The live RE_VERIFY path: a task whose description carries a
    ``postgres://`` connection URL must NOT reach the LLM context. A benign
    sibling keyword-matches so the LLM IS invoked (proves FILTERED, not
    merely absent)."""
    async with mcp_session(tmp_path):
        _seed_task(
            task_id="task-pg-leak",
            title="Configure database connection",
            description=f"Use {_POSTGRES_URL} for the database connection.",
        )
        _seed_task(
            task_id="task-db-benign",
            title="Document database connection",
            description="Write the database connection runbook.",
        )
        cap = _wire_capture(monkeypatch)

        await query_rag_system("how do we configure the database connection?")

        assert cap.messages is not None, "LLM never invoked; context empty"
        user = next(
            m["content"] for m in cap.messages if m["role"] == "user"
        )
        assert _POSTGRES_URL not in user, (
            "postgres:// connection URL leaked to the LLM via ask_project_rag"
        )
        assert "task-db-benign" in user, "benign sibling missing — no assembly"


@pytest.mark.asyncio
async def test_ask_project_rag_does_not_leak_stripe_key(
    tmp_path, monkeypatch
) -> None:
    """A Stripe ``sk_live_`` key in a task description must not reach the
    LLM context."""
    async with mcp_session(tmp_path):
        _seed_task(
            task_id="task-stripe-leak",
            title="Configure payment provider",
            description=f"Set the Stripe key to {_STRIPE_LIVE} in config.",
        )
        _seed_task(
            task_id="task-pay-benign",
            title="Document payment provider",
            description="Write the payment provider runbook.",
        )
        cap = _wire_capture(monkeypatch)

        await query_rag_system("how do we configure the payment provider?")

        assert cap.messages is not None, "LLM never invoked; context empty"
        user = next(
            m["content"] for m in cap.messages if m["role"] == "user"
        )
        assert _STRIPE_LIVE not in user, (
            "Stripe sk_live_ key leaked to the LLM via ask_project_rag"
        )
        assert "task-pay-benign" in user, "benign sibling missing — no assembly"
