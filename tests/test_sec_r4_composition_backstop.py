"""ADR-0017 (Wave 12 PR B): composition REST reads return values in FULL.

This was the round-4 value-embedded-secret backstop: the dashboard REST
endpoints redacted a benign-KEYED value that carried an embedded
credential. Wave 12 PR B removes content-based secret detection entirely —
project_context is shared project knowledge, returned AS-IS to any
authorized reader (a non-confirmed-tier operator included). Real secrets
belong in the operator-only, non-RAG project_settings store.

The former "endpoint redacts the embedded-secret value" tests are inverted
here to "endpoint returns the value in full". The exception-hygiene test
(str(e) suppression on a 500) is unrelated and preserved.
"""

from __future__ import annotations

import pytest

# A benign KEY name whose VALUE embeds credential-shaped strings.
_BENIGN_KEY = "deploy_runbook"
_EMBEDDED_AWS = "AKIAIOSFODNN7EXAMPLE"
_EMBEDDED_SK = "sk-proj-ABCdef0123456789ABCDEFG"
_BENIGN_VALUE = f"Deploy steps: use {_EMBEDDED_AWS} then {_EMBEDDED_SK} to ship."

_INNOCENT_KEY = "project_readme"
_INNOCENT_VALUE = "public-r4-runbook-info"


def _seed(admin, *, key: str, value: str) -> None:
    r = admin.post(
        "/api/memories",
        json={
            "context_key": key,
            "context_value": value,
        },
    )
    assert r.status_code == 200, r.text


def _bearer(admin) -> dict[str, str]:
    """Confirmed operator-tier auth (per-agent manager bearer)."""
    return {"Authorization": f"Bearer {admin.admin_token}"}


def _assert_value_present(text: str) -> None:
    """ADR-0017: the embedded-credential-shaped value is returned AS-IS."""
    assert _EMBEDDED_AWS in text, "value must be returned in full"
    assert _EMBEDDED_SK in text, "value must be returned in full"


from tests.harness import mcp_session


@pytest.mark.asyncio
async def test_all_data_returns_embedded_secret_value(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        _seed(admin, key=_BENIGN_KEY, value=_BENIGN_VALUE)
        _seed(admin, key=_INNOCENT_KEY, value=_INNOCENT_VALUE)

        # Non-confirmed operator (forwarding header) — value returned.
        r = admin.get("/api/all-data")
        assert r.status_code == 200, r.text
        _assert_value_present(r.text)
        assert _INNOCENT_VALUE in r.text

        # Confirmed operator bearer → also present.
        r2 = admin.client.get("/api/all-data", headers=_bearer(admin))
        assert r2.status_code == 200, r2.text
        assert _EMBEDDED_AWS in r2.text


@pytest.mark.asyncio
async def test_node_details_returns_embedded_secret_value(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        _seed(admin, key=_BENIGN_KEY, value=_BENIGN_VALUE)

        node = f"context_{_BENIGN_KEY}"
        r = admin.get(f"/api/node-details?node_id={node}")
        assert r.status_code == 200, r.text
        _assert_value_present(r.text)

        r2 = admin.client.get(
            f"/api/node-details?node_id={node}", headers=_bearer(admin)
        )
        assert r2.status_code == 200, r2.text
        assert _EMBEDDED_AWS in r2.text


@pytest.mark.asyncio
async def test_context_data_returns_embedded_secret_value(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        _seed(admin, key=_BENIGN_KEY, value=_BENIGN_VALUE)
        _seed(admin, key=_INNOCENT_KEY, value=_INNOCENT_VALUE)

        r = admin.get("/api/context-data")
        assert r.status_code == 200, r.text
        _assert_value_present(r.text)
        assert _INNOCENT_VALUE in r.text

        r2 = admin.client.get("/api/context-data", headers=_bearer(admin))
        assert r2.status_code == 200, r2.text
        assert _EMBEDDED_AWS in r2.text


# ── SD-R4-2: exception-string disclosure on 500 (unrelated; preserved) ─


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
