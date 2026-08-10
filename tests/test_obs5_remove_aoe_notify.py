"""OBS5 — the ``aoe_notify`` side-channel feature is removed.

The spawn-era AoE notify push (``features/aoe_notify.py`` + the
``config_aoe_*`` settings) is superseded by the ADR-0021 delivery bridge
and its tmux premise died with Wave 7. These tests pin the removal:

  * the ``config_aoe_*`` settings are gone from the schema registry;
  * the ``aoe_notify`` module no longer exists;
  * ``send_agent_message`` no longer references / calls any AoE path and
    still succeeds (the message store + gate are untouched).

The migration that purges pre-existing ``config_aoe_*`` rows is covered
by ``tests/test_migration_0024_drop_config_aoe.py``.
"""

from __future__ import annotations

import importlib
import inspect

import pytest

from agent_mcp.core.tool_result import Ok
from tests.harness import mcp_session

pytestmark = pytest.mark.asyncio


async def test_no_config_aoe_keys_in_schema() -> None:
    from agent_mcp.core.settings_schema import (
        SECRET_SETTING_KEYS,
        SETTINGS_SCHEMA,
    )

    assert not any(
        s.key.startswith("config_aoe_") for s in SETTINGS_SCHEMA
    ), "config_aoe_* settings must be gone from the schema"
    # AoE bearer(+file) were the only secret-typed settings.
    assert SECRET_SETTING_KEYS == frozenset()


async def test_aoe_notify_module_is_removed() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("agent_mcp.features.aoe_notify")


async def test_comm_tools_has_no_aoe_reference() -> None:
    """The message-send tool module carries no AoE import or call site."""
    import agent_mcp.tools.agent_communication_tools as comm

    src = inspect.getsource(comm)
    assert "aoe" not in src.lower(), (
        "agent_communication_tools still references AoE after removal"
    )


async def test_send_agent_message_succeeds_without_aoe(tmp_path) -> None:
    """An operator send still stores the message and returns Ok — the AoE
    fire-and-forget hop is gone, the store + gate are unchanged."""
    async with mcp_session(tmp_path) as admin:
        await admin.create_worker("aoe-free-alice")

        from agent_mcp.tools.agent_communication_tools import (
            send_agent_message_tool_impl,
        )

        result = await send_agent_message_tool_impl(
            {"recipient_id": "aoe-free-alice", "message": "hello with no aoe"},
            principal=admin._principal(),
        )
        assert isinstance(result, Ok), f"expected Ok, got {result!r}"
        assert result.data.get("recipient_id") == "aoe-free-alice"
        assert result.data.get("delivery_status") == "stored"
