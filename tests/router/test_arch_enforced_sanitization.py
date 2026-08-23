"""N1 architecture invariant: no raw decode of untrusted request bytes.

Eleven findings across nine ``pentest-all`` rounds — R3-F1, R4-F3,
R4-F4, R5-F8, R5-F9, R13-F2, R14-F3, R15-F1, R15-F2, R16-F3, R20-F2 —
rediscovered the SAME shape: untrusted bytes became Python data at a
decode point that did not route through ``utils.json_utils``'s
sanitizer, so C0/C1/DEL control bytes, zero-width spaces, bidi
overrides, variation selectors or lone surrogates reached persistence
and the dashboard unstripped. Every one of those fixes widened WHAT the
sanitizer strips (``_CONTROL_BYTE_RE`` → +C1 → +``Cf`` by category →
+``Cs``) or WHEN it runs (the malformed-JSON fallback → every parse
path → already-decoded dicts). Not one of them made SKIPPING the
sanitizer structurally impossible — so each new decode point someone
added inherited nothing, and the class recurred.

``json_utils.decode_untrusted_body`` (added for N1) is the single entry
point for "turn untrusted request bytes into Python data". This file is
what makes it unskippable: it AST-walks every request-handling module
and fails on any raw decode call that is not routed through the seam
and is not on the ``_DECLARED_EXEMPTIONS`` table below.

Idiom deliberately mirrors ``test_arch_enforced_revalidation.py`` (the
OBS-R11-1 backstop, made dynamic by Finding G): discover the targets by
walking the package directory and AST-parsing each file, rather than
hardcoding a module allowlist that a future file silently falls outside
of. The hand-maintained part here is the EXEMPTION table — which is the
right thing to maintain by hand, because each entry is a security
judgement someone made on purpose. ``test_no_stale_declared_exemptions``
keeps that table honest: an exemption whose call site has moved,
changed API, or been fixed fails loudly instead of quietly widening the
allowlist.

What counts as a "raw decode" (the decode APIs this codebase actually
uses — enumerated from a survey of ``agent_mcp/router/`` +
``agent_mcp/app/``, not guessed):

  * ``json.loads(...)`` / ``json.load(...)`` — the C decoder, with no
    sanitization of any kind.
  * ``<x>.json()`` — aiohttp's ``ClientResponse.json`` and httpx's
    ``Response.json``; both decode a remote peer's bytes.
  * ``await <x>.post()`` — aiohttp's form-body decoder. Only the
    AWAITED form counts: a bare ``router.post("/path")`` is a FastAPI
    route decorator, of which there are ~20 in ``agent_mcp/app/routers/``
    and none of them decode anything.

``json_utils.py`` itself is deliberately OUT of scope — it is the
sanitizer, so its own ``json.loads`` calls are the implementation the
seam is made of, not a bypass of it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

# Every function below is a plain synchronous AST walk and must NOT
# inherit an asyncio mark.

#: Package directories scanned for raw decodes. Both are request
#: surfaces: ``router/`` is the aiohttp front door (admin REST, login,
#: SSO, the project-lifecycle API, the backend proxy) and ``app/`` is
#: the per-project FastAPI/ASGI backend (``main_app.py``'s ``/mcp``
#: transport plus the ``routers/`` REST tier).
_SCANNED_PACKAGES = ("agent_mcp.router", "agent_mcp.app")

#: The one entry point every request-handling module must decode
#: through. Named here (rather than only in the failure message) so a
#: rename of the seam breaks this test instead of silently disabling it
#: — see ``test_the_named_seam_exists``.
_SEAM = "decode_untrusted_body"


def _package_dir(dotted: str) -> Path:
    """Filesystem directory backing ``dotted``, resolved via the
    package's own ``__file__`` (not a path relative to this test file)
    so discovery is correct regardless of where pytest runs from."""
    import importlib

    mod = importlib.import_module(dotted)
    return Path(mod.__file__).parent


def _agent_mcp_root() -> Path:
    import agent_mcp

    return Path(agent_mcp.__file__).parent


def _scanned_files() -> list[Path]:
    """Every ``.py`` file under the scanned packages, recursively.

    Dynamic on purpose (Finding G's lesson applied to this detector): a
    NEW module dropped into ``agent_mcp/router/`` or
    ``agent_mcp/app/routers/`` is scanned automatically. The
    hand-maintained list in this file is the exemption table, not the
    target list.
    """
    seen: dict[Path, None] = {}
    for dotted in _SCANNED_PACKAGES:
        for path in sorted(_package_dir(dotted).rglob("*.py")):
            seen[path] = None
    return list(seen)


def _relkey(path: Path) -> str:
    """``router/app.py``-style key, relative to the ``agent_mcp``
    package root — stable across checkouts and readable in an
    exemption table."""
    return path.relative_to(_agent_mcp_root()).as_posix()


#: ``{(module_relpath, enclosing_function, decode_api): reason}``.
#:
#: Every entry is a decode point that is NOT an untrusted HTTP request
#: body, or is one this PR deliberately left alone. Adding an entry is
#: a security decision: state which trust boundary the bytes crossed
#: and why the hidden-Unicode strip does not belong there.
_DECLARED_EXEMPTIONS: dict[tuple[str, str, str], str] = {
    # ── Not a request body: server-owned or operator-owned bytes ──
    ("router/sso.py", "_parse_group_mapping", "json.loads"): (
        "AGENT_MCP_SSO_OIDC_GROUP_MAPPING — an environment variable set "
        "by the operator deploying the router, not anything a client "
        "sends. Same trust tier as the rest of the process environment."
    ),
    ("router/project_registry.py", "_parse_or_recover", "json.loads"): (
        "The router's own projects.json on disk, written by this "
        "process. Project names reaching it are already sanitized at "
        "ingress by ``router.app._parse_json_body`` (N1) and by "
        "``project_registry``'s own name validator; re-stripping on "
        "every read would silently rewrite persisted state instead of "
        "rejecting it."
    ),
    ("app/server_lifecycle.py", "application_startup", "json.loads"): (
        "A TEXT column read back out of this project's own SQLite DB "
        "(a JSON-encoded list field), not request bytes. Values were "
        "sanitized on the way in via the tool/REST decode paths."
    ),
    ("app/main_app.py", "_sanitize_jsonrpc_error_body", "json.loads"): (
        "Decodes THIS SERVER'S OWN outbound JSON-RPC error envelope on "
        "the way out, in order to rewrite the SDK's leaky pydantic "
        "message into a terse one (SEC-1). Nothing client-supplied is "
        "parsed here, and the result is re-serialised, never persisted."
    ),
    ("router/app.py", "_agent_token_map", "<response>.json()"): (
        "The response body of this router's own backend, fetched over a "
        "per-project unix socket the router itself created. Same "
        "process tree, same trust tier; the values (agent tokens and "
        "agent ids) are opaque identifiers matched exactly, never "
        "rendered."
    ),
    # ── IdP responses: consumed as configuration / crypto material ──
    ("router/sso.py", "_fetch_oidc_metadata", "<response>.json()"): (
        "The OIDC discovery document, fetched from the IdP URL the "
        "operator configured. Consumed as endpoint URLs handed to "
        "authlib, never persisted or rendered. Stripping hidden-format "
        "characters out of a URL would silently repair a malformed "
        "endpoint rather than failing on it."
    ),
    ("router/sso.py", "_decode_id_token", "<response>.json()"): (
        "The IdP's JWKS. Pure crypto material (base64url key "
        "components) handed straight to authlib for signature "
        "verification — a sanitizing pass over it could only corrupt a "
        "key or mask a malformed one."
    ),
    # ── Deliberately deferred to another workstream ──
    ("router/sso.py", "_decode_flow_cookie", "json.loads"): (
        "IS an untrusted decode (the flow cookie is unsigned "
        "base64(JSON) and therefore attacker-craftable) and IS in "
        "scope for N1 — deliberately DEFERRED, not missed. Phase 3 of "
        "the same hardening plan (Finding C, the ``SsoSubject`` value "
        "type) is reworking ``sso.py`` heavily; fixing it here would "
        "collide with that work. The blast radius is small in the "
        "meantime: the three decoded fields (state / verifier / nonce) "
        "are compared for equality against server-generated values and "
        "never persisted or rendered, so hidden-format characters can "
        "only make a flow fail closed. Phase 3 must route this through "
        f"``json_utils.{_SEAM}`` and delete this entry."
    ),
    # ── Form-encoded credential bodies: DECLARED EXEMPTION ──
    #
    # These two are the "do form bodies join the same seam?" scope
    # question N1 posed. Answer: no — and specifically because of the
    # PASSWORD field. See ``tests/router/test_arch_n1_form_credentials.py``
    # for the tests that pin this decision rather than leaving it to
    # silence.
    ("router/login.py", "login_post_handler", "<request>.post()"): (
        "Form-encoded credentials. A password is a byte-for-byte "
        "secret compared against an argon2 hash — unicode-stripping it "
        "at LOGIN while ``create_user`` stored the unstripped original "
        "would lock the account out, and stripping at BOTH ends would "
        "silently collapse distinct passwords onto one. The username "
        "is likewise used for an EXACT lookup only "
        "(``get_user_by_username``): stripping it here would let "
        "``ad<ZWSP>min`` authenticate as ``admin``, which is a "
        "WIDENING, the opposite of what the sanitizer is for. Identity "
        "fields are instead sanitized where they are PERSISTED, in "
        "``identity.create_user``. Pinned by "
        "tests/router/test_arch_n1_form_credentials.py."
    ),
    ("router/setup_wizard.py", "setup_post_handler", "<request>.post()"): (
        "Same reasoning as ``login.login_post_handler`` above. This "
        "handler's username/email DO get stripped — but at the "
        "persistence seam (``identity.create_user``), not at the form "
        "parse, so the stored value is canonical while the submitted "
        "password stays byte-exact. Pinned by "
        "tests/router/test_arch_n1_form_credentials.py."
    ),
}


def _call_attr_chain(func: ast.AST) -> str | None:
    """``json.loads`` for ``json.loads(...)``; ``json`` for a bare
    ``json(...)``; ``None`` for anything not a simple name/attribute."""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        base = _call_attr_chain(func.value)
        return f"{base}.{func.attr}" if base else func.attr
    return None


def _classify_decode(node: ast.AST, awaited: set[int]) -> str | None:
    """Name of the raw-decode API ``node`` is, or ``None``.

    ``awaited`` holds ``id()``s of ``ast.Call`` nodes that are the
    direct operand of an ``await`` — needed to tell aiohttp's
    ``await request.post()`` (a form-body decode) from FastAPI's
    ``@router.post("/path")`` (a route decorator, never awaited).
    """
    if not isinstance(node, ast.Call):
        return None
    chain = _call_attr_chain(node.func)
    if chain in ("json.loads", "json.load"):
        return "json.loads" if chain == "json.loads" else "json.load"
    if not isinstance(node.func, ast.Attribute):
        return None
    if node.func.attr == "json":
        return "<response>.json()"
    if node.func.attr == "post" and id(node) in awaited:
        return "<request>.post()"
    return None


def _enclosing_functions(tree: ast.Module) -> dict[int, str]:
    """``{id(node): enclosing_function_name}`` for every node inside a
    (possibly nested) function definition. The INNERMOST enclosing
    function wins, which is what an exemption entry should name."""
    owner: dict[int, str] = {}
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            # Walk order is outer-to-inner across the module, so a later
            # (inner) function overwrites the outer one's claim.
            owner[id(node)] = fn.name
    return owner


def _awaited_call_ids(tree: ast.Module) -> set[int]:
    return {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Await) and isinstance(node.value, ast.Call)
    }


def _decode_sites_in(path: Path) -> list[tuple[str, str, str, int]]:
    """``[(module_relpath, function, decode_api, lineno), ...]``."""
    tree = ast.parse(path.read_text())
    owner = _enclosing_functions(tree)
    awaited = _awaited_call_ids(tree)
    key = _relkey(path)
    out: list[tuple[str, str, str, int]] = []
    for node in ast.walk(tree):
        api = _classify_decode(node, awaited)
        if api is None:
            continue
        out.append((key, owner.get(id(node), "<module>"), api, node.lineno))
    return out


def _all_decode_sites() -> list[tuple[str, str, str, int]]:
    sites: list[tuple[str, str, str, int]] = []
    for path in _scanned_files():
        sites.extend(_decode_sites_in(path))
    return sorted(sites)


_DECODE_SITES = _all_decode_sites()


def test_the_named_seam_exists() -> None:
    """Guard on the detector's own premise: if the seam is renamed or
    deleted, this file's failure messages would point at nothing and
    the exemption reasons would be lies. Fail here instead."""
    from agent_mcp.utils import json_utils

    assert hasattr(json_utils, _SEAM), (
        f"json_utils.{_SEAM} — the single decode seam this whole test "
        f"file exists to make unskippable — is gone. If it was renamed, "
        f"update _SEAM and every reference to it in the exemption "
        f"table's reasons."
    )


def test_detector_is_not_vacuous() -> None:
    """Sanity guard on the detector itself, mirroring
    ``test_arch_enforced_revalidation.py``'s
    ``test_discovery_found_the_known_call_sites``: if a future refactor
    (a new decode API, a changed AST shape) silently makes
    ``_all_decode_sites`` find nothing, ``test_no_undeclared_raw_decode``
    below would "pass" without checking anything — worse than not
    having it. Pin that the known exempt sites are all still FOUND."""
    found = {(mod, fn, api) for mod, fn, api, _ in _DECODE_SITES}
    missing = set(_DECLARED_EXEMPTIONS) - found
    assert not missing, (
        f"the raw-decode detector no longer finds {sorted(missing)!r}, "
        f"which the exemption table says exist. Either those call sites "
        f"changed (update/remove the exemption — see "
        f"test_no_stale_declared_exemptions) or the AST matcher in "
        f"_classify_decode stopped matching an API it used to, which "
        f"would make this whole file vacuous."
    )
    assert len(found) >= len(_DECLARED_EXEMPTIONS), (
        f"detector found only {len(found)} decode site(s) across "
        f"{len(_scanned_files())} scanned files — implausibly few."
    )


def test_no_undeclared_raw_decode() -> None:
    """THE invariant. Any raw decode of bytes in a request-handling
    module must either route through the shared seam or be a declared,
    reasoned exemption."""
    violations = [
        (mod, fn, api, line)
        for mod, fn, api, line in _DECODE_SITES
        if (mod, fn, api) not in _DECLARED_EXEMPTIONS
    ]
    assert not violations, (
        "raw decode of untrusted bytes outside the shared sanitization "
        "seam:\n"
        + "\n".join(
            f"  agent_mcp/{mod}:{line} in {fn}() — {api}"
            for mod, fn, api, line in violations
        )
        + f"\n\nThis is the N1 recurring class (R3-F1/R4-F3/R4-F4/R5-F8/"
        f"R5-F9/R13-F2/R14-F3/R15-F1/R15-F2/R16-F3/R20-F2): every one of "
        f"those findings was a decode point that skipped "
        f"``utils.json_utils``'s hidden-Unicode/control-byte strip.\n"
        f"Route the body through ``json_utils.{_SEAM}`` instead of "
        f"decoding it raw. If these bytes are NOT an untrusted request "
        f"body (server-owned state, this process's own output, "
        f"operator-set configuration), add an entry to "
        f"_DECLARED_EXEMPTIONS in this file saying which trust boundary "
        f"they crossed and why the strip does not belong there."
    )


def test_no_stale_declared_exemptions() -> None:
    """Keeps the hand-maintained half honest. An exemption whose call
    site was renamed, moved, changed decode API, or fixed must be
    removed — a stale entry is an allowlist that silently widens (the
    exact opt-in-and-forget failure mode this file exists to prevent,
    recurring in the detector's own plumbing)."""
    found = {(mod, fn, api) for mod, fn, api, _ in _DECODE_SITES}
    stale = sorted(set(_DECLARED_EXEMPTIONS) - found)
    assert not stale, (
        f"declared exemption(s) with no matching call site: {stale!r}. "
        f"If the decode was fixed (routed through the seam) or moved, "
        f"delete/update the entry — leaving it behind pre-approves a "
        f"future decode that happens to land on the same "
        f"(module, function, api) triple."
    )


def test_every_exemption_carries_a_real_reason() -> None:
    """A one-word reason is not a security decision. Each entry must
    say enough for a reviewer to disagree with it."""
    for key, reason in _DECLARED_EXEMPTIONS.items():
        assert len(reason.split()) >= 15, (
            f"exemption {key!r} has a {len(reason.split())}-word reason. "
            f"Say which trust boundary the bytes crossed and why the "
            f"hidden-Unicode strip does not belong there."
        )


def test_no_module_bypasses_the_matcher_via_a_direct_json_import() -> None:
    """``from json import loads`` would produce a bare ``loads(...)``
    call that ``_classify_decode``'s ``json.loads`` attribute match
    cannot see. No scanned module does this today; assert it stays that
    way rather than leaving a silent hole in the detector."""
    offenders = []
    for path in _scanned_files():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "json"
                and any(a.name in ("loads", "load") for a in node.names)
            ):
                offenders.append(f"{_relkey(path)}:{node.lineno}")
    assert not offenders, (
        f"{offenders!r} import json.loads/json.load directly, which "
        f"produces a bare ``loads(...)`` call the AST matcher in "
        f"_classify_decode does not recognise. Use "
        f"``json_utils.{_SEAM}`` for request bodies, or ``import json`` "
        f"+ ``json.loads`` for anything genuinely exempt, so this "
        f"detector can still see it."
    )


# ── Synthetic shape tests for the detector itself ──────────────────
#
# These exercise ``_classify_decode`` directly, independent of any real
# module, so a change to the matcher's behaviour fails here with a
# readable diff rather than as a mysterious shift in the violation list.


def _classify_source(src: str) -> list[str]:
    tree = ast.parse(src)
    awaited = _awaited_call_ids(tree)
    return [
        api
        for node in ast.walk(tree)
        if (api := _classify_decode(node, awaited)) is not None
    ]


def test_detector_flags_a_bare_json_loads_body_read() -> None:
    """The pre-N1 ``router.app._parse_json_body`` shape."""
    src = (
        "async def handler(req):\n"
        "    raw = await req.read()\n"
        "    return json.loads(raw)\n"
    )
    assert _classify_source(src) == ["json.loads"]


def test_detector_clears_a_body_read_routed_through_the_seam() -> None:
    """The post-N1 shape — no violation."""
    src = (
        "async def handler(req):\n"
        "    raw = await req.read()\n"
        "    return decode_untrusted_body(raw)\n"
    )
    assert _classify_source(src) == []


def test_detector_flags_an_awaited_form_post() -> None:
    src = (
        "async def handler(request):\n"
        "    form = await request.post()\n"
    )
    assert _classify_source(src) == ["<request>.post()"]


def test_detector_ignores_a_fastapi_post_route_decorator() -> None:
    """~20 of these live in ``agent_mcp/app/routers/``; none decode
    anything. Flagging them would drown the real signal."""
    src = (
        '@router.post("/settings")\n'
        "async def create_setting(request):\n"
        "    return None\n"
    )
    assert _classify_source(src) == []


def test_detector_flags_a_response_json_call() -> None:
    src = "async def f(sess):\n    body = await sess.get(u).json()\n"
    assert _classify_source(src) == ["<response>.json()"]


@pytest.mark.parametrize(
    "mod, fn, api",
    sorted(_DECLARED_EXEMPTIONS),
    ids=lambda v: v if isinstance(v, str) else str(v),
)
def test_declared_exemption_is_reachable_in_source(
    mod: str, fn: str, api: str,
) -> None:
    """Per-entry version of ``test_no_stale_declared_exemptions``, so a
    single rotten entry names itself in the pytest report instead of
    hiding inside one aggregate assertion."""
    found = {(m, f, a) for m, f, a, _ in _DECODE_SITES}
    assert (mod, fn, api) in found
