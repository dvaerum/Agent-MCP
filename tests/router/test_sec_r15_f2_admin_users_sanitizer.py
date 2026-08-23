"""Security R15-F2 (HIGH, CONFIRMED, live-exploited) — admin_users_api's
``_json_body`` never routed through the shared JSON sanitizer.

FINDING: ``admin_users_api.py::_json_body`` parses every request body on
this REST surface with a bare ``json.loads(raw)`` — it never calls
``utils.json_utils.sanitize_json_input``/``_strip_control_bytes``/
``_strip_hidden_unicode``, the single shared chokepoint every other
JSON-body surface in the codebase uses (MCP ``tools/call``, the FastAPI
``app/routers/*`` tier via ``get_sanitized_json_body``). Class-sweep miss
on the R4-F3/R5-F8/R13-F2/R14-F3/R15-F1 sanitizer lineage.

Two confirmed live consequences, both reproduced here:

1. CRASH (pre-R15-F1): an unpaired UTF-16 surrogate in ``email`` (via a
   JSON ``\\ud800``-style escape) survived unsanitized and crashed
   SQLite's UTF-8 TEXT bind with ``UnicodeEncodeError`` — bare 500 on
   both ``POST /users`` (create) and ``PATCH /users/<id>`` (edit, which
   had NO exception handling around its UPDATE at all).

2. SPOOFING: hidden-Unicode spoofing characters (zero-width space
   U+200B, RTLO U+202E) pass through completely unstripped into
   ``email`` and are echoed back verbatim by ``GET /users`` — the exact
   character classes R13-F2/R14-F3 strip on every other surface.

R16-F3 UPDATE: R15-F1 (PR #700, merged AFTER this file's tests were
written but BEFORE this file's tests were merged) widened
``_HIDDEN_FORMAT_CATEGORIES`` to include ``Cs`` (Surrogate), so
``sanitize_json_input``/``_strip_control_bytes`` now SILENTLY STRIPS a
lone/unpaired surrogate from every JSON string leaf upstream of
everything in this file. The four tests below that originally asserted
"reject with a clean 400 / raise InvalidEmailError" have been rewritten
to assert the ACTUAL correct post-R15-F1 contract: the surrogate is
silently stripped and the call SUCCEEDS — matching how every other
sanitizer call site in this codebase already behaves, and matching
this session's own round-15 RE_VERIFY finding that silent-strip-then-
succeed (not reject) is the consistent, intended behavior. The critical
security property under test is unchanged and still asserted: no bare
500, no crash, no unsanitized surrogate ever reaching a DB bind.
"""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.asyncio


_STRICT_ACCEPT = {
    "Accept": "application/vnd.agent-mcp.v1+json",
    "Content-Type": "application/json",
}

# A JSON string carrying an unpaired UTF-16 surrogate escape. json.loads
# happily decodes \ud800 into a lone-surrogate Python str (valid Python,
# not valid Unicode text) -- this is what crashes the SQLite TEXT bind.
_SURROGATE_EMAIL_BODY = b'{"username": "surruser", "password": "longenoughpassword", "email": "abc\\ud800def@example.com"}'

_ZWSP = "\u200b"  # zero-width space
_RTLO = "\u202e"  # right-to-left override
_SPOOF_EMAIL = f"abc{_ZWSP}{_RTLO}def@example.com"


async def _create_user(client, username: str, password: str = "longenoughpassword") -> str:
    resp = await client.post(
        "/agent-mcp/api/router/users",
        data=json.dumps({"username": username, "password": password}),
        headers=_STRICT_ACCEPT,
    )
    assert resp.status == 201, f"got {resp.status}: {await resp.text()}"
    return (await resp.json())["user"]["user_id"]


# ── Consequence 1: crash on create ─────────────────────────────────


async def test_create_user_with_unpaired_surrogate_email_no_bare_500(
    aiohttp_client, router_app,
) -> None:
    """R16-F3: post-R15-F1, the unpaired surrogate is silently stripped
    by the shared sanitizer BEFORE ``_json_body`` ever returns it — the
    create call succeeds (201) with the sanitized email, not a 400
    reject. The security property that matters (no bare 500, no raw
    surrogate reaching the DB bind) still holds and is asserted here."""
    client = await aiohttp_client(router_app)

    resp = await client.post(
        "/agent-mcp/api/router/users",
        data=_SURROGATE_EMAIL_BODY,
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 201, f"got {resp.status}: {await resp.text()}"
    body = await resp.json()
    assert body["success"] is True
    # The surrogate escape is gone; the surrounding text survives.
    assert body["user"]["email"] == "abcdef@example.com"


# ── Consequence 1: crash on edit ───────────────────────────────────


async def test_edit_user_with_unpaired_surrogate_email_no_bare_500(
    aiohttp_client, router_app,
) -> None:
    """R16-F3: same sanitizer-strips-first contract on PATCH — the edit
    succeeds (200) with the sanitized email. edit_user_handler had NO
    exception handling around its UPDATE at all pre-R15-F2; that raw-
    UnicodeEncodeError-escapes-as-500 crash is what must never come
    back, not any particular HTTP status."""
    client = await aiohttp_client(router_app)
    user_id = await _create_user(client, "surredit")

    resp = await client.patch(
        f"/agent-mcp/api/router/users/{user_id}",
        data=b'{"email": "abc\\ud800def@example.com"}',
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 200, f"got {resp.status}: {await resp.text()}"
    body = await resp.json()
    assert body["success"] is True
    assert body["user"]["email"] == "abcdef@example.com"


# ── Consequence 2: spoofing survives unstripped ────────────────────


async def test_create_user_strips_hidden_unicode_from_email(
    aiohttp_client, router_app,
) -> None:
    """Zero-width space / RTLO in ``email`` must be stripped the same
    way R13-F2/R14-F3 strip them everywhere else -- not echoed back
    verbatim by a subsequent GET."""
    client = await aiohttp_client(router_app)

    resp = await client.post(
        "/agent-mcp/api/router/users",
        data=json.dumps({
            "username": "spoofuser",
            "password": "longenoughpassword",
            "email": _SPOOF_EMAIL,
        }),
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 201, f"got {resp.status}: {await resp.text()}"
    body = await resp.json()
    created_email = body["user"]["email"]
    assert _ZWSP not in created_email, repr(created_email)
    assert _RTLO not in created_email, repr(created_email)

    listing = await client.get(
        "/agent-mcp/api/router/users", headers=_STRICT_ACCEPT,
    )
    listed = next(
        u for u in (await listing.json())["users"]
        if u["username"] == "spoofuser"
    )
    assert _ZWSP not in listed["email"], repr(listed["email"])
    assert _RTLO not in listed["email"], repr(listed["email"])


# ── Regression: legitimate Unicode email still works ───────────────


async def test_create_user_with_real_non_latin_email_still_works(
    aiohttp_client, router_app,
) -> None:
    """A real internationalised email (non-Latin local-part/domain) must
    round-trip unchanged -- the fix must not over-strip legitimate
    content."""
    client = await aiohttp_client(router_app)
    real_email = "用户@例え.jp"

    resp = await client.post(
        "/agent-mcp/api/router/users",
        data=json.dumps({
            "username": "intluser",
            "password": "longenoughpassword",
            "email": real_email,
        }),
        headers=_STRICT_ACCEPT,
    )

    assert resp.status == 201, f"got {resp.status}: {await resp.text()}"
    body = await resp.json()
    assert body["user"]["email"] == real_email


# ── Sibling: identity.py's SSO/CLI provisioning path ───────────────
#
# Same bug CLASS as the REST-layer crash above, in the ONE OTHER place
# ``email`` reaches a ``users`` INSERT: ``identity.create_user`` (used by
# CLI bootstrap, the setup wizard, AND ``sso.find_or_create_sso_user``'s
# JIT-create fork for IdP-claim-derived email). Not live-testable via
# HTTP here (SSO isn't configured on this target) but fully testable at
# the function level -- exactly how the OIDC callback drives it.


async def test_identity_create_user_rejects_unpaired_surrogate_email(
    router_app,
) -> None:
    """R16-F3: ``identity.create_user`` strips the unpaired surrogate
    via its own internal ``_strip_control_bytes(email)`` call (the same
    R15-F1 sanitizer, applied directly here rather than by a caller's
    JSON-body layer — this path is exercised by the CLI bootstrap and
    the setup wizard, neither of which goes through
    ``sanitize_json_input``) and succeeds with the sanitized email,
    instead of letting a raw ``UnicodeEncodeError`` escape from the
    INSERT. The critical property (no crash, no raw surrogate reaching
    the DB bind) still holds."""
    from agent_mcp.router import identity

    identity.run_router_migrations_upgrade()
    user_id = identity.create_user(
        username="idsurr",
        password="longenoughpassword",
        email="abc\ud800def@example.test",
    )
    row = identity.get_user_by_id(user_id)
    assert row is not None
    assert row["email"] == "abcdef@example.test"


async def test_identity_create_user_strips_hidden_unicode_from_email(
    router_app,
) -> None:
    """Same hidden-Unicode strip as the REST layer, applied once for
    every ``create_user`` caller (CLI/SSO included)."""
    from agent_mcp.router import identity

    identity.run_router_migrations_upgrade()
    identity.create_user(
        username="idspoof",
        password="longenoughpassword",
        email=_SPOOF_EMAIL,
    )
    row = identity.get_user_by_username("idspoof")
    assert row is not None
    assert _ZWSP not in row["email"], repr(row["email"])
    assert _RTLO not in row["email"], repr(row["email"])


async def test_sso_find_or_create_rejects_unpaired_surrogate_email(
    router_app,
) -> None:
    """R16-F3: the IdP-claim-derived JIT-create path must not crash on a
    malicious/misconfigured IdP's ``email`` claim -- mirrors the REST
    behaviour but through ``sso.find_or_create_sso_user`` exactly as
    ``handle_oidc_callback`` drives it. Post-R15-F1 the surrogate is
    stripped (by ``identity.create_user``'s own sanitizer call) and the
    JIT-create succeeds with the sanitized email, rather than raising."""
    import sys

    from agent_mcp.router import identity

    sso = sys.modules.get("agent_mcp.router.sso")
    if sso is None:
        import importlib
        sso = importlib.import_module("agent_mcp.router.sso")
    identity.run_router_migrations_upgrade()

    user = sso.find_or_create_sso_user(
        email="abc\ud800def@example.test",
        preferred_username="idpuser",
        subject=sso.SsoSubject("https://idp.example.test", "sub-1").encode(),
        email_verified=False,
    )
    row = identity.get_user_by_username("idpuser")
    assert row is not None
    assert row["user_id"] == user["user_id"]
    assert row["email"] == "abcdef@example.test"


# ── R16-F3 permanent cross-PR-interaction guard ────────────────────
#
# R15-F1 and R15-F2 were developed in isolated worktrees off the same
# earlier base, each with a fully green LOCAL suite, and merged in
# sequence -- R15-F1's widened sanitizer silently made R15-F2's own
# validation guard unreachable dead code, and nothing caught it until a
# later round ran the full suite against real merged `main`. The tests
# below don't just re-check today's behaviour; they pin down the
# INVARIANT the whole class of "sanitize upstream, reject-guard
# downstream" pattern depends on, so the next isolated-worktree pair
# that touches either side of this boundary gets a red test instead of
# a silent interaction.


async def test_hidden_format_categories_is_exactly_the_utf8_unencodable_set() -> None:
    """Pin the fact that ``Cs`` (Surrogate) is the ONLY Unicode general
    category whose members fail ``str.encode("utf-8", "strict")``, and
    that it's present in ``_HIDDEN_FORMAT_CATEGORIES``.

    This is the exact fact R16-F3 is about: ``_reject_unencodable_str``/
    ``identity.create_user``'s old encode-check assumed ``Cs`` was OUT
    of scope for the sanitizer; R15-F1 put it IN scope, which made that
    downstream check permanently unreachable. If a future change ever
    removes ``Cs`` from this set, this test's exhaustive-over-a-sample
    encodability check will start including surrogates as false
    "encodable" holes and MUST make the reader re-add an explicit
    downstream UTF-8-round-trip guard again -- see the module-level
    comment on ``_HIDDEN_FORMAT_CATEGORIES`` in
    ``agent_mcp/utils/json_utils.py``.
    """
    import unicodedata

    from agent_mcp.utils.json_utils import _HIDDEN_FORMAT_CATEGORIES

    assert "Cs" in _HIDDEN_FORMAT_CATEGORIES

    # Every BMP + a sample of astral-plane code points: confirm the only
    # encode failures are category `Cs`, and confirm every `Cs` code
    # point in range does fail. (Full 0x0-0x10FFFF sweep is the same
    # check at 17x the cost; the BMP already contains every `Cs` code
    # point that exists -- surrogates only occupy U+D800-U+DFFF.)
    unencodable_categories: set[str] = set()
    surrogate_failures = 0
    surrogate_total = 0
    for cp in range(0x10000):
        ch = chr(cp)
        cat = unicodedata.category(ch)
        try:
            ch.encode("utf-8", "strict")
        except UnicodeEncodeError:
            unencodable_categories.add(cat)
            if cat == "Cs":
                surrogate_failures += 1
        if cat == "Cs":
            surrogate_total += 1

    assert unencodable_categories == {"Cs"}, (
        f"a non-Cs category now fails UTF-8 encoding: {unencodable_categories!r} "
        "-- the sanitizer's Cs-only strip is no longer sufficient; a "
        "downstream UTF-8-round-trip guard is needed again"
    )
    assert surrogate_total > 0 and surrogate_failures == surrogate_total


async def test_strip_control_bytes_output_is_always_utf8_encodable() -> None:
    """The invariant every downstream "reject unencodable content"
    guard used to enforce independently: after
    ``_strip_control_bytes``, a string ALWAYS round-trips through UTF-8.

    This is what made ``_reject_unencodable_str``/``create_user``'s old
    encode-check redundant (option (a) from the R16-F3 fix direction:
    the sanitizer's output is already valid per the downstream check,
    for every input) -- pinned directly so a future change to either
    side can't silently reopen the gap without a test noticing.
    """
    from agent_mcp.utils.json_utils import _strip_control_bytes

    samples = [
        "abc\ud800def@example.com",  # lone high surrogate
        "abc\udfffdef@example.com",  # lone low surrogate
        "\ud800\ud800\ud800",  # run of surrogates
        "plain-ascii@example.com",
        "用户@例え.jp",  # real non-Latin content must survive untouched
        _SPOOF_EMAIL,  # R13-F2/R14-F3 hidden Unicode (ZWSP + RTLO)
    ]
    for s in samples:
        stripped = _strip_control_bytes(s)
        stripped.encode("utf-8", "strict")  # must never raise


async def test_sanitize_json_input_email_field_always_utf8_encodable_end_to_end() -> None:
    """Runs the SAME sanitizer call ``admin_users_api._json_body`` makes
    against a realistic request body, end to end -- not a mock, not an
    isolated-worktree assumption about what the other PR's code does.
    This is the "real merged main" check: exercises R15-F1's sanitizer
    and confirms its output already satisfies the property R15-F2's
    (now-removed) downstream guard used to check separately.
    """
    import json as _json

    from agent_mcp.utils.json_utils import sanitize_json_input

    raw = _SURROGATE_EMAIL_BODY
    parsed = sanitize_json_input(raw)
    assert isinstance(parsed, dict)
    parsed["email"].encode("utf-8", "strict")  # must never raise
    assert "\udc00" not in parsed["email"] and "\ud800" not in parsed["email"]

    # Sanity: the input truly contained a lone surrogate pre-sanitize,
    # so this test would have failed loudly pre-R15-F1.
    pre_sanitize = _json.loads(raw.decode("utf-8", "surrogatepass"))
    with pytest.raises(UnicodeEncodeError):
        pre_sanitize["email"].encode("utf-8", "strict")
