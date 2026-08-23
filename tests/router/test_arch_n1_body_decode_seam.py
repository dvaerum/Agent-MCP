"""N1 (security-arch hardening, pass 2): the three request-body decode
wrappers must be one seam, not three lookalikes.

Eleven findings across nine ``pentest-all`` rounds (R3-F1, R4-F3, R4-F4,
R5-F8, R5-F9, R13-F2, R14-F3, R15-F1, R15-F2, R16-F3, R20-F2) all had
the same shape: untrusted bytes became Python data at a decode point
that did not route through ``utils.json_utils``'s sanitizer. Every fix
widened WHAT the sanitizer strips or WHEN it runs; none made skipping it
structurally impossible.

This file is the *behavioural* half of that fix (the structural half is
``test_arch_enforced_sanitization.py``, an AST discovery test). It pins
that the three historical wrappers —

  * ``json_utils.get_sanitized_json_body``  (Starlette/FastAPI tier,
    ~26 call sites across ``agent_mcp/app/routers/*``),
  * ``admin_users_api._json_body``          (aiohttp router tier),
  * ``router.app._parse_json_body``         (aiohttp router tier),

— agree, leaf-for-leaf, on what a body decodes to, because they are all
thin call-throughs to the SAME entry point
(``json_utils.decode_untrusted_body``).

RED state before the fix: ``_parse_json_body`` did a bare
``json.loads(raw)``, so every hidden-Unicode case below came back
unstripped from it while the other two stripped it. That is the live
bypass behind the project ``name`` field on POST /api/projects and
PATCH /api/projects/<name> (``admin_api.create_project_handler`` /
``rename_project_handler``) — the value that becomes the project slug
on disk and the label rendered in the dashboard.

Deliberately NOT unified (verified differences, kept on purpose — each
is an API contract of its tier, not a sanitization property):

  * empty body: the aiohttp tier returns ``{}`` (several POSTs take no
    fields); the Starlette tier raises. Left at the call sites, not
    pushed into the seam, so the difference stays visible where it is
    decided.
  * error envelope: ``_parse_json_body`` reports ``invalid_json``,
    ``_json_body`` reports ``validation_error``, and
    ``get_sanitized_json_body`` raises ``ValueError`` for the FastAPI
    handlers to map. Changing any of those is a client-visible API
    change, out of scope for a mechanism-only refactor.
"""

from __future__ import annotations

import unicodedata

import pytest
from aiohttp import web

from agent_mcp.router import admin_users_api
from agent_mcp.utils.json_utils import (
    UntrustedBodyError,
    decode_untrusted_body,
    get_sanitized_json_body,
)


class _StarletteLikeRequest:
    """Minimal stand-in for the object ``get_sanitized_json_body`` takes
    (anything with an awaitable ``.body()``)."""

    def __init__(self, raw: bytes) -> None:
        self._raw = raw

    async def body(self) -> bytes:
        return self._raw


class _AiohttpLikeRequest:
    """Minimal stand-in for ``web.Request`` as the two aiohttp-tier
    wrappers use it (they only ever call ``await req.read()``)."""

    def __init__(self, raw: bytes) -> None:
        self._raw = raw

    async def read(self) -> bytes:
        return self._raw


#: Bodies whose decoded value must come out IDENTICAL from all three
#: wrappers. Every entry is legal JSON that ``json.loads`` accepts
#: as-is — the point is what survives INTO the returned dict, not
#: whether it parses.
_EQUIVALENCE_BODIES = [
    pytest.param(b'{"name": "plain"}', id="plain"),
    # R13-F2/R14-F3: zero-width space + RTL override.
    pytest.param(
        '{"name": "ad\u200bmin\u202egnp.txt"}'.encode(),
        id="zero-width-and-bidi",
    ),
    # R4-F3/R5-F8: escaped C0 (ESC) and raw C1 (CSI) control bytes.
    pytest.param(b'{"name": "a\x1b[31mred"}', id="escaped-c0"),
    pytest.param('{"name": "a\x9b31mred"}'.encode(), id="raw-c1"),
    # R15-F1: lone unpaired UTF-16 surrogate (category Cs).
    pytest.param(rb'{"name": "lone\ud800surrogate"}', id="lone-surrogate"),
    # R5-F9: variation selector.
    pytest.param('{"name": "vs\ufe0fhidden"}'.encode(), id="variation-selector"),
    # R14-F3: zalgo run gets capped, not removed.
    pytest.param(
        ('{"name": "a' + "\u0301" * 40 + '"}').encode(), id="combining-run",
    ),
    # Ordinary printable non-Latin text must survive UNCHANGED — the
    # sanitizer targets hidden-format/bidi/zero-width classes only, so
    # widening the seam to new fields cannot break a project named in a
    # non-Latin script or a username with accented characters.
    pytest.param('{"name": "プロジェクト"}'.encode(),
                 id="japanese"),
    pytest.param('{"name": "café-résumé"}'.encode(), id="accented"),
    pytest.param('{"name": "مشروع"}'.encode(),
                 id="arabic"),
    # Nesting: the strip must reach leaves at depth, not just the top level.
    pytest.param(
        '{"outer": {"inner": ["a\u200bb", {"k": "c\u202dd"}]}}'.encode(),
        id="nested-leaves",
    ),
    # Whitespace that must NOT be stripped (tab/LF/CR are legitimate).
    pytest.param(rb'{"desc": "line1\nline2\ttabbed\r\n"}', id="legit-whitespace"),
]

#: Hidden/format Unicode categories the shared sanitizer strips outright
#: (mirrors ``json_utils._HIDDEN_FORMAT_CATEGORIES`` — asserted equal in
#: ``test_forbidden_category_set_matches_the_sanitizer`` below so this
#: copy can never drift into vacuously passing).
_FORBIDDEN_CATEGORIES = {"Cf", "Zl", "Zp", "Cs"}


def test_forbidden_category_set_matches_the_sanitizer() -> None:
    from agent_mcp.utils import json_utils

    assert set(json_utils._HIDDEN_FORMAT_CATEGORIES) == _FORBIDDEN_CATEGORIES


def _wrappers(router_module) -> dict:
    """The three historical wrappers, uniformly callable as
    ``await fn(raw_bytes) -> dict``.

    ``router.app`` is imported through the ``router_module`` fixture
    rather than at module scope: it reads a fistful of env vars at
    import time (see ``tests/router/conftest.py``).
    """

    async def via_get_sanitized_json_body(raw: bytes) -> dict:
        return await get_sanitized_json_body(_StarletteLikeRequest(raw))

    async def via_admin_users_json_body(raw: bytes) -> dict:
        return await admin_users_api._json_body(_AiohttpLikeRequest(raw))

    async def via_router_parse_json_body(raw: bytes) -> dict:
        return await router_module._parse_json_body(_AiohttpLikeRequest(raw))

    return {
        "get_sanitized_json_body": via_get_sanitized_json_body,
        "admin_users_api._json_body": via_admin_users_json_body,
        "router.app._parse_json_body": via_router_parse_json_body,
    }


def _string_leaves(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _string_leaves(v)
    elif isinstance(value, list):
        for v in value:
            yield from _string_leaves(v)


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", _EQUIVALENCE_BODIES)
async def test_all_three_wrappers_decode_identically(
    router_module, raw: bytes,
) -> None:
    """The property this whole finding is about: three wrappers, one
    decode result. Historically ``_parse_json_body`` disagreed with the
    other two on every hidden-Unicode case."""
    results = {
        name: await fn(raw) for name, fn in _wrappers(router_module).items()
    }
    reference = results["get_sanitized_json_body"]
    for name, value in results.items():
        assert value == reference, (
            f"{name} decoded {raw!r} to {value!r}, but "
            f"get_sanitized_json_body decoded it to {reference!r}. The "
            f"three body-decode wrappers must be thin call-throughs to "
            f"json_utils.decode_untrusted_body so they cannot disagree "
            f"about what an untrusted body means (N1)."
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", _EQUIVALENCE_BODIES)
async def test_no_wrapper_returns_hidden_format_characters(
    router_module, raw: bytes,
) -> None:
    """Complements the equivalence check above: three wrappers agreeing
    on an UNSANITIZED value would also pass that test. Assert the shared
    result actually carries none of the hidden-format classes the
    sanitizer targets."""
    for name, fn in _wrappers(router_module).items():
        decoded = await fn(raw)
        for leaf in _string_leaves(decoded):
            bad = [
                ch for ch in leaf
                if unicodedata.category(ch) in _FORBIDDEN_CATEGORIES
            ]
            assert not bad, (
                f"{name} returned hidden-format character(s) "
                f"{[hex(ord(c)) for c in bad]} in {leaf!r} from body "
                f"{raw!r}"
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw, expected",
    [
        ('{"name": "プロジェクト"}'.encode(),
         "プロジェクト"),
        ('{"name": "café-résumé"}'.encode(), "café-résumé"),
        ('{"name": "مشروع"}'.encode(), "مشروع"),
        ('{"name": "Tiếng Việt"}'.encode(), "Tiếng Việt"),
    ],
    ids=["japanese", "accented", "arabic", "vietnamese"],
)
async def test_ordinary_unicode_survives_the_widened_seam(
    router_module, raw: bytes, expected: str,
) -> None:
    """No-policy-change guard for this refactor: routing the project
    ``name`` (and every other ``_parse_json_body`` field) through the
    sanitizer for the first time must NOT break a legitimately
    non-Latin value. The sanitizer strips hidden-format/zero-width/
    bidi/surrogate classes only — ordinary printable Unicode, including
    the combining marks Vietnamese and Arabic depend on, passes
    through byte-identical."""
    for name, fn in _wrappers(router_module).items():
        decoded = await fn(raw)
        assert decoded["name"] == expected, (
            f"{name} altered ordinary printable Unicode: "
            f"{decoded['name']!r} != {expected!r}"
        )


@pytest.mark.asyncio
async def test_project_name_reaches_handlers_sanitized(router_module) -> None:
    """The concrete live bypass: ``_parse_json_body`` backs
    ``admin_api.create_project_handler`` / ``rename_project_handler``,
    so the project ``name`` — persisted as the on-disk slug and rendered
    in the dashboard project list — used to arrive with its zero-width
    and bidi-override characters intact."""
    body = '{"name": "proj\u200bect\u202e"}'.encode()
    parsed = await router_module._parse_json_body(_AiohttpLikeRequest(body))
    assert parsed["name"] == "project", (
        f"project name arrived as {parsed['name']!r}; the zero-width "
        f"space / RTL override should have been stripped by the shared "
        f"decode seam."
    )


# ── Deliberately-preserved per-tier differences ────────────────────


@pytest.mark.asyncio
async def test_aiohttp_tier_maps_empty_body_to_empty_object(
    router_module,
) -> None:
    """Kept, not unified: several aiohttp POSTs legitimately take no
    fields. This stays at the call sites rather than in the seam."""
    assert await router_module._parse_json_body(_AiohttpLikeRequest(b"")) == {}
    assert await admin_users_api._json_body(_AiohttpLikeRequest(b"")) == {}


@pytest.mark.asyncio
async def test_starlette_tier_rejects_empty_body() -> None:
    """Kept, not unified: the FastAPI tier's callers all immediately
    ``data.get(...)``, and an empty body there has always been a 400."""
    with pytest.raises(ValueError):
        await get_sanitized_json_body(_StarletteLikeRequest(b""))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw", [b"{not json", b'["a", "list"]', b'"a string"', b"42"],
)
async def test_all_three_wrappers_reject_the_same_bodies(
    router_module, raw: bytes,
) -> None:
    """Malformed JSON and non-object top-level bodies are rejected by
    all three — only the error TYPE differs per tier (ValueError for the
    FastAPI handlers to map, ``web.HTTPBadRequest`` for aiohttp)."""
    wrappers = _wrappers(router_module)
    with pytest.raises(ValueError):
        await wrappers["get_sanitized_json_body"](raw)
    for name in ("admin_users_api._json_body", "router.app._parse_json_body"):
        with pytest.raises(web.HTTPBadRequest):
            await wrappers[name](raw)


def test_seam_rejects_non_object_bodies_with_a_client_safe_message() -> None:
    """``decode_untrusted_body`` is the one place that decides what an
    untrusted body means; its exception message is what every tier
    surfaces, so it must never carry interpreter internals."""
    with pytest.raises(UntrustedBodyError) as exc:
        decode_untrusted_body(b'["a", "list"]')
    assert str(exc.value) == "request body must be a JSON object"

    with pytest.raises(UntrustedBodyError) as exc:
        decode_untrusted_body(b"{not json")
    assert "recursion" not in str(exc.value).lower()
    assert str(exc.value).startswith("request body is not valid JSON")


def test_seam_maps_deep_nesting_to_a_terse_message() -> None:
    """PF-R20-1/R20-F3: a body deep enough to trip CPython's own
    recursion guard must surface as a terse 400, never as
    "maximum recursion depth exceeded"."""
    deep = ("[" * 6000) + ("]" * 6000)
    with pytest.raises(UntrustedBodyError) as exc:
        decode_untrusted_body(deep.encode())
    assert "recursion" not in str(exc.value).lower(), str(exc.value)
