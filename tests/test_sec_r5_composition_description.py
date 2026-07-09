"""Round-5: composition REST must redact the context DESCRIPTION too.

The round-4 backstop (#308) taught the three composition reads to redact
on the TWO-part filter ``is_secret_key(key) OR
_value_has_embedded_secret(value, description)`` — where the predicate
scans BOTH the value AND the description. But the endpoints only applied
the verdict to blank the ``value``; the ``description`` shipped VERBATIM.

So a ``project_context`` row with a benign key + benign value + a
credential pasted into the DESCRIPTION: the predicate fires (on the
description), the value is masked (holds nothing), and the description —
the field actually holding the secret — leaked to every viewer-tier /
cookie / forwarding-operator caller (which ``is_confirmed_operator_tier``
cannot verify). The tool boundary (``project_context_tools``) DROPS the
whole row when the predicate matches, so composition was strictly weaker
than the surface its docstring claims to mirror.

This suite pins (SD-R5-1): ``/api/all-data``, ``/api/node-details``, and
``/api/context-data`` redact BOTH ``value`` AND ``description`` for a
non-confirmed-operator caller when the secret lives in the description,
while a confirmed operator bearer still sees both, and an innocent
description is NOT over-redacted.
"""

from __future__ import annotations

import pytest

# Benign KEY (is_secret_key False) + benign VALUE — only the DESCRIPTION
# carries the credential, so the leak can only come from the description.
_BENIGN_KEY = "deploy_notes_sectest"
_BENIGN_VALUE = "see runbook"
# A GitHub PAT pasted into the description field.
_SECRET_IN_DESC = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789xx"
_DESC_WITH_SECRET = f"Deploy creds: {_SECRET_IN_DESC} — do not share."

# Benign KEY + benign VALUE + innocent DESCRIPTION — must never redact.
_INNOCENT_KEY = "project_readme_r5"
_INNOCENT_VALUE = "public info r5"
_INNOCENT_DESC = "public runbook description r5"

_REDACTED = "[redacted]"


def _seed(admin, *, key: str, value: str, description: str) -> None:
    r = admin.client.post(
        "/api/memories",
        json={
            "token": admin.admin_token,
            "context_key": key,
            "context_value": value,
            "description": description,
        },
    )
    assert r.status_code == 200, r.text


def _bearer(admin) -> dict[str, str]:
    """Confirmed operator-tier auth (per-agent manager bearer)."""
    return {"Authorization": f"Bearer {admin.admin_token}"}


def _assert_no_secret(text: str) -> None:
    assert _SECRET_IN_DESC not in text, "secret pasted into description leaked"


def test_predicate_fires_only_on_description() -> None:
    """Sanity: neither the KEY nor the VALUE is flagged — only the
    DESCRIPTION trips the filter (otherwise the test proves nothing)."""
    from agent_mcp.tools.project_context_tools import is_secret_key

    assert not is_secret_key(_BENIGN_KEY)
    from agent_mcp.features.rag.indexing import _value_has_embedded_secret

    assert not _value_has_embedded_secret(_BENIGN_VALUE)
    assert _value_has_embedded_secret(_BENIGN_VALUE, _DESC_WITH_SECRET)


from tests.harness import mcp_session  # noqa: E402


@pytest.mark.asyncio
async def test_all_data_redacts_secret_in_description(tmp_path) -> None:
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

        # Non-confirmed operator (forwarding header) → BOTH redacted.
        r = admin.get("/api/all-data")
        assert r.status_code == 200, r.text
        _assert_no_secret(r.text)
        row = _find_ctx(r.json()["context"], _BENIGN_KEY)
        assert row["value"] == _REDACTED
        assert row["description"] == _REDACTED
        # Innocent row not over-redacted.
        innocent = _find_ctx(r.json()["context"], _INNOCENT_KEY)
        assert _INNOCENT_VALUE in innocent["value"]
        assert innocent["description"] == _INNOCENT_DESC

        # Confirmed operator bearer → still sees BOTH.
        r2 = admin.client.get("/api/all-data", headers=_bearer(admin))
        assert r2.status_code == 200, r2.text
        assert _SECRET_IN_DESC in r2.text, (
            "confirmed operator must still receive the raw description"
        )


@pytest.mark.asyncio
async def test_context_data_redacts_secret_in_description(tmp_path) -> None:
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
        _assert_no_secret(r.text)
        row = _find_ctx(r.json(), _BENIGN_KEY)
        assert row["value"] == _REDACTED
        assert row["description"] == _REDACTED
        innocent = _find_ctx(r.json(), _INNOCENT_KEY)
        assert _INNOCENT_VALUE in innocent["value"]
        assert innocent["description"] == _INNOCENT_DESC

        r2 = admin.client.get("/api/context-data", headers=_bearer(admin))
        assert r2.status_code == 200, r2.text
        assert _SECRET_IN_DESC in r2.text, (
            "confirmed operator must still receive the raw description"
        )


@pytest.mark.asyncio
async def test_node_details_redacts_secret_in_description(tmp_path) -> None:
    async with mcp_session(tmp_path) as admin:
        _seed(
            admin,
            key=_BENIGN_KEY,
            value=_BENIGN_VALUE,
            description=_DESC_WITH_SECRET,
        )

        node = f"context_{_BENIGN_KEY}"
        r = admin.get(f"/api/node-details?node_id={node}")
        assert r.status_code == 200, r.text
        _assert_no_secret(r.text)
        data = r.json()["data"]
        assert data["value"] == _REDACTED
        assert data["description"] == _REDACTED

        r2 = admin.client.get(
            f"/api/node-details?node_id={node}", headers=_bearer(admin)
        )
        assert r2.status_code == 200, r2.text
        assert _SECRET_IN_DESC in r2.text, (
            "confirmed operator must still see the raw description"
        )


def _find_ctx(rows, key):
    for row in rows:
        if row.get("context_key") == key:
            return row
    raise AssertionError(f"context row {key!r} not found in {rows!r}")
