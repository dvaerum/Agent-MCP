"""Round-4: value-embedded-secret backstop on composition REST endpoints.

Rounds 2-3 wired a TWO-part secret filter into the project_context read
surfaces: redact when ``is_secret_key(key)`` OR
``_value_has_embedded_secret(value, description)``. The value backstop
(a credential pasted into a benign-named key) reached the tool boundary
(``project_context_tools``) and the RAG surfaces, but the three
dashboard REST endpoints in ``composition.py`` still redacted on
``is_secret_key(context_key)`` ONLY.

``is_confirmed_operator_tier`` treats only ``operator_bearer`` as
confirmed, so every cookie-session / signed-forwarding operator (incl. a
read-only viewer-tier member) takes the redaction path yet, absent the
backstop, still received the RAW value. This suite pins:

  (SD-R4-1) ``/api/all-data``, ``/api/node-details``, ``/api/context-data``
    redact a benign-KEYED value that carries an embedded secret for a
    non-confirmed-operator caller, while a confirmed operator bearer
    still sees it, and an innocent value is NOT over-redacted;
  (SD-R4-2) a handler 500 returns a generic message, not raw ``str(e)``.
"""

from __future__ import annotations

import pytest

# A benign KEY name (is_secret_key False) whose VALUE embeds credentials.
_BENIGN_KEY = "deploy_runbook"
# AWS access-key-id + an sk- style token — both trip
# _value_has_embedded_secret via the well-known prefix patterns.
_EMBEDDED_AWS = "AKIAIOSFODNN7EXAMPLE"
_EMBEDDED_SK = "sk-proj-ABCdef0123456789ABCDEFG"
_BENIGN_VALUE = f"Deploy steps: use {_EMBEDDED_AWS} then {_EMBEDDED_SK} to ship."

# A benign KEY with an innocent VALUE — must never be over-redacted.
_INNOCENT_KEY = "project_readme"
_INNOCENT_VALUE = "public-r4-runbook-info"


def _seed(admin, *, key: str, value: str) -> None:
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


def _assert_no_secret(text: str) -> None:
    assert _EMBEDDED_AWS not in text, "embedded AWS key leaked"
    assert _EMBEDDED_SK not in text, "embedded sk- token leaked"


# ── SD-R4-1: value-embedded-secret backstop ──────────────────────────


def test_is_secret_key_false_for_benign_key() -> None:
    """Sanity: the KEY alone is NOT flagged, so only the VALUE backstop
    can catch this row (otherwise the test proves nothing)."""
    from agent_mcp.tools.project_context_tools import is_secret_key

    assert not is_secret_key(_BENIGN_KEY)
    from agent_mcp.features.rag.indexing import _value_has_embedded_secret

    assert _value_has_embedded_secret(_BENIGN_VALUE)


from tests.harness import mcp_session  # noqa: E402


@pytest.mark.asyncio
async def test_all_data_backstops_embedded_secret_value(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        _seed(admin, key=_BENIGN_KEY, value=_BENIGN_VALUE)
        _seed(admin, key=_INNOCENT_KEY, value=_INNOCENT_VALUE)

        # Non-confirmed operator (forwarding header) → value redacted.
        r = admin.get("/api/all-data")
        assert r.status_code == 200, r.text
        _assert_no_secret(r.text)
        # Innocent value not over-redacted.
        assert _INNOCENT_VALUE in r.text

        # Confirmed operator bearer → embedded secret still visible.
        r2 = admin.client.get("/api/all-data", headers=_bearer(admin))
        assert r2.status_code == 200, r2.text
        assert _EMBEDDED_AWS in r2.text, (
            "confirmed operator must still receive the raw value"
        )


@pytest.mark.asyncio
async def test_node_details_backstops_embedded_secret_value(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        _seed(admin, key=_BENIGN_KEY, value=_BENIGN_VALUE)

        node = f"context_{_BENIGN_KEY}"
        r = admin.get(f"/api/node-details?node_id={node}")
        assert r.status_code == 200, r.text
        _assert_no_secret(r.text)

        r2 = admin.client.get(
            f"/api/node-details?node_id={node}", headers=_bearer(admin)
        )
        assert r2.status_code == 200, r2.text
        assert _EMBEDDED_AWS in r2.text, (
            "confirmed operator must still see the raw value"
        )


@pytest.mark.asyncio
async def test_context_data_backstops_embedded_secret_value(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        _seed(admin, key=_BENIGN_KEY, value=_BENIGN_VALUE)
        _seed(admin, key=_INNOCENT_KEY, value=_INNOCENT_VALUE)

        r = admin.get("/api/context-data")
        assert r.status_code == 200, r.text
        _assert_no_secret(r.text)
        assert _INNOCENT_VALUE in r.text

        r2 = admin.client.get("/api/context-data", headers=_bearer(admin))
        assert r2.status_code == 200, r2.text
        assert _EMBEDDED_AWS in r2.text, (
            "confirmed operator must still receive the raw value"
        )


# ── SD-R4-2: exception-string disclosure on 500 ──────────────────────


@pytest.mark.asyncio
async def test_node_details_500_hides_exception_string(
    tmp_path, monkeypatch
) -> None:
    """A DB failure must not reflect ``str(e)`` (schema/paths) to the
    client — a generic message is returned and the detail is logged."""
    from agent_mcp.app.routers import composition as comp

    sentinel = "SENTINEL-DB-SCHEMA-/secret/path.db-r4"

    def _boom():
        raise RuntimeError(sentinel)

    async with mcp_session(tmp_path) as admin:
        monkeypatch.setattr(comp, "get_db_connection", _boom)
        r = admin.get("/api/node-details?node_id=context_anything")
        assert r.status_code == 500, r.text
        assert sentinel not in r.text, (
            "raw exception string leaked to the client on 500"
        )
