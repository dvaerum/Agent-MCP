"""F009: worker-policy toggles must read back TRUE state to a
cookie/forwarding operator.

BUG (operator-approved, high-confidence): ``is_secret_key`` treated ANY
``config_*`` key as secret. Combined with the per-project backend
conservatively treating cookie/forwarding sessions as NON-confirmed
operator tier, the ``/api/all-data`` + ``/api/context-data`` reads
redacted EVERY ``config_*`` value — including the non-secret worker-policy
toggles the Settings dashboard reads — to ``[redacted]``. The dashboard's
``coerceBool('[redacted]', default)`` then fell back to the default, so a
toggle stored ``true`` rendered OFF.

Fix A: an explicit NON-secret POLICY carve-out in ``is_secret_key``,
checked BEFORE the blanket ``config_*`` rule. These tests pin BOTH sides
of the class boundary on the SAME response:

  * the policy keys read back their real value (not ``[redacted]``), and
  * ``config_aoe_bearer_token`` (a credential) STAYS ``[redacted]`` —
    proving the fix does not re-open the pentest-hardened secret.
"""

from __future__ import annotations

import json

import pytest

from tests.harness import mcp_session, seed_config_context_as_sysadmin


_REDACTED = "[redacted]"
_BEARER_SECRET = "SENTINEL-AOE-BEARER-9f04"

# Every policy toggle the Settings dashboard reads
# (settings-dashboard.tsx). Seeded true / non-zero so a redaction would be
# observable as the wrong (default) state.
_POLICY_SEEDS: dict[str, object] = {
    "config_allow_worker_to_worker": True,
    "config_allow_worker_self_assign": True,
    "config_allow_worker_update_own_status": True,
    "config_allow_worker_create_unassigned": True,
    "config_auto_event_loop_global": True,
    "config_aoe_notify_enabled": True,
    "config_message_retention_days": 7,
}


def _seed_all() -> None:
    for key, value in _POLICY_SEEDS.items():
        seed_config_context_as_sysadmin(key, value)
    # Paired guard: a real credential in the same namespace.
    seed_config_context_as_sysadmin("config_aoe_bearer_token", _BEARER_SECRET)


def _row(context: list[dict], key: str) -> dict:
    for r in context:
        if r.get("context_key") == key:
            return r
    raise AssertionError(f"context row {key!r} missing from response")


@pytest.mark.asyncio
async def test_all_data_policy_toggles_readable_to_session_operator(
    tmp_path,
) -> None:
    """``/api/all-data`` as a cookie/forwarding (non-confirmed) operator:
    policy toggles carry their real value; the AoE bearer stays redacted."""
    async with mcp_session(tmp_path) as admin:
        _seed_all()

        r = admin.get("/api/all-data")  # signed forwarding header
        assert r.status_code == 200, r.text
        context = r.json()["context"]

        for key, value in _POLICY_SEEDS.items():
            row = _row(context, key)
            assert row["value"] != _REDACTED, (
                f"policy key {key} redacted to a session operator (F009)"
            )
            assert json.loads(row["value"]) == value, (
                f"policy key {key} value corrupted: {row['value']!r}"
            )

        # PAIRED GUARD — the credential MUST stay redacted on this call.
        bearer = _row(context, "config_aoe_bearer_token")
        assert bearer["value"] == _REDACTED, (
            "config_aoe_bearer_token leaked to a non-confirmed operator"
        )
        assert _BEARER_SECRET not in r.text, "bearer secret in all-data body"


@pytest.mark.asyncio
async def test_context_data_policy_toggles_readable_to_session_operator(
    tmp_path,
) -> None:
    """Same guarantee on ``/api/context-data`` (the sibling read seam)."""
    async with mcp_session(tmp_path) as admin:
        _seed_all()

        r = admin.get("/api/context-data")  # signed forwarding header
        assert r.status_code == 200, r.text
        context = r.json()

        for key, value in _POLICY_SEEDS.items():
            row = _row(context, key)
            assert row["value"] != _REDACTED, (
                f"policy key {key} redacted to a session operator (F009)"
            )
            assert json.loads(row["value"]) == value

        bearer = _row(context, "config_aoe_bearer_token")
        assert bearer["value"] == _REDACTED, (
            "config_aoe_bearer_token leaked to a non-confirmed operator"
        )
        assert _BEARER_SECRET not in r.text, "bearer secret in context-data body"


# ── is_secret_key unit boundary ──────────────────────────────────────


def test_is_secret_key_policy_carveout() -> None:
    """Each policy key is NOT secret; the blanket ``config_*`` default and
    the AoE credential gate still hold."""
    from agent_mcp.tools.project_context_tools import is_secret_key

    for key in _POLICY_SEEDS:
        assert not is_secret_key(key), f"{key} must be readable (policy)"
        # Case-insensitive: the redaction regexes are IGNORECASE, so the
        # carve-out must be too.
        assert not is_secret_key(key.upper()), f"{key.upper()} (case)"

    # HARD CONSTRAINT — the AoE bearer stays secret (blanket rule AND the
    # bearer/token vocab in _SECRET_SUFFIX_RE both cover it).
    assert is_secret_key("config_aoe_bearer_token")
    assert is_secret_key("config_aoe_bearer_token_file")

    # Unlisted config_* keys still redact — blanket default is intact.
    for key in ("config_zzz", "config_foo", "config_aoe_base_url",
                "config_aoe_notify_template", "config_aoe_timeout_ms"):
        assert is_secret_key(key), f"{key} must stay secret (blanket)"


def test_is_secret_key_gates_on_key_name_not_value() -> None:
    """The carve-out is KEY-name only: the value-scan backstop still
    redacts a credential pasted into a policy key's VALUE."""
    from agent_mcp.features.rag.indexing import _value_has_embedded_secret

    # A policy key's NAME is not secret ...
    from agent_mcp.tools.project_context_tools import is_secret_key

    assert not is_secret_key("config_allow_worker_to_worker")
    # ... but a secret pasted into its value still trips the value scan,
    # which every redaction seam runs alongside is_secret_key.
    assert _value_has_embedded_secret("ghp_0123456789abcdefghijABCDEFG", None)
