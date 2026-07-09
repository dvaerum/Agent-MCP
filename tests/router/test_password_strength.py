"""Password-strength policy tests (round-3 security finding AC-2).

The first-boot setup wizard historically enforced NO minimum
password length/complexity (the live operator was ``dev``/``dev``,
3 chars). argon2 + login rate-limiting mitigate online brute-force,
but a self-provisioning multi-tenant operator deserves a floor.

The policy lives in ONE place — ``identity.validate_password_strength``
(canonical home) — and is enforced by the setup wizard BEFORE the
password reaches ``create_user``. This module exercises both the
helper directly and the setup-wizard HTTP surface.
"""

from __future__ import annotations

import pytest


# no_seed_operator applies module-wide (the async setup tests need an
# empty users table); the async tests carry @pytest.mark.asyncio
# individually so the sync helper-unit tests don't get a spurious mark.
pytestmark = pytest.mark.no_seed_operator


def _identity_module():
    import agent_mcp.router.identity as identity

    identity.run_router_migrations_upgrade()
    return identity


# ── Helper unit tests ──────────────────────────────────────────────


def test_validate_password_strength_rejects_short() -> None:
    """A password shorter than the minimum raises WeakPasswordError."""
    import agent_mcp.router.identity as identity

    too_short = "a" * (identity.PASSWORD_MIN_LENGTH - 1)
    with pytest.raises(identity.WeakPasswordError):
        identity.validate_password_strength(too_short)


def test_validate_password_strength_rejects_empty() -> None:
    """An empty password is rejected."""
    import agent_mcp.router.identity as identity

    with pytest.raises(identity.WeakPasswordError):
        identity.validate_password_strength("")


def test_validate_password_strength_accepts_min_length() -> None:
    """A password at exactly the minimum length is accepted (no raise)."""
    import agent_mcp.router.identity as identity

    ok = "a" * identity.PASSWORD_MIN_LENGTH
    # Must not raise.
    identity.validate_password_strength(ok)


# ── Setup-wizard enforcement ───────────────────────────────────────


@pytest.mark.asyncio
async def test_setup_rejects_weak_password(
    aiohttp_client, router_app,
) -> None:
    """POST /setup with a too-short password → 400, no user created."""
    identity = _identity_module()
    weak = "x" * (identity.PASSWORD_MIN_LENGTH - 1)
    client = await aiohttp_client(router_app)
    resp = await client.post(
        "/agent-mcp/setup",
        data={
            "username": "weakop",
            "password": weak,
            "password_confirm": weak,
        },
        allow_redirects=False,
    )
    assert resp.status == 400, await resp.text()
    # The rejection MUST surface before any user row is written.
    assert identity.get_user_by_username("weakop") is None
    # No session cookie is minted on a rejected setup.
    assert "agent_mcp_session" not in resp.headers.get("Set-Cookie", "")


@pytest.mark.asyncio
async def test_setup_accepts_strong_password(
    aiohttp_client, router_app,
) -> None:
    """POST /setup with a policy-compliant password → 303, user created."""
    identity = _identity_module()
    strong = "correct-horse-battery"  # 21 chars, comfortably over floor
    client = await aiohttp_client(router_app)
    resp = await client.post(
        "/agent-mcp/setup",
        data={
            "username": "strongop",
            "password": strong,
            "password_confirm": strong,
        },
        allow_redirects=False,
    )
    assert resp.status == 303, await resp.text()
    user = identity.get_user_by_username("strongop")
    assert user is not None
    assert identity.verify_password(user["password_hash"], strong)
