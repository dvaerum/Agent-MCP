"""Phase 3 / Finding C — the ``SsoSubject`` value type.

``agent_mcp/router/sso.py``'s OIDC subject key was, for five straight
pentest rounds (R16 → R20), a bare f-string that silently carried FOUR
separate responsibilities at once:

  (a) the persisted ``users.sso_subject`` reconciliation key,
  (b) the scalar TYPE tag that keeps ``sub=True`` and ``sub="True"``
      apart (R18-F1),
  (c) the pre-R18-F1 UNTAGGED legacy-format lookup key (R19-F1), and
  (d) the refuse-on-ambiguity rule that decides when (c) may be
      offered at all (R20-F1).

Each round's fix bolted one more of those onto the same interpolation,
and each bolt-on seeded the next round's finding. This file pins the
same four responsibilities as separate, independently-testable pieces
of a real value type — and re-states R18-F1's, R19-F1's and R20-F1's
exact repros through it.

**This is a representation change, not a policy change.** Every
property those three fixes proved must survive byte-for-byte; the
golden-wire-format test below is the load-bearing guard for that (the
encoded string is a PERSISTED DB key — changing its bytes would orphan
every existing SSO row, which is exactly the R19-F1 bug).
"""

from __future__ import annotations

import random

import pytest

_ISS = "https://idp.example.test"

# The R18-F1 collision set: distinct claim TYPES whose ``str()`` forms
# are identical. A bare f-string collapsed each pair onto one key.
_COLLISION_SET: list[object] = [
    True, "True", False, "False",
    1, "1", 0, "0", -42, "-42",
    1.0, "1.0", 0.5, "0.5",
]


# ── (a)+(b) the key and its type tag ───────────────────────────────


def test_encode_matches_the_persisted_wire_format_byte_for_byte() -> None:
    """GOLDEN: the encoded form is a PERSISTED DB value, so the typed
    re-expression must emit the exact same bytes the f-string did.

    These literals are the pre-refactor ``_oidc_subject`` output,
    copied verbatim. If this test ever needs updating, that is a
    schema migration, not a refactor (R19-F1 is the bug you get for
    changing the key format without one)."""
    from agent_mcp.router.sso import SsoSubject

    assert SsoSubject(_ISS, "abc-123").encode() == (
        "oidc:https://idp.example.test:str:abc-123"
    )
    assert SsoSubject(_ISS, 1).encode() == "oidc:https://idp.example.test:int:1"
    assert SsoSubject(_ISS, 1.0).encode() == (
        "oidc:https://idp.example.test:float:1.0"
    )
    assert SsoSubject(_ISS, True).encode() == (
        "oidc:https://idp.example.test:bool:True"
    )
    assert SsoSubject(_ISS, False).encode() == (
        "oidc:https://idp.example.test:bool:False"
    )
    assert SsoSubject(_ISS, -42).encode() == (
        "oidc:https://idp.example.test:int:-42"
    )


def test_type_tag_is_code_controlled_never_idp_data() -> None:
    """The tag spliced into the key comes from the fixed scalar-type
    tuple, never from IdP-supplied content — that's why it is safe
    unescaped (R18-F1's stated rationale)."""
    from agent_mcp.router.sso import _OIDC_SUBJECT_SCALAR_TYPES, SsoSubject

    tags = {t.__name__ for t in _OIDC_SUBJECT_SCALAR_TYPES}
    for sub in _COLLISION_SET:
        assert SsoSubject(_ISS, sub).type_tag in tags


# ── construction / acceptance rules ────────────────────────────────


def test_from_claims_degrades_unusable_claims_to_none() -> None:
    from agent_mcp.router.sso import SsoSubject

    assert SsoSubject.from_claims(None, "abc") is None
    assert SsoSubject.from_claims("", "abc") is None
    assert SsoSubject.from_claims(_ISS, None) is None
    # R18-F1: an empty ``sub`` carries no identity — same as absent.
    assert SsoSubject.from_claims(_ISS, "") is None
    # Non-scalar shapes (a misserialised multi-valued attribute) are
    # not a sane identity key.
    assert SsoSubject.from_claims(_ISS, {"a": 1}) is None
    assert SsoSubject.from_claims(_ISS, ["a"]) is None


def test_from_claims_accepts_every_json_scalar() -> None:
    """R17-F1: ``sub`` is deliberately wider than ``str`` — degrading a
    numeric IdP subject id to None re-mints an orphan row every login."""
    from agent_mcp.router.sso import SsoSubject

    for sub in _COLLISION_SET:
        built = SsoSubject.from_claims(_ISS, sub)
        assert built is not None
        assert built.sub == sub
        assert type(built.sub) is type(sub)


def test_direct_construction_refuses_an_unusable_subject() -> None:
    """The acceptance rules live on the type: an invalid ``SsoSubject``
    is unconstructable, so no call site can bypass them."""
    from agent_mcp.router.sso import SsoSubject

    for bad_iss, bad_sub in (("", "abc"), (None, "abc")):
        with pytest.raises((ValueError, TypeError)):
            SsoSubject(bad_iss, bad_sub)  # type: ignore[arg-type]
    for bad_sub in (None, "", {"a": 1}, ["a"]):
        with pytest.raises((ValueError, TypeError)):
            SsoSubject(_ISS, bad_sub)  # type: ignore[arg-type]


def test_equality_and_hash_are_type_exact() -> None:
    """Python's own ``==`` collapses ``True == 1 == 1.0``. The value
    type must NOT — type discrimination is the whole point of R18-F1,
    and a value type whose equality collapses the collision set would
    silently re-open it for any dict/set-based caller."""
    from agent_mcp.router.sso import SsoSubject

    bool_sub = SsoSubject(_ISS, True)
    int_sub = SsoSubject(_ISS, 1)
    float_sub = SsoSubject(_ISS, 1.0)
    str_sub = SsoSubject(_ISS, "True")

    assert bool_sub != int_sub
    assert int_sub != float_sub
    assert bool_sub != str_sub
    assert len({bool_sub, int_sub, float_sub, str_sub}) == 4
    assert bool_sub == SsoSubject(_ISS, True)
    assert SsoSubject(_ISS, "x") != SsoSubject("https://other.test", "x")


# ── decode / round-trip properties ─────────────────────────────────


def test_decode_encode_roundtrip_on_the_r18f1_collision_set() -> None:
    from agent_mcp.router.sso import SsoSubject

    for sub in _COLLISION_SET:
        original = SsoSubject(_ISS, sub)
        assert SsoSubject.decode(original.encode()) == original


def _fuzz_pairs(seed: int = 20260823, count: int = 600):
    """Deterministic fuzz over ``(iss, sub)`` — no hypothesis dep."""
    rng = random.Random(seed)
    issuers = [
        "https://idp.example.test",
        "https://keycloak.corp.example/realms/agent-mcp",
        "https://accounts.google.com",
        "http-less-issuer",
        "https://idp.example.test:8443/oidc",  # port colon in the iss
    ]
    alphabet = "abcXYZ019-_.@/+ :äé\\\"'"
    for _ in range(count):
        iss = rng.choice(issuers)
        kind = rng.randrange(5)
        sub: object
        if kind == 0:
            sub = "".join(
                rng.choice(alphabet) for _ in range(rng.randrange(1, 24))
            )
        elif kind == 1:
            sub = rng.randrange(-(2 ** 70), 2 ** 70)
        elif kind == 2:
            sub = rng.uniform(-1e6, 1e6)
        elif kind == 3:
            sub = rng.choice([True, False])
        else:
            sub = rng.choice(_COLLISION_SET)
        yield iss, sub


def test_decode_encode_roundtrip_property_fuzzed() -> None:
    """``decode(encode(x)) == x`` — exactly, including the sub's TYPE."""
    from agent_mcp.router.sso import SsoSubject

    for iss, sub in _fuzz_pairs():
        original = SsoSubject(iss, sub)
        decoded = SsoSubject.decode(original.encode())
        assert decoded == original, (iss, sub, original.encode())
        assert type(decoded.sub) is type(original.sub)


def test_encode_is_injective_over_the_collision_set() -> None:
    """R18-F1 as a property: no two distinct typed subjects may share a
    persisted key. Pre-fix, ``True``/``"True"`` collapsed to one."""
    from agent_mcp.router.sso import SsoSubject

    keys = [SsoSubject(_ISS, sub).encode() for sub in _COLLISION_SET]
    assert len(set(keys)) == len(keys)


def test_decode_never_invents_a_subject_that_reencodes_differently() -> None:
    """Totality guard: for ANY input string, ``decode`` either refuses
    or returns a subject that re-encodes to the identical bytes. A
    stored row can therefore never be mis-attributed to a subject that
    would have been persisted under a different key."""
    from agent_mcp.router.sso import SsoSubject

    candidates = [
        "", "oidc:", "oidc::", "oidc:iss:", "oidc:iss:str:",
        "oidc:iss:str:x", "oidc:iss:int:007", "oidc:iss:int:1",
        "oidc:iss:float:1", "oidc:iss:float:1.0", "oidc:iss:bool:true",
        "oidc:iss:bool:True", "oidc:iss:dict:{}", "proxy:someone",
        "oidc:https://a:str:str:b", "not-a-subject-at-all",
    ]
    candidates += [SsoSubject(i, s).encode() for i, s in _fuzz_pairs(count=120)]
    for raw in candidates:
        decoded = SsoSubject.decode(raw)
        assert decoded is None or decoded.encode() == raw, raw


def test_decode_rejects_a_non_oidc_namespace() -> None:
    """The proxy-header key space (``proxy:``) is deliberately disjoint
    and is not an OIDC subject."""
    from agent_mcp.router.sso import SsoSubject

    assert SsoSubject.decode("proxy:someone@corp") is None


# ── R18-F1: the exact repro, through the typed path ────────────────


@pytest.mark.parametrize(
    ("typed_sub", "str_sub"),
    [(True, "True"), (False, "False"), (1, "1"), (1.0, "1.0"), (-42, "-42")],
)
def test_r18f1_distinct_claim_types_never_share_a_key(
    typed_sub: object, str_sub: str,
) -> None:
    """R18-F1 (#708): ``str(True) == "True"``, ``str(1) == "1"`` — a
    bare interpolation let a SECOND, genuinely distinct OIDC claimant
    reconcile into the FIRST claimant's account."""
    from agent_mcp.router.sso import SsoSubject

    typed = SsoSubject.from_claims(_ISS, typed_sub)
    as_str = SsoSubject.from_claims(_ISS, str_sub)
    assert typed is not None and as_str is not None
    assert typed.encode() != as_str.encode()


def test_r18f1_int_and_float_stay_distinct() -> None:
    from agent_mcp.router.sso import SsoSubject

    assert SsoSubject(_ISS, 1).encode() != SsoSubject(_ISS, 1.0).encode()


# ── (c) legacy-format matching + (d) ambiguity refusal ─────────────


def test_r19f1_legacy_lookup_key_reproduces_the_untagged_format() -> None:
    """R19-F1 (#709): the fallback key must be the EXACT pre-R18-F1
    shape — it exists only to match what an old row already stored."""
    from agent_mcp.router.sso import SsoSubject

    assert SsoSubject(_ISS, "abc-123").legacy_lookup_key() == (
        "oidc:https://idp.example.test:abc-123"
    )


@pytest.mark.parametrize("sub", [1, 1.0, True, False, -42, 2 ** 70])
def test_r20f1_every_non_str_sub_is_unconditionally_ambiguous(
    sub: object,
) -> None:
    """R20-F1 (#710), direction 1: an untagged legacy key can't record
    the type it was minted from, and a hypothetical ``str`` sub of the
    same content always stringifies to itself — so a non-``str`` sub
    can NEVER safely claim a legacy row."""
    from agent_mcp.router.sso import SsoSubject

    subject = SsoSubject(_ISS, sub)
    assert subject.is_ambiguous() is True
    assert subject.legacy_lookup_key() is None


@pytest.mark.parametrize(
    ("sub", "ambiguous"),
    [
        ("1", True), ("-42", True), ("0", True),
        ("1.5", True), ("1.0", True),
        ("True", True), ("False", True),
        ("alice-sub-1", False), ("abc-123", False),
        ("007", False),        # not a canonical int repr
        ("1.50", False),       # not a canonical float repr
        ("true", False),       # bool repr is capitalised
        ("   ", False),
    ],
)
def test_r20f1_str_sub_is_ambiguous_iff_numeric_or_bool_shaped(
    sub: str, ambiguous: bool,
) -> None:
    """R20-F1, direction 2 (the mirror case the fix agent confirmed
    genuinely matters): a ``str`` claimant must not hijack a legacy row
    that could equally have been minted from a same-content
    int/float/bool sub."""
    from agent_mcp.router.sso import SsoSubject

    subject = SsoSubject(_ISS, sub)
    assert subject.is_ambiguous() is ambiguous
    assert (subject.legacy_lookup_key() is None) is ambiguous


def test_r20f1_ambiguity_refusal_never_touches_the_current_key() -> None:
    """Withholding the legacy fallback must not weaken the CURRENT
    tagged key — an ambiguous sub still reconciles normally via (a)."""
    from agent_mcp.router.sso import SsoSubject

    subject = SsoSubject(_ISS, 1)
    assert subject.legacy_lookup_key() is None
    assert subject.encode() == "oidc:https://idp.example.test:int:1"


# ── the reconciliation call path, through the typed value ──────────


def _seed_row(username: str, sso_subject: str, *, sysadmin: bool = False) -> str:
    from agent_mcp.router import identity

    identity.run_router_migrations_upgrade()
    return identity.create_user(
        username=username,
        password=None,
        email=f"{username}@example.test",
        password_hash=None,
        is_sysadmin=sysadmin,
        sso_subject=sso_subject,
    )


def test_r19f1_legacy_row_reconciles_and_self_heals(router_env) -> None:
    """R19-F1 repro through the typed path: a pre-existing row carrying
    the OLD untagged key must still be FOUND (not orphaned into a fresh
    JIT row) and must be re-stamped to the current tagged format."""
    from agent_mcp.router import identity, sso
    from agent_mcp.router.sso import SsoSubject

    legacy_key = f"oidc:{_ISS}:alice-sub-1"
    user_id = _seed_row("alice", legacy_key, sysadmin=True)

    subject = SsoSubject.from_claims(_ISS, "alice-sub-1")
    assert subject is not None
    row = sso.find_or_create_sso_user(
        email="alice@example.test",
        preferred_username="alice",
        subject=subject.encode(),
        legacy_subject=subject.legacy_lookup_key(),
        email_verified=True,
    )

    assert row["user_id"] == user_id, "must reconcile, not JIT-create"
    assert identity.get_user_by_username("alice-2") is None
    healed = identity.get_user_by_id(user_id)
    assert healed is not None
    assert healed["sso_subject"] == subject.encode(), "row must self-heal"
    assert healed["is_sysadmin"] in (1, True)


def test_r20f1_differently_typed_claimant_cannot_take_over(router_env) -> None:
    """R20-F1 repro through the typed path: a brand-new ``sub=1`` (int)
    login must NOT reconcile into — nor re-stamp — a legacy row keyed
    ``oidc:<iss>:1``. It gets its own unprivileged row instead."""
    from agent_mcp.router import identity, sso
    from agent_mcp.router.sso import SsoSubject

    legacy_key = f"oidc:{_ISS}:1"
    victim_id = _seed_row("victim", legacy_key, sysadmin=True)

    subject = SsoSubject.from_claims(_ISS, 1)
    assert subject is not None
    assert subject.legacy_lookup_key() is None
    row = sso.find_or_create_sso_user(
        email=None,
        preferred_username="mallory",
        subject=subject.encode(),
        legacy_subject=subject.legacy_lookup_key(),
    )

    assert row["user_id"] != victim_id
    assert not (row["is_sysadmin"] == 1 or row["is_sysadmin"] is True)
    victim_after = identity.get_user_by_id(victim_id)
    assert victim_after is not None
    assert victim_after["sso_subject"] == legacy_key, (
        "the victim row must not have been retagged by another claimant"
    )
    assert victim_after["last_login_at"] is None
