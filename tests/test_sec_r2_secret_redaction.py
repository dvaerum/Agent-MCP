"""Round-2 secret-redaction hardening (owner-authorized security review).

The round-1 fix (#285) added a shared ``is_secret_key`` predicate but it
was too narrow: it required a ``config_`` prefix AND a second ``_``
before one of only 5 suffixes. So a whole family of credential keys —
``config_api_key``, ``config_secret``, ``config_token``,
``config_apikey``, ``config_github_pat``, ``config_encryption_key``,
``openai_api_key``, ``db_password`` — all returned NOT-secret and leaked
through the worker-reachable RAG side-channel and the dashboard reads.

This round-2 suite pins:

  (1) the broadened ``is_secret_key`` vocabulary (config_* is always
      secret; non-config keys with a delimited secret-word segment are
      secret) WITHOUT over-redacting innocent keys;
  (2) worker ``view_project_context`` + ``ask_project_rag`` no longer
      echo the newly-covered keys' values;
  (3) ``/api/all-data`` and ``/api/node-details`` redact secret context
      VALUES for non-confirmed-operator callers, while a confirmed
      operator bearer still sees them;
  (4) an embedded-secret VALUE under a non-secret KEY is skipped at
      index time;
  (5) ``view_project_context`` clamps ``max_results`` so ``-1`` can't
      become an unbounded ``LIMIT -1`` full dump.
"""

from __future__ import annotations

import pytest

from agent_mcp.features.rag import query as query_mod
from agent_mcp.features.rag.query import query_rag_system
from tests.harness import make_principal, mcp_session


_SECRET_VALUE = "SENTINEL-R2-SECRET-4c8e"
_PUBLIC_VALUE = "public-r2-info"


def _seed(admin, *, key: str, value: str) -> None:
    """Seed a project_context row.

    Wave 11 (ADR-0016): the memories write path rejects config_* keys
    for every caller, so the config-keyed rows these read-side
    redaction tests pin (legacy pre-cutover shapes) are seeded raw via
    the repository; knowledge keys keep flowing through the REST seam.
    The config branch of the redaction machinery — and these config-key
    assertions — go away in the ADR-0016 follow-up PR.
    """
    if key.lower().startswith("config_"):
        import json as _json

        from agent_mcp.db.connection import get_db_connection
        from agent_mcp.repositories import (
            project_context_repository as _pc_repo,
        )

        conn = get_db_connection()
        try:
            _pc_repo.upsert(
                key,
                _json.dumps(value),
                None,
                description_provided=False,
                actor="admin",
                connection=conn.cursor(),
            )
            conn.commit()
        finally:
            conn.close()
        return
    r = admin.client.post(
        "/api/memories",
        json={
            "token": admin.admin_token,
            "context_key": key,
            "context_value": value,
        },
    )
    assert r.status_code == 200, r.text


def _bearer(admin) -> dict[str, str]:
    """Confirmed operator-tier auth (per-agent manager bearer)."""
    return {"Authorization": f"Bearer {admin.admin_token}"}


# ── (1) broadened is_secret_key vocabulary ───────────────────────────


def test_is_secret_key_covers_round1_gaps() -> None:
    from agent_mcp.tools.project_context_tools import is_secret_key

    # config_* is ALWAYS secret now, regardless of suffix shape.
    for key in (
        "config_api_key",
        "config_secret",
        "config_token",
        "config_apikey",
        "config_github_pat",
        "config_encryption_key",
        "config_foo",  # any config_* — policy/credential namespace
    ):
        assert is_secret_key(key), f"{key} should be secret (config_*)"

    # Non-config credential keys are caught by the delimited secret-word.
    for key in (
        "openai_api_key",
        "db_password",
        "github_pat",
        "session_cookie",
        "jwt_secret",
        "service_bearer",
        "aws_credentials",
        "user_passphrase",
    ):
        assert is_secret_key(key), f"{key} should be secret (secret-word)"


def test_is_secret_key_does_not_over_redact() -> None:
    from agent_mcp.tools.project_context_tools import is_secret_key

    # Innocent keys that merely CONTAIN secret letters must NOT match.
    for key in (
        "monkey",           # ⊅ key (no delimiter)
        "passenger_list",   # ⊅ pass
        "author_name",      # ⊅ auth
        "compatible_mode",  # ⊅ pat
        "project_readme",
        "app.settings.theme",
        "database.connection.timeout",
        "",
    ):
        assert not is_secret_key(key), f"{key} should NOT be secret"


# ── (2) worker read surfaces (tool + RAG) ────────────────────────────


@pytest.mark.asyncio
async def test_worker_view_context_redacts_round1_gap_keys(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        _seed(admin, key="config_api_key", value=_SECRET_VALUE)
        _seed(admin, key="openai_api_key", value=_SECRET_VALUE + "-2")
        _seed(admin, key="project_notes", value=_PUBLIC_VALUE)
        worker = await admin.create_worker("r2-worker")

        text = (await worker.call("view_project_context", {}))[0].text
        assert "config_api_key" not in text
        assert "openai_api_key" not in text
        assert _SECRET_VALUE not in text
        # Non-secret content still visible (no over-filtering).
        assert "project_notes" in text
        assert _PUBLIC_VALUE in text


@pytest.mark.asyncio
async def test_ask_project_rag_redacts_round1_gap_keys(
    tmp_path, monkeypatch
) -> None:
    """The worker-reachable RAG live-context must drop the broadened
    secret keys before the LLM sees them."""

    class _Cap:
        provider = "mock"
        model = "mock"

        def __init__(self) -> None:
            self.messages = None

        async def chat(self, messages, temperature: float = 0.4) -> str:
            self.messages = messages
            return "ANSWER"

    async with mcp_session(tmp_path) as admin:
        _seed(admin, key="config_api_key", value=_SECRET_VALUE)
        _seed(admin, key="openai_api_key", value=_SECRET_VALUE + "-2")
        _seed(admin, key="project_readme", value=_PUBLIC_VALUE)

        cap = _Cap()
        monkeypatch.setattr(query_mod, "completion_client", lambda: cap)
        # vss disabled → the embedding seam is never reached; no need to
        # stub embedding_client (the old get_openai_client guard is gone).
        monkeypatch.setattr(query_mod, "is_vss_loadable", lambda: False)

        await query_rag_system("what api keys does the project use?")

        assert cap.messages is not None, "LLM never invoked"
        user = next(m["content"] for m in cap.messages if m["role"] == "user")
        assert _SECRET_VALUE not in user
        assert "config_api_key" not in user
        assert "openai_api_key" not in user
        assert _PUBLIC_VALUE in user


# ── (3) dashboard composition endpoints ──────────────────────────────


@pytest.mark.asyncio
async def test_all_data_redacts_secret_context_value(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        _seed(admin, key="config_api_key", value=_SECRET_VALUE)
        _seed(admin, key="project_readme", value=_PUBLIC_VALUE)

        # Non-confirmed operator (forwarding header) → redacted.
        r = admin.get("/api/all-data")
        assert r.status_code == 200, r.text
        assert _SECRET_VALUE not in r.text, (
            "secret context value leaked to non-confirmed operator via "
            "/api/all-data"
        )
        assert _PUBLIC_VALUE in r.text  # non-secret still present

        # Confirmed operator bearer → value present.
        r2 = admin.client.get("/api/all-data", headers=_bearer(admin))
        assert r2.status_code == 200, r2.text
        assert _SECRET_VALUE in r2.text, (
            "confirmed operator must still receive the secret value"
        )


@pytest.mark.asyncio
async def test_node_details_redacts_secret_context_value(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        _seed(admin, key="config_api_key", value=_SECRET_VALUE)

        node = "context_config_api_key"
        r = admin.get(f"/api/node-details?node_id={node}")
        assert r.status_code == 200, r.text
        assert _SECRET_VALUE not in r.text, (
            "secret context value leaked to non-confirmed operator via "
            "/api/node-details"
        )

        r2 = admin.client.get(
            f"/api/node-details?node_id={node}", headers=_bearer(admin)
        )
        assert r2.status_code == 200, r2.text
        assert _SECRET_VALUE in r2.text, (
            "confirmed operator must still see the secret context value"
        )


# ── (4) index-time embedded-secret VALUE scan ────────────────────────


def test_value_scanner_flags_embedded_secrets() -> None:
    from agent_mcp.features.rag.indexing import _value_has_embedded_secret

    # Known token prefixes.
    assert _value_has_embedded_secret("here is sk-abcdef0123456789ABCD")
    assert _value_has_embedded_secret("ghp_0123456789abcdefghijABCDEFG")
    assert _value_has_embedded_secret("AKIAIOSFODNN7EXAMPLE")
    # Long high-entropy token (letters + digits).
    assert _value_has_embedded_secret("tok_" + "a1B2c3D4" * 6)
    # Plain prose / short values must NOT trip it.
    assert not _value_has_embedded_secret("the quick brown fox jumps")
    assert not _value_has_embedded_secret("https://api.example.com")
    assert not _value_has_embedded_secret(None, "")


def test_indexer_scans_value_for_embedded_secrets() -> None:
    """The context scan must call the value scanner so a secret pasted
    into a non-secret-named key's VALUE is never embedded. The scan
    lives inside the NoReturn indexer loop (not unit-callable), so guard
    the wiring at the source level (mirrors the round-1 approach)."""
    import inspect

    from agent_mcp.features.rag import indexing as indexing_mod

    src = inspect.getsource(indexing_mod.run_rag_indexing_periodically)
    assert "_value_has_embedded_secret" in src, (
        "index-time value-secret scan was removed from the context scan"
    )


# ── (5) max_results clamp ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_view_context_clamps_negative_max_results(tmp_path) -> None:
    """max_results=-1 would otherwise become SQLite ``LIMIT -1`` (no
    limit → full dump). The tool schema declares ``minimum: 1`` so the
    jsonschema-validating dispatcher rejects it — but that dependency is
    optional. This drives the impl DIRECTLY (the jsonschema-absent path)
    and asserts the in-code clamp caps the result at a single row despite
    three rows existing."""
    from agent_mcp.tools.project_context_tools import (
        view_project_context_tool_impl,
    )

    async with mcp_session(tmp_path) as admin:
        _seed(admin, key="r2_clamp_a", value="aaa")
        _seed(admin, key="r2_clamp_b", value="bbb")
        _seed(admin, key="r2_clamp_c", value="ccc")

        principal = make_principal(
            kind="agent_bearer",
            user_id="op",
            agent_id="admin",
            sysadmin=True,
            project_name="harness",
            project_role="operator",
            agent_role="manager",
            can_wake_loop=False,
            source_token=None,
        )
        result = await view_project_context_tool_impl(
            {"max_results": -1}, principal=principal
        )
        assert result.data["count"] == 1, (
            "max_results=-1 was not clamped to a single-row LIMIT; got "
            f"count={result.data['count']}"
        )
