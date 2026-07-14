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

Wave 11 (ADR-0016): the ROOT-CAUSE fix — ``config_*`` rows moved out of
``project_context`` into the dedicated ``project_settings`` store, so
the readback seam is now ``GET /api/settings-data`` (the composition
reads no longer carry config rows at all). The REST tests below assert
the same two-sided guarantee against the new seam; the ``is_secret_key``
unit tests keep pinning the read-side carve-out until the redaction
machinery is deleted in the ADR-0016 follow-up PR.
"""

from __future__ import annotations

import json

import pytest

from tests.harness import mcp_session, seed_config_setting_as_sysadmin


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
        seed_config_setting_as_sysadmin(key, value)
    # Paired guard: a real credential in the same namespace.
    seed_config_setting_as_sysadmin("config_aoe_bearer_token", _BEARER_SECRET)


def _row(context: list[dict], key: str) -> dict:
    for r in context:
        if r.get("context_key") == key:
            return r
    raise AssertionError(f"context row {key!r} missing from response")


@pytest.mark.asyncio
async def test_settings_data_policy_toggles_readable_to_session_operator(
    tmp_path,
) -> None:
    """``/api/settings-data`` as a cookie/forwarding (non-confirmed)
    operator: policy toggles carry their real value; the AoE bearer stays
    redacted. This is the F009 guarantee at its post-ADR-0016 seam."""
    async with mcp_session(tmp_path) as admin:
        _seed_all()

        r = admin.get("/api/settings-data")  # signed forwarding header
        assert r.status_code == 200, r.text
        rows = r.json()["settings"]

        for key, value in _POLICY_SEEDS.items():
            row = _row(rows, key)
            assert row["value"] != _REDACTED, (
                f"policy key {key} redacted to a session operator (F009)"
            )
            assert json.loads(row["value"]) == value, (
                f"policy key {key} value corrupted: {row['value']!r}"
            )

        # PAIRED GUARD — the credential MUST stay redacted on this call.
        bearer = _row(rows, "config_aoe_bearer_token")
        assert bearer["value"] == _REDACTED, (
            "config_aoe_bearer_token leaked to a non-confirmed operator"
        )
        assert _BEARER_SECRET not in r.text, "bearer secret in settings-data body"


@pytest.mark.asyncio
async def test_config_rows_absent_from_composition_reads(tmp_path) -> None:
    """Post-cutover: config rows no longer pollute the memory read seams
    at all — ``/api/all-data`` and ``/api/context-data`` carry neither
    the toggles nor the AoE bearer (in any form)."""
    async with mcp_session(tmp_path) as admin:
        _seed_all()

        r = admin.get("/api/all-data")
        assert r.status_code == 200, r.text
        context_keys = {c.get("context_key") for c in r.json()["context"]}
        assert not (set(_POLICY_SEEDS) & context_keys)
        assert "config_aoe_bearer_token" not in context_keys
        assert _BEARER_SECRET not in r.text

        r = admin.get("/api/context-data")
        assert r.status_code == 200, r.text
        context_keys = {c.get("context_key") for c in r.json()}
        assert not (set(_POLICY_SEEDS) & context_keys)
        assert _BEARER_SECRET not in r.text


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
