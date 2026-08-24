"""N6 structural half — one owner for "which agent columns are secret".

Four surfaces withhold agent bearer secrets. They agreed on the *who*
(``core/operator_tier.is_confirmed_operator_tier``) but each restated the
*what* — which columns count as credentials — in its own way:

* ``tools/admin_tools.get_agent_tokens`` overwrote ``token`` with
  ``"***"`` (and knew about ``token`` only);
* ``routers/composition./all-data`` ``pop``'d two hardcoded column names;
* ``routers/composition./node-details`` kept a hand-written safe-column
  allowlist whose comment said "Keep this in sync with the agents model
  when columns change";
* ``routers/settings./api/tokens`` gates the whole endpoint instead.

Four restatements of one fact is the shape that drifts. This module pins
both halves of the fix:

1. **Behaviour is unchanged.** Every per-tier outcome at all four sites
   is asserted here against the exact wire shape, so the consolidation
   cannot quietly widen or narrow what any caller receives. These
   assertions were written against the OLD mechanics and pass unmodified
   against the new ones.
2. **The vocabulary is now derived, once.** The secret set comes from
   ``mapped_column(..., info={"secret": True})`` on the ORM model, and a
   synthetic extra secret column proves every consuming site closes over
   it automatically rather than needing four edits.
"""

from __future__ import annotations

import datetime

import pytest

from agent_mcp.core.agent_secrets import (
    REDACTED_TOKEN,
    agent_secret_columns,
    redact_agent_row,
    strip_agent_secrets,
    without_secret_columns,
)
from tests.harness import make_principal, mcp_session

pytestmark = pytest.mark.asyncio


def _seed_agent(agent_id: str, token: str, *, aoe: str = "deadbeefcafe0000") -> None:
    """INSERT a bare agents row carrying BOTH secret columns."""
    from agent_mcp.db.connection import get_db_connection

    now = datetime.datetime.now().isoformat()
    conn = get_db_connection()
    try:
        conn.cursor().execute(
            "INSERT INTO agents (token, agent_id, created_at, status, "
            "working_directory, color, updated_at, agent_role, "
            "aoe_session_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (token, agent_id, now, "active", "/tmp", "#abc", now,
             "worker", aoe),
        )
        conn.commit()
    finally:
        conn.close()


# ── the shared vocabulary ─────────────────────────────────────────


def test_secret_columns_are_declared_on_the_model() -> None:
    """The secret set is derived from the ORM model, not re-listed.

    RED before N6: no such derivation existed — ``token`` and
    ``aoe_session_id`` were named as literals at three separate sites.
    """
    from agent_mcp.db.models.agent import Agent

    assert agent_secret_columns() == {"token", "aoe_session_id"}
    # ...and that set IS the model's own marking, not a coincidence.
    assert agent_secret_columns() == {
        c.key for c in Agent.__table__.columns if c.info.get("secret")
    }


def test_redact_masks_every_declared_secret_column() -> None:
    """``redact_agent_row`` covers the whole declared set, so a future
    credential column is masked by declaring it — not by remembering to
    edit this function."""
    row = {k: f"secret-{k}" for k in agent_secret_columns()}
    row["agent_id"] = "w1"

    masked = redact_agent_row(row, confirmed_operator_tier=False)

    assert masked["agent_id"] == "w1"  # non-secrets pass through
    for key in agent_secret_columns():
        assert masked[key] == REDACTED_TOKEN, key
        assert key in masked, "keys stay present; only values are masked"
    # Confirmed tier is untouched.
    assert redact_agent_row(row, confirmed_operator_tier=True) == row


def test_strip_drops_every_declared_secret_column() -> None:
    row = {k: f"secret-{k}" for k in agent_secret_columns()}
    row["agent_id"] = "w1"

    assert strip_agent_secrets(row) == {"agent_id": "w1"}


def test_display_allowlist_cannot_carry_a_secret_column() -> None:
    """``/node-details``' projection stays a hand-chosen presentation
    list, but a secret slipped into it is filtered out structurally."""
    from agent_mcp.app.routers.composition import _AGENT_NODE_SAFE_COLUMNS

    assert not set(_AGENT_NODE_SAFE_COLUMNS) & agent_secret_columns()
    assert without_secret_columns(
        ("agent_id", "token", "status", "aoe_session_id")
    ) == ("agent_id", "status")


def test_every_site_consumes_the_shared_declaration() -> None:
    """No site re-derives the secret set from string literals.

    RED before N6: ``composition.py`` popped ``'token'`` /
    ``'aoe_session_id'`` by name and ``admin_tools.py`` assigned
    ``agent_data["token"] = "***"`` directly.
    """
    import pathlib

    import agent_mcp

    root = pathlib.Path(agent_mcp.__file__).resolve().parent
    banned = (
        '.pop(\'token\'', '.pop("token"',
        '.pop(\'aoe_session_id\'', '.pop("aoe_session_id"',
        '["token"] = "***"', "['token'] = '***'",
    )
    offenders = [
        f"{path.relative_to(root.parent)}: {needle}"
        for path in sorted(root.rglob("*.py"))
        for needle in banned
        if needle in path.read_text(encoding="utf-8")
    ]
    assert offenders == [], (
        "an agent-secret column is being withheld by name again: "
        + ", ".join(offenders)
        + ". Use core.agent_secrets so the model's declaration stays the "
        "single source of truth."
    )


# ── site A: MCP get_agent_tokens (mask) ───────────────────────────


async def test_get_agent_tokens_masks_for_non_confirmed_tier(tmp_path) -> None:
    """A caller who is not confirmed operator tier sees ``"***"`` — the
    key is present, the value is fully masked."""
    from agent_mcp.tools.admin_tools import get_agent_tokens_tool_impl

    async with mcp_session(tmp_path) as admin:
        worker = await admin.create_worker("w1")
        # A VIEWER who nonetheless carries ``agents.register`` — e.g. via
        # a group grant. They pass the coarse cap gate; the masking below
        # is the second, defence-in-depth layer that still withholds.
        viewer = make_principal(
            kind="operator_session",
            user_id="v1",
            project_role="viewer",
            capabilities=frozenset({"agents.register"}),
        )
        result = await get_agent_tokens_tool_impl(
            {"include_sensitive_data": True}, principal=viewer,
        )
        rows = result.data["agents"]
        assert rows, "expected at least the seeded worker"
        assert all(r["token"] == REDACTED_TOKEN for r in rows)
        assert worker.token not in result.message
        assert result.data["filters_applied"]["include_sensitive_data"] is False


async def test_get_agent_tokens_confirmed_tier_needs_explicit_optin(
    tmp_path,
) -> None:
    """Confirmed operator tier is necessary but NOT sufficient — the
    caller must also ask. Both halves are pinned."""
    from agent_mcp.tools.admin_tools import get_agent_tokens_tool_impl

    async with mcp_session(tmp_path) as admin:
        worker = await admin.create_worker("w1")
        principal = make_principal(
            kind="operator_session", user_id="o1",
            project_role="operator",
        )

        masked = await get_agent_tokens_tool_impl({}, principal=principal)
        assert all(r["token"] == REDACTED_TOKEN for r in masked.data["agents"])

        plain = await get_agent_tokens_tool_impl(
            {"include_sensitive_data": True}, principal=principal,
        )
        tokens = {r["token"] for r in plain.data["agents"]}
        assert worker.token in tokens
        assert plain.data["filters_applied"]["include_sensitive_data"] is True


# ── site B: REST /api/all-data (drop + gated auth_token) ──────────


@pytest.mark.parametrize("confirmed", [True, False])
async def test_all_data_never_returns_a_raw_secret_column(
    tmp_path, confirmed: bool,
) -> None:
    """Neither secret column appears on an ``/api/all-data`` agent row at
    ANY tier — the raw columns are ``SELECT *`` artefacts. The gated
    ``auth_token`` field is the only bearer this endpoint exposes."""
    async with mcp_session(tmp_path) as admin:
        worker = await admin.create_worker("w1")
        _seed_agent("w-aoe", "tok-aoe-1234567890")

        if confirmed:
            r = admin.client.get(
                "/api/all-data",
                headers={"Authorization": f"Bearer {admin.admin_token}"},
            )
        else:
            r = admin.get("/api/all-data")
        assert r.status_code == 200, r.text

        agents = r.json()["agents"]
        assert agents
        for row in agents:
            assert "token" not in row
            assert "aoe_session_id" not in row
            assert "auth_token" in row, "the gated field stays present"
        assert "deadbeefcafe0000" not in r.text
        assert "tok-aoe-1234567890" not in r.text

        w1 = next(a for a in agents if a["agent_id"] == "w1")
        assert w1["auth_token"] == (worker.token if confirmed else None)


# ── site C: REST /api/node-details (allowlist, no tier at all) ────


@pytest.mark.parametrize("confirmed", [True, False])
async def test_node_details_withholds_secrets_from_every_tier(
    tmp_path, confirmed: bool,
) -> None:
    """Including a CONFIRMED operator: this panel is a display surface,
    not a credential surface. Pinning both tiers is the point — routing
    it through a tier-conditional redactor would change this."""
    async with mcp_session(tmp_path) as admin:
        _seed_agent("w-node", "tok-node-1234567890")

        url = "/api/node-details?node_id=agent_w-node"
        if confirmed:
            r = admin.client.get(
                url, headers={"Authorization": f"Bearer {admin.admin_token}"},
            )
        else:
            r = admin.get(url)
        assert r.status_code == 200, r.text

        data = r.json()["data"]
        assert data["agent_id"] == "w-node"
        assert "token" not in data
        assert "aoe_session_id" not in data
        assert "tok-node-1234567890" not in r.text
        assert "deadbeefcafe0000" not in r.text


# ── site D: REST /api/tokens (gate, not redaction) ────────────────


async def test_tokens_endpoint_gates_rather_than_masks(tmp_path) -> None:
    """The one surface whose purpose IS serving plaintext bearers: 403 or
    the real list, never a masked middle ground. Pinned so the
    consolidation doesn't turn it into a redaction site."""
    async with mcp_session(tmp_path) as admin:
        worker = await admin.create_worker("w1")

        denied = admin.get("/api/tokens")
        assert denied.status_code == 403
        assert worker.token not in denied.text
        assert REDACTED_TOKEN not in denied.text, (
            "this endpoint denies; it does not hand back masked rows"
        )

        allowed = admin.client.get(
            "/api/tokens",
            headers={"Authorization": f"Bearer {admin.admin_token}"},
        )
        assert allowed.status_code == 200
        assert worker.token in {
            t["token"] for t in allowed.json()["agent_tokens"]
        }
