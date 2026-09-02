"""ADR-0017 (Wave 12 PR B): composition REST returns the DESCRIPTION in FULL.

This was the round-5 fix: the composition reads must blank the context
DESCRIPTION too (not just the value) when a credential was pasted into it.
Wave 12 PR B removes content-based secret detection entirely — both the
value AND the description are shared project content, returned AS-IS to any
authorized reader. Real secrets belong in the operator-only, non-RAG
project_settings store.

The former "endpoint redacts value + description" tests are inverted here
to "endpoint returns both in full". The ``is_secret_key`` /
``_value_has_embedded_secret`` sanity unit is deleted (the machinery is
gone).
"""

from __future__ import annotations

import pytest

_BENIGN_KEY = "deploy_notes_sectest"
_BENIGN_VALUE = "see runbook"
# A GitHub PAT pasted into the description field — returned AS-IS now.
_SECRET_IN_DESC = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789xx"
_DESC_WITH_SECRET = f"Deploy creds: {_SECRET_IN_DESC} — do not share."

_INNOCENT_KEY = "project_readme_r5"
_INNOCENT_VALUE = "public info r5"
_INNOCENT_DESC = "public runbook description r5"


def _seed(admin, *, key: str, value: str, description: str) -> None:
    r = admin.post(
        "/api/memories",
        json={
            "context_key": key,
            "context_value": value,
            "description": description,
        },
    )
    assert r.status_code == 200, r.text


def _bearer(admin) -> dict[str, str]:
    """Confirmed operator-tier auth (per-agent manager bearer)."""
    return {"Authorization": f"Bearer {admin.admin_token}"}


def _assert_desc_present(text: str) -> None:
    assert _SECRET_IN_DESC in text, "description must be returned in full"


from tests.harness import mcp_session


@pytest.mark.asyncio
async def test_all_data_returns_secret_in_description(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        _seed(
            admin,
            key=_BENIGN_KEY,
            value=_BENIGN_VALUE,
            description=_DESC_WITH_SECRET,
        )
        _seed(
            admin,
            key=_INNOCENT_KEY,
            value=_INNOCENT_VALUE,
            description=_INNOCENT_DESC,
        )

        # Non-confirmed operator (forwarding header) → value + desc AS-IS.
        r = admin.get("/api/all-data")
        assert r.status_code == 200, r.text
        _assert_desc_present(r.text)
        row = _find_ctx(r.json()["context"], _BENIGN_KEY)
        assert _BENIGN_VALUE in row["value"]
        assert row["description"] == _DESC_WITH_SECRET
        innocent = _find_ctx(r.json()["context"], _INNOCENT_KEY)
        assert _INNOCENT_VALUE in innocent["value"]
        assert innocent["description"] == _INNOCENT_DESC

        # Confirmed operator bearer → also sees both.
        r2 = admin.client.get("/api/all-data", headers=_bearer(admin))
        assert r2.status_code == 200, r2.text
        assert _SECRET_IN_DESC in r2.text


@pytest.mark.asyncio
async def test_context_data_returns_secret_in_description(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        _seed(
            admin,
            key=_BENIGN_KEY,
            value=_BENIGN_VALUE,
            description=_DESC_WITH_SECRET,
        )
        _seed(
            admin,
            key=_INNOCENT_KEY,
            value=_INNOCENT_VALUE,
            description=_INNOCENT_DESC,
        )

        r = admin.get("/api/context-data")
        assert r.status_code == 200, r.text
        _assert_desc_present(r.text)
        row = _find_ctx(r.json(), _BENIGN_KEY)
        assert _BENIGN_VALUE in row["value"]
        assert row["description"] == _DESC_WITH_SECRET
        innocent = _find_ctx(r.json(), _INNOCENT_KEY)
        assert _INNOCENT_VALUE in innocent["value"]
        assert innocent["description"] == _INNOCENT_DESC

        r2 = admin.client.get("/api/context-data", headers=_bearer(admin))
        assert r2.status_code == 200, r2.text
        assert _SECRET_IN_DESC in r2.text


def _find_ctx(rows, key):
    for row in rows:
        if row.get("context_key") == key:
            return row
    raise AssertionError(f"context row {key!r} not found in {rows!r}")
