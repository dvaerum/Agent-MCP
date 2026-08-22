# Agent-MCP/mcp_template/mcp_server_src/utils/json_utils.py
import json
import re
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

# Hidden/spoofing Unicode worth stripping from a *parsed* string value:
# zero-width spaces (U+200B-U+200F), the BOM / zero-width no-break space
# (U+FEFF), and the line/paragraph separators (U+2028/U+2029). R13-F2:
# this is the exact character class the old "Step 4" fallback-only regex
# matched — hoisted to module scope for the same reason R5-F8 hoisted
# _CONTROL_BYTE_RE: so both the post-parse-success path and the
# already-a-dict/list path can share one definition instead of drifting
# apart. Visually near-indistinguishable strings (e.g. a task title with
# a trailing zero-width space) are a duplicate-identifier / spoofing risk
# even though they parse as perfectly legal JSON string content.
_HIDDEN_UNICODE_RE = re.compile(r'[\u200B-\u200F\uFEFF\u2028\u2029]')


def _strip_control_bytes(value: Any) -> Any:
    """Recursively walk a parsed JSON value and strip C0/C1/DEL control
    bytes (``_CONTROL_BYTE_RE``) and hidden/spoofing Unicode
    (``_HIDDEN_UNICODE_RE``) from every string leaf.

    R4-F3/R5-F8/R13-F2: this is the ONLY place sanitization needs to
    happen now — called unconditionally on every input to
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
        return _HIDDEN_UNICODE_RE.sub('', _CONTROL_BYTE_RE.sub('', value))
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

    # Step 4: Remove problematic Unicode (Zero-width spaces, BOM, line/paragraph separators)
    # R13-F2: for string LEAF VALUES this is now redundant — _strip_control_bytes
    # (called unconditionally below, and on every other return path) already
    # strips _HIDDEN_UNICODE_RE from every string leaf post-parse. This raw-TEXT
    # pass still earns its keep, though: these hidden-Unicode characters are not
    # valid JSON structural whitespace, so one sitting between tokens (e.g. right
    # after a '{' or a ',') would otherwise make Step 5's json.loads() fail (and
    # Step 6's regex fallback fail the same way, since '\s' doesn't match them
    # either) even after Step 3's control-byte strip succeeds. Removing this line
    # would reintroduce that "chars in structural position" fallback failure —
    # kept, not dead code.
    cleaned = _HIDDEN_UNICODE_RE.sub('', cleaned)

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

    Container-shape guard (PF-R12-1): every FastAPI ``app/routers/*``
    caller of this helper immediately does ``data.get(...)`` /
    ``data[key]`` — they all expect a JSON *object*. A top-level
    non-dict body (a bare list ``[1,2,3]``, a JSON string, or a scalar)
    parses cleanly, then raises ``AttributeError`` / ``TypeError`` at the
    ``.get()`` site and surfaces as an uncaught 500. Enforcing
    ``isinstance(parsed, dict)`` HERE turns that class of type-confusion
    into a clean ``ValueError`` the callers already map to 400 — matching
    the aiohttp ``router/`` tier's ``_parse_json_body`` object guard. No
    caller of this helper legitimately expects a top-level array; the
    lower-level :func:`sanitize_json_input` (used by the tool-argument
    path in ``tools/registry.py``) is deliberately left unguarded so it
    can still return list/scalar values.
    """
    try:
        # Get the raw body data
        raw_body = await request.body() # This is usually bytes

        # Sanitize and parse it (sanitize_json_input now handles bytes decoding)
        parsed = sanitize_json_input(raw_body)
        if not isinstance(parsed, dict):
            raise ValueError("request body must be a JSON object")
        return parsed
    except ValueError as ve: # Catch ValueError from sanitize_json_input or body decoding
        logger.error(f"Failed to get/sanitize request body: {ve}")
        raise ValueError(f"Invalid request body: {ve}") # Re-raise with context
    except Exception as e:
        # Catching other potential exceptions from request.body() or unexpected issues
        logger.error(f"Unexpected error processing request body: {e}", exc_info=True)
        raise ValueError(f"Error processing request body: {e}")

# --- End JSON Sanitization Utility ---