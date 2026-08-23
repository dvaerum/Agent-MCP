# Agent-MCP/mcp_template/mcp_server_src/utils/json_utils.py
import json
import re
import unicodedata
from typing import Any, Union, Dict, List

# Import the centrally configured logger
from ..core.config import logger
# For Starlette Request type hint, if you want to be very specific,
# you'd import it, but 'Any' is fine for now if Starlette isn't a direct dependency here.
# from starlette.requests import Request # Example

# Control bytes worth stripping from a *parsed* string value: the C0
# control range excluding tab (\x09), LF (\x0A) and CR (\x0D) — those
# three are legitimate whitespace (e.g. a multi-line task description)
# and must survive — plus DEL (\x7F) and the full C1 range
# (\x80-\x9F). R5-F8: C1 includes CSI (U+009B) and OSC (U+009D), the
# 8-bit single-character equivalents of ESC[ / ESC] — the exact same
# terminal-injection primitive R3-F1/R4-F3 strip via the C0 ESC byte,
# except U+0080-U+009F are ordinary legal JSON string content per RFC
# 8259 (only U+0000-U+001F must be escaped), so they need no ESC byte
# and no JSON escaping to reach a string leaf here. This mirrors
# aoe-bridge/src/render.rs's sanitize_for_pane, which already treats
# C0+DEL+C1 as one control class. This is the exact set Step 3 below
# has always matched; it's hoisted to module scope so both the
# post-parse-success path and the already-a-dict/list path can share
# one definition — keep the two in sync (Step 3 reuses this pattern
# rather than duplicating it, so they cannot drift apart again).
_CONTROL_BYTE_RE = re.compile(r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F-\x9F]')

# Hidden/spoofing Unicode worth stripping from a *parsed* string value.
# R13-F2 originally hand-enumerated zero-width spaces (U+200B-U+200F),
# the BOM (U+FEFF), and the line/paragraph separators (U+2028/U+2029) as
# a single regex hoisted to module scope for the same reason R5-F8
# hoisted _CONTROL_BYTE_RE. R14-F3 widened this: that hand-picked set
# missed the bidi override/embedding/isolate controls (U+202A-U+202E
# LRE/RLE/PDF/LRO/RLO -- RLO is the classic filename/display-spoofing
# primitive that flips the visual order of trailing characters -- and
# U+2066-U+2069 LRI/RLI/FSI/PDI) and the word joiner (U+2060), all of
# which round-tripped byte-for-byte through a live task-title exploit.
#
# Rather than hand-enumerating yet another range and risking the same
# gap again, this now mirrors aoe-bridge/src/render.rs's
# sanitize_for_pane (R4-F4/R5-F9): every character in Unicode general
# category `Cf` (Format) is stripped by *category*, not by range table.
# `Cf` covers all of the above in one check -- LRE/RLE/PDF/LRO/RLO,
# LRI/RLI/FSI/PDI, ZWSP/ZWNJ/ZWJ/LRM/RLM, the word joiner, the Arabic
# Letter Mark, and the BOM -- without needing to know the exact
# codepoints; Python's stdlib `unicodedata` module classifies these
# identically to the Rust `unicode_general_category` crate render.rs
# uses. U+2028/U+2029 (line/paragraph separator) are categories
# `Zl`/`Zp`, not `Cf`, so they're kept as an explicit addition to match
# the original R13-F2 scope (each category has exactly one member, so
# this can't silently grow to catch more than intended).
#
# Variation selectors (U+FE00-U+FE0F, U+E0100-U+E01EF) are category
# `Mn` (Mark, nonspacing), not `Cf` -- render.rs excludes them from its
# `Cf` check for the same reason and lists them explicitly (R5-F9);
# this does the same via _VARIATION_SELECTOR_RANGES below. Like
# render.rs, general combining marks (the rest of `Mn`/`Mc`/`Me`) are
# deliberately NOT stripped outright: legitimate non-Latin text
# (Vietnamese tone marks, Arabic tashkeel, Devanagari vowel signs, ...)
# is built from combining marks in the same Unicode blocks a naive
# strip would hit, and over-stripping would corrupt real content the
# way under-stripping under-protects it. Unlike render.rs -- which
# only ever renders a single ephemeral terminal-nudge line -- this
# sanitizer's output is PERSISTED and rendered indefinitely in the
# dashboard, so an unbounded run of combining marks stacked onto one
# base character ("zalgo text") is a real display-integrity issue here
# that render.rs's scope doesn't have to address. _cap_combining_marks
# below caps (does not remove) runs of Mn/Me marks per base character
# instead of stripping the category outright -- see its docstring for
# the chosen limit and why.
#
# Visually near-indistinguishable strings (e.g. a task title with a
# trailing zero-width space, or one carrying an RTL override) are a
# duplicate-identifier / spoofing risk even though they parse as
# perfectly legal JSON string content.
#
# R15-F1: category `Cs` (Surrogate) is stripped the same way -- a lone/
# unpaired UTF-16 surrogate code point (e.g. from a JSON `\udXXX` escape
# not followed by a valid low-surrogate partner) parses fine under
# `json.loads()` as a real Python string character, but is not valid
# UTF-8 (`'\ud800'.encode('utf-8')` raises `UnicodeEncodeError:
# surrogates not allowed`) and crashes SQLite's TEXT binding with an
# unhandled exception instead of a clean 4xx. A genuine surrogate PAIR
# (e.g. an emoji) is not affected: Python combines a valid pair into a
# single non-`Cs` astral-plane code point before this code ever runs,
# so only the pathological lone/unpaired case is in scope here -- same
# pure-strip treatment as `Cf`, not the cap-not-strip treatment used
# for combining marks, since a lone surrogate has zero legitimate use
# in JSON string content.
#
# R16-F3 CROSS-PR-INTERACTION WARNING for whoever edits this set next:
# this module is a SHARED chokepoint every downstream validation guard
# implicitly depends on. Adding `Cs` here (R15-F1, PR #700) silently
# made a DIFFERENT, already-merged-in-the-same-round PR's own explicit
# validation guard (R15-F2, PR #702's `admin_users_api._reject_unencodable_str`
# and `identity.create_user`'s UTF-8-round-trip check) permanently
# unreachable dead code for the surrogate case -- both PRs were
# developed in isolated worktrees off the same earlier base, each
# passed its own local suite, and the interaction only showed up once
# both had merged into `main` in sequence (caught by R16-F3). Every
# character in Unicode general category `Cs` is the ONLY class of
# character that fails `str.encode("utf-8", "strict")` (exhaustively
# verified over 0x0-0x10FFFF) -- so ANY future widening of this set
# that covers a category some downstream code explicitly checks for
# can have the exact same effect: silently satisfying that check
# upstream and turning it into dead code. Before adding a category
# here, grep the codebase for guards checking the SAME failure mode
# (search for `UnicodeEncodeError`, `.encode("utf-8"`, and
# `InvalidEmailError`) and update or remove them in the SAME change --
# don't let a future round rediscover this the hard way.
_HIDDEN_FORMAT_CATEGORIES = frozenset({"Cf", "Zl", "Zp", "Cs"})

# Variation Selectors 1-16 (BMP) and 17-256 (supplementary plane): carry
# no glyph of their own, only ever modifying/annotating the preceding
# character (or rendering as nothing when unpaired) -- same reasoning
# and same ranges as render.rs's R5-F9 fix.
_VARIATION_SELECTOR_RANGES = ((0xFE00, 0xFE0F), (0xE0100, 0xE01EF))

# R14-F3: cap on consecutive Unicode combining marks (general category
# `Mn`/`Me`) allowed after a single base character. Chosen to
# comfortably clear legitimate stacking depth (Vietnamese needs at most
# 2: a vowel modifier + a tone mark; Arabic tashkeel commonly stacks
# 2-3, e.g. shadda + a vowel; Devanagari vowel-sign combinations rarely
# exceed 2) while still capping "zalgo" text, which stacks tens to
# hundreds of combining marks onto one character specifically to break
# layout/readability. This limits rather than removes the category,
# unlike the Cf/variation-selector strip above, because -- per the
# comment above -- Mn/Me marks carry real, load-bearing glyph content
# for those scripts; only pathological run *lengths* are the actual
# spoofing/layout-DoS primitive, not the category itself.
_MAX_COMBINING_MARKS_PER_BASE = 4


def _is_variation_selector(codepoint: int) -> bool:
    return any(lo <= codepoint <= hi for lo, hi in _VARIATION_SELECTOR_RANGES)


def _strip_hidden_unicode(value: str) -> str:
    """Strip hidden/format Unicode and variation selectors from ``value``
    by general category (R14-F3), replacing the old hand-enumerated
    ``_HIDDEN_UNICODE_RE`` regex. See the module-level comment above
    for the category rationale.
    """
    return ''.join(
        ch for ch in value
        if unicodedata.category(ch) not in _HIDDEN_FORMAT_CATEGORIES
        and not _is_variation_selector(ord(ch))
    )


def _cap_combining_marks(value: str) -> str:
    """Cap (not remove) runs of consecutive combining marks (categories
    ``Mn``/``Me``) to ``_MAX_COMBINING_MARKS_PER_BASE`` per base
    character. See ``_MAX_COMBINING_MARKS_PER_BASE`` above for why this
    is a cap rather than an outright strip.
    """
    out = []
    run_length = 0
    for ch in value:
        if unicodedata.category(ch) in ("Mn", "Me"):
            run_length += 1
            if run_length > _MAX_COMBINING_MARKS_PER_BASE:
                continue
        else:
            run_length = 0
        out.append(ch)
    return ''.join(out)


def _strip_control_bytes(value: Any) -> Any:
    """Recursively walk a parsed JSON value and strip C0/C1/DEL control
    bytes (``_CONTROL_BYTE_RE``), hidden/format Unicode and variation
    selectors (``_strip_hidden_unicode``), and cap runs of combining
    marks (``_cap_combining_marks``) on every string leaf.

    R4-F3/R5-F8/R13-F2/R14-F3: this is the ONLY place sanitization needs
    to happen now — called unconditionally on every input to
    ``sanitize_json_input``, whether it arrived as an already-decoded
    dict/list (the MCP ``tools/call`` path) or as a string/bytes payload
    that parsed cleanly on the first ``json.loads()`` attempt (the
    common REST case: standard ``\\u001b``-style escapes, and hidden
    Unicode characters, are valid JSON and never reach the old
    malformed-JSON-only fallback cleaning).

    dict keys are left untouched (matches historical behavior — only
    values were ever sanitized); non-string leaves (numbers, bools,
    None) pass through unchanged.
    """
    if isinstance(value, str):
        return _cap_combining_marks(
            _strip_hidden_unicode(_CONTROL_BYTE_RE.sub('', value))
        )
    if isinstance(value, dict):
        return {k: _strip_control_bytes(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_strip_control_bytes(v) for v in value]
    return value


# --- JSON Sanitization Utility ---
# Original location: main.py lines 52-123
def sanitize_json_input(input_data: Union[str, bytes, Dict, List, Any]) -> Union[Dict, List, Any]: # Added bytes to input_data
    """
    Sanitize JSON input aggressively to handle hidden Unicode characters,
    misplaced whitespace, and line breaks.

    Args:
        input_data: Can be a string, bytes (from request.body()),
                    or a Python object (dict, list).

    Returns:
        Properly parsed Python object (dict, list, etc.), with every
        string leaf value run through ``_strip_control_bytes`` — this
        is UNCONDITIONAL (R4-F3): it applies to already-parsed dict/list
        input, and to string/bytes input regardless of whether the
        initial ``json.loads()`` succeeded or needed fallback cleaning.
    """
    # If already a Python object (dict/list), it's the MCP tools/call
    # path (the SDK JSON-decodes the JSON-RPC body before this code
    # ever sees it) — still walk it and strip control bytes from every
    # string leaf; historically this returned the input completely
    # unsanitized (R4-F3).
    if isinstance(input_data, (dict, list)):
        return _strip_control_bytes(input_data)

    # If bytes, decode to string first
    if isinstance(input_data, bytes):
        try:
            input_data_str = input_data.decode('utf-8')
        except UnicodeDecodeError:
            logger.warning("Failed to decode input data as UTF-8, trying latin-1.")
            try:
                input_data_str = input_data.decode('latin-1')
            except UnicodeDecodeError as ude:
                logger.error(f"Could not decode input bytes: {ude}")
                raise ValueError(f"Invalid input bytes encoding: {ude}")
    elif isinstance(input_data, str):
        input_data_str = input_data
    else:
        # If not string or bytes, try to convert to string
        try:
            input_data_str = str(input_data)
        except Exception as e:
            logger.error(f"Failed to convert input to string: {e}")
            raise ValueError(f"Input must be a JSON string, bytes, or Python object, got {type(input_data)}")

    # Step 1: Initial direct parse attempt
    try:
        # R4-F3: standard JSON Unicode escapes (e.g. backslash-u001b) for
        # control characters are valid JSON under json.loads()'s default
        # strict=True — this succeeds on well-formed JSON carrying
        # escaped control characters, which used to return here
        # UNSTRIPPED (Step 3 below only ran on the malformed-JSON
        # fallback path). Strip unconditionally before returning.
        return _strip_control_bytes(json.loads(input_data_str))
    except json.JSONDecodeError:
        pass # Continue cleaning if direct parse fails

    # Step 2: Aggressive Whitespace Removal (Handles CR/LF/Spaces between elements)
    # Remove whitespace after opening braces/brackets
    cleaned = re.sub(r'([\{\[])\s+', r'\1', input_data_str)
    # Remove whitespace before closing braces/brackets
    cleaned = re.sub(r'\s+([\}\]])', r'\1', cleaned)
    # Remove whitespace after commas and colons
    cleaned = re.sub(r'([:,])\s+', r'\1', cleaned)
    # Remove whitespace before commas
    cleaned = re.sub(r'\s+(,)', r'\1', cleaned)
    # Remove line breaks that might be separating elements
    cleaned = cleaned.replace('\r\n', '').replace('\n', '').replace('\r', '')

    # Step 3: Remove Control Characters (excluding tab \t)
    # R5-F8: reuses _CONTROL_BYTE_RE (module scope) instead of a second
    # copy of the character class — the R4-F3 fallout was exactly this
    # pattern existing twice and drifting apart (C0-only here, C0+C1+DEL
    # there); one shared pattern object makes that class of bug structurally
    # impossible to reintroduce.
    cleaned = _CONTROL_BYTE_RE.sub('', cleaned)

    # Step 4: Remove problematic Unicode (format/bidi controls, variation
    # selectors, BOM, line/paragraph separators — see _strip_hidden_unicode)
    # R13-F2/R14-F3: for string LEAF VALUES this is now redundant — _strip_control_bytes
    # (called unconditionally below, and on every other return path) already
    # strips this same class from every string leaf post-parse. This raw-TEXT
    # pass still earns its keep, though: these hidden-Unicode characters are not
    # valid JSON structural whitespace, so one sitting between tokens (e.g. right
    # after a '{' or a ',') would otherwise make Step 5's json.loads() fail (and
    # Step 6's regex fallback fail the same way, since '\s' doesn't match them
    # either) even after Step 3's control-byte strip succeeds. Removing this line
    # would reintroduce that "chars in structural position" fallback failure —
    # kept, not dead code. (No combining-mark cap here — combining marks are
    # valid JSON whitespace-adjacent content and don't break structural
    # parsing; the leaf-level cap below is sufficient.)
    cleaned = _strip_hidden_unicode(cleaned)

    # Step 5: Try parsing the aggressively cleaned string
    try:
        # R4-F3: same escaped-control-character gap as Step 1 — strip
        # unconditionally on the parsed result, not just the raw text
        # (Step 3 above only catches UNESCAPED control bytes; a
        # -style escape decodes to a real control char only once
        # json.loads runs).
        return _strip_control_bytes(json.loads(cleaned))
    except json.JSONDecodeError as e_cleaned:
        # Step 6: Fallback for potentially nested/escaped JSON or other oddities
        try:
            # Try to find the main JSON object/array within the string
            match = re.search(r'^\s*(\{.*\}|\[.*\])\s*$', cleaned, re.DOTALL)
            if match:
                return _strip_control_bytes(json.loads(match.group(1)))
        except json.JSONDecodeError:
             pass # If even the extracted part fails, fall through
        except Exception as inner_e:
             logger.warning(f"Inner regex/parse fallback failed during sanitization: {inner_e}")
             pass

        # Log the final failure state for debugging
        error_excerpt = cleaned[:100] + ('...' if len(cleaned) > 100 else '')
        logger.error(f"Aggressive JSON parsing failed: {e_cleaned}, cleaned data (excerpt): {error_excerpt}")
        raise ValueError(f"Failed to parse JSON even after aggressive sanitization: {e_cleaned}")

class UntrustedBodyError(ValueError):
    """Raised by :func:`decode_untrusted_body` for any body it refuses.

    ``str()`` of this exception is deliberately CLIENT-SAFE: every tier
    surfaces it verbatim (the aiohttp wrappers embed it in their 400
    envelope, the FastAPI handlers do ``{"error": str(e)}``), so it must
    never carry interpreter or library internals — see R20-F3, where a
    raw ``RecursionError``'s "maximum recursion depth exceeded" reached
    the client. A ``ValueError`` subclass so the ~26 existing
    ``except ValueError`` call sites keep working unchanged.
    """


def decode_untrusted_body(raw: Union[str, bytes]) -> Dict:
    """THE decode seam: turn untrusted request bytes into a Python dict.

    N1 (security-arch hardening pass 2). Untrusted bytes become Python
    data at several structurally independent points in this codebase,
    and eleven findings across nine ``pentest-all`` rounds (R3-F1,
    R4-F3, R4-F4, R5-F8, R5-F9, R13-F2, R14-F3, R15-F1, R15-F2, R16-F3,
    R20-F2) were all the same bug: one of those points did a bare
    ``json.loads`` and skipped :func:`sanitize_json_input`. Each fix
    widened WHAT the sanitizer strips or WHEN it runs; none made
    skipping it structurally impossible, so the next decode point
    someone added inherited nothing.

    This function is the one entry point every request-handling module
    decodes through. It is what ``tests/router/
    test_arch_enforced_sanitization.py`` checks for the *absence* of: an
    AST walk over ``agent_mcp/router/`` + ``agent_mcp/app/`` flags any
    raw decode call that is not routed through here and is not on that
    file's declared-exemption list. Adding a twelfth decode point
    therefore fails a test instead of quietly reintroducing the class.

    Three wrappers used to implement this inline, near-identically:
    :func:`get_sanitized_json_body` (Starlette/FastAPI tier),
    ``admin_users_api._json_body`` and ``router.app._parse_json_body``
    (aiohttp tier). They are now thin call-throughs to this. Two
    differences between them were checked and deliberately KEPT at the
    call sites rather than folded in here, because each is that tier's
    API contract rather than a sanitization property:

      * **empty body.** The aiohttp wrappers map ``b""`` to ``{}`` (a
        few POSTs legitimately take no fields); the FastAPI tier has
        always rejected it. Both keep doing that — the check stays
        immediately above their call to this function, where the
        decision is visible.
      * **error envelope.** ``_parse_json_body`` reports
        ``invalid_json``, ``_json_body`` reports ``validation_error``,
        and the FastAPI handlers map a bare ``ValueError`` themselves.
        Unifying those would be a client-visible API change.

    Returns the parsed body as a ``dict`` with every string leaf run
    through :func:`_strip_control_bytes`. Raises
    :class:`UntrustedBodyError` (a ``ValueError``) on a malformed body,
    a top-level non-object, or a body deep enough to trip CPython's own
    recursion guard.
    """
    try:
        parsed = sanitize_json_input(raw)
    except RecursionError as exc:
        # PF-R20-1/R20-F3: ~3000+ levels of nesting trips CPython's
        # recursion guard inside json.loads() *or* inside
        # _strip_control_bytes's own dict/list walk. Neither message
        # belongs in a response.
        logger.error(f"Request body too deeply nested to parse: {exc}")
        raise UntrustedBodyError("request body is too deeply nested") from exc
    except ValueError as exc:
        # PF-R21-1: covers JSONDecodeError AND the UnicodeDecodeError an
        # invalid-UTF8 body raises — both ValueError subclasses, and the
        # narrower JSONDecodeError-only guard used to let the latter
        # escape to an uncaught 500.
        msg = exc.msg if isinstance(exc, json.JSONDecodeError) else str(exc)
        raise UntrustedBodyError(
            f"request body is not valid JSON: {msg}"
        ) from exc
    if not isinstance(parsed, dict):
        # PF-R12-1: a top-level list/string/scalar parses cleanly and
        # then blows up at the caller's ``data.get(...)`` as a 500.
        raise UntrustedBodyError("request body must be a JSON object")
    return parsed


# Helper function for API request handling
# Original location: main.py lines 126-143
async def get_sanitized_json_body(request: Any) -> Dict: # 'request: Request' if Starlette is imported
    """
    Helper function to safely get and sanitize a JSON request body.
    Assumes 'request' is a Starlette Request object or similar with an awaitable .body() method.

    Args:
        request: The Starlette request object (or any object with awaitable .body())

    Returns:
        The sanitized JSON body as a ``dict``.

    Raises:
        ValueError: If the request body is not valid JSON, cannot be
            processed, or does not decode to a top-level JSON object.

    N1: this is now a thin adapter — read the bytes off a Starlette
    request, hand them to :func:`decode_untrusted_body`. Everything the
    body-shape docstring below used to describe (the PF-R12-1 top-level
    object guard, the PF-R20-1/R20-F3 recursion-depth guard, the
    PF-R21-1 invalid-UTF8 guard) lives in that one shared seam now, so
    the aiohttp tier's two wrappers and this one cannot drift apart on
    any of it.

    Container-shape guard (PF-R12-1): every FastAPI ``app/routers/*``
    caller of this helper immediately does ``data.get(...)`` /
    ``data[key]`` — they all expect a JSON *object*. A top-level
    non-dict body (a bare list ``[1,2,3]``, a JSON string, or a scalar)
    parses cleanly, then raises ``AttributeError`` / ``TypeError`` at the
    ``.get()`` site and surfaces as an uncaught 500. The seam turns that
    class of type-confusion into a clean ``ValueError`` the callers
    already map to 400. No caller of this helper legitimately expects a
    top-level array; the lower-level :func:`sanitize_json_input` (used
    by the tool-argument path in ``tools/registry.py``) is deliberately
    left unguarded so it can still return list/scalar values.

    Empty body: rejected here (and mapped to 400 by every caller),
    unlike the aiohttp tier's ``{}``. See
    :func:`decode_untrusted_body`'s docstring for why that difference
    is kept at the call sites rather than folded into the seam.
    """
    try:
        # Get the raw body data
        raw_body = await request.body() # This is usually bytes
    except Exception as e:
        # Catching potential exceptions from request.body() itself (a
        # client disconnect mid-upload, an ASGI-server error).
        # R20-F3: the client-visible message stays static -- str(e) can
        # carry interpreter/library-internal detail; the real detail is
        # already logged here with exc_info for debugging.
        logger.error(f"Unexpected error processing request body: {e}", exc_info=True)
        raise ValueError("Error processing request body")

    try:
        return decode_untrusted_body(raw_body)
    except UntrustedBodyError as ube:
        logger.error(f"Failed to get/sanitize request body: {ube}")
        raise

# --- End JSON Sanitization Utility ---