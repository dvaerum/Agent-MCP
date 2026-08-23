"""N1 scope decision: form-encoded credential bodies do NOT join the
body-decode seam — identity fields are sanitized where they are
PERSISTED instead.

N1 posed this as an open scope question ("do form-encoded bodies join
the same seam or get a declared+tested exemption") and required it be
settled with a test rather than by discussion. This file is that test,
and it is what ``tests/router/test_arch_enforced_sanitization.py``'s
exemption entries for ``login.login_post_handler`` and
``setup_wizard.setup_post_handler`` point at.

The decision, and why:

1. **A password must not be sanitized.** It is a byte-for-byte secret
   compared against an argon2 hash. Stripping at login while
   ``create_user`` stored the unstripped original locks the account
   out; stripping at BOTH ends silently collapses distinct passwords
   onto one, shrinking the keyspace. Neither is acceptable, so the
   form parse stays raw.

2. **A submitted username must not be sanitized either** — for the
   opposite reason. ``get_user_by_username`` is an EXACT lookup;
   stripping the submitted value would let ``ad<ZWSP>min`` authenticate
   as the stored ``admin``. That is a WIDENING, precisely the inverse
   of what the sanitizer exists for. Fail-closed (no match) is correct.

3. **Identity fields ARE sanitized — at the write.**
   ``identity.create_user`` is the single writer of the ``username`` /
   ``email`` columns shared by every provisioning path (CLI bootstrap,
   env-var bootstrap, the unauthenticated first-boot wizard, the REST
   create-user endpoint, and SSO JIT-create from an IdP claim). N1
   added ``username`` to the strip that R15-F2 had applied to ``email``
   only. So the STORED identity is canonical while the SUBMITTED
   credential stays byte-exact — the two properties that (1) and (2)
   need, held at the same time.

Bypass #2 of the finding is the ``username`` half of point 3: pre-fix,
``create_user`` stripped ``email`` but not ``username``, and the two
paths that reach it without any sanitizing decode in between (the
form-encoded wizard, and SSO JIT-create) wrote whatever they were
given.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.no_seed_operator

#: (submitted, stored) — the hidden-format classes prior pentest rounds
#: found reaching persisted, rendered strings.
_SPOOFED_USERNAMES = [
    pytest.param("ad\u200bmin", "admin", id="zwsp"),
    pytest.param("admin\u202e", "admin", id="rtlo"),
    pytest.param("ad\ufeffmin", "admin", id="bom"),
    pytest.param("ad\u2060min", "admin", id="word-joiner"),
    pytest.param("ad\x1b[31mmin", "ad[31mmin", id="c0-esc"),
    pytest.param("ad\x9bmin", "admin", id="c1-csi"),
    pytest.param("ad\ud800min", "admin", id="lone-surrogate"),
]


def _identity_module():
    import agent_mcp.router.identity as identity

    identity.run_router_migrations_upgrade()
    return identity


# ── Point 3: create_user is the sanitizing seam for identity ───────


@pytest.mark.parametrize("submitted, stored", _SPOOFED_USERNAMES)
def test_create_user_strips_hidden_unicode_from_username(
    router_module, submitted: str, stored: str,
) -> None:
    """Bypass #2. ``create_user`` stripped ``email`` (R15-F2) but left
    ``username`` raw, so every provisioning path wrote a spoofable
    operator identity."""
    identity = _identity_module()
    user_id = identity.create_user(username=submitted, password="hunter2-long")
    row = identity.get_user_by_id(user_id)
    assert row["username"] == stored, (
        f"username persisted as {row['username']!r}; create_user is the "
        f"single writer shared by the CLI, the env bootstrap, the "
        f"first-boot wizard, the REST endpoint and SSO JIT-create, so "
        f"the strip belongs there (N1 bypass #2)."
    )


@pytest.mark.parametrize(
    "username",
    ["alice", "Ops-Team_01", "josé", "Tiếng", "田中", "مشغل"],
    ids=["ascii", "punctuated", "accented", "vietnamese", "cjk", "arabic"],
)
def test_create_user_leaves_ordinary_usernames_untouched(
    router_module, username: str,
) -> None:
    """No-policy-change guard for bypass #2: widening the strip to
    ``username`` must not break a legitimate non-Latin operator name.
    The sanitizer targets hidden-format/zero-width/bidi/surrogate
    classes only — ordinary printable Unicode, including the combining
    marks Vietnamese and Arabic rely on, round-trips byte-identical."""
    identity = _identity_module()
    user_id = identity.create_user(username=username, password="hunter2-long")
    assert identity.get_user_by_id(user_id)["username"] == username
    # And the canonical value is the one the exact-match lookup finds.
    assert identity.get_user_by_username(username)["user_id"] == user_id


def test_username_lookup_stays_an_exact_match(router_module) -> None:
    """Point 2. The stored name is canonical, but the LOOKUP is not
    sanitized — so a spoofed submission fails closed instead of being
    folded onto the real account."""
    identity = _identity_module()
    identity.create_user(username="admin", password="hunter2-long")
    assert identity.get_user_by_username("ad\u200bmin") is None, (
        "get_user_by_username must not strip its argument: doing so "
        "would let ad<ZWSP>min resolve to admin, a widening."
    )


# ── Points 1 + 2: the form handlers stay raw ───────────────────────


@pytest.mark.asyncio
async def test_password_with_hidden_unicode_is_not_stripped_at_login(
    aiohttp_client, router_app,
) -> None:
    """Point 1, the load-bearing half. A password containing a
    zero-width space authenticates ONLY as its exact bytes. If
    ``login_post_handler`` ever joined the decode seam, the stripped
    form of this password would start authenticating (and the exact
    form would stop), which is both a lockout and a keyspace
    collapse."""
    identity = _identity_module()
    exact = "correct\u200bhorse\u202ebattery"
    stripped = "correcthorsebattery"
    identity.create_user(username="pwop", password=exact)
    client = await aiohttp_client(router_app)

    ok = await client.post(
        "/agent-mcp/login",
        data={"username": "pwop", "password": exact},
        allow_redirects=False,
    )
    assert ok.status == 303, await ok.text()

    bad = await client.post(
        "/agent-mcp/login",
        data={"username": "pwop", "password": stripped},
        allow_redirects=False,
    )
    assert bad.status == 401, (
        "the sanitizer-stripped form of the password authenticated — "
        "login's form body must NOT be routed through the decode seam "
        "(see tests/router/test_arch_enforced_sanitization.py's "
        "declared exemption for login_post_handler)."
    )


@pytest.mark.asyncio
async def test_spoofed_username_does_not_authenticate_as_the_real_one(
    aiohttp_client, router_app,
) -> None:
    """Point 2 over the real HTTP surface."""
    identity = _identity_module()
    identity.create_user(username="admin", password="hunter2-long")
    client = await aiohttp_client(router_app)
    resp = await client.post(
        "/agent-mcp/login",
        data={"username": "ad\u200bmin", "password": "hunter2-long"},
        allow_redirects=False,
    )
    assert resp.status == 401, (
        "a zero-width-space-spoofed username authenticated as the real "
        "operator — the login lookup must stay an exact match."
    )


@pytest.mark.asyncio
async def test_setup_wizard_persists_a_sanitized_username(
    aiohttp_client, router_app,
) -> None:
    """Point 3 over the real HTTP surface, on the path that matters
    most: the first-boot wizard is UNAUTHENTICATED and form-encoded, so
    it reaches ``create_user`` without passing any sanitizing decode.
    The submitted password still works byte-exact afterwards, proving
    the two properties hold together."""
    identity = _identity_module()
    password = "correct-horse-battery"
    client = await aiohttp_client(router_app)
    resp = await client.post(
        "/agent-mcp/setup",
        data={
            "username": "ad\u200bmin\u202e",
            "password": password,
            "password_confirm": password,
            "email": "ops\u200b@example.com",
        },
        allow_redirects=False,
    )
    assert resp.status == 303, await resp.text()

    user = identity.get_user_by_username("admin")
    assert user is not None, (
        "the first operator was stored under a spoofable username — "
        "create_user must strip it (N1 bypass #2)."
    )
    assert user["email"] == "ops@example.com"
    assert identity.verify_password(user["password_hash"], password), (
        "the wizard's password must be hashed byte-exact, unaffected by "
        "the username/email strip."
    )
