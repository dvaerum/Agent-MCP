"""
String utility functions for the MCP server.

This module provides various string manipulation functions that can be used
throughout the MCP server application.
"""

import re


# --- Unsafe-Unicode validator (F005 verify-all-v6 MUTATING #3) -------------
#
# Identifier-like fields (e.g. ``project_context.context_key``) are
# displayed back to operators in the dashboard and quoted into shell
# tooling / log lines. Unicode control characters, bidirectional
# overrides, and invisible/format characters in those fields are a
# spoofing + tooling-corruption attack surface:
#
#   * U+0000 NULL terminates C-style strings → truncates audit
#     records, breaks ``grep``/``find`` pipelines.
#   * U+202E RIGHT-TO-LEFT OVERRIDE flips display order — a key
#     stored as ``config<U+202E>drowssap`` renders as
#     ``configpassword`` in the UI but searches/stores as the
#     original. Classic homoglyph-substitute attack vector.
#   * U+FEFF BOM / U+200B ZERO-WIDTH SPACE / U+2060 WORD JOINER and
#     friends are invisible — two keys that "look" identical can
#     differ by an invisible char, defeating uniqueness checks for
#     human reviewers.
#
# verify-all-v6 MUTATING #3 surfaced this by POSTing
# ``{"context_key": "u‮ ﻿\U0001F680key", ...}`` to
# ``/api/memories`` and getting a 200 — the row landed in the DB
# with raw NULL / RTL / BOM bytes in the primary-key field.
#
# Emoji (e.g. U+1F680 ROCKET) are intentionally NOT rejected — they
# render predictably across renderers and don't carry the
# spoofing/control semantics that the ranges below do.

# Pre-compiled regex matching any disallowed-in-identifiers character:
#   U+0000-U+001F : ASCII C0 controls (NULL, BELL, ESC, …)
#   U+007F        : DEL
#   U+200B-U+200F : zero-width space/non-joiner/joiner, LRM, RLM
#   U+2028-U+2029 : line separator, paragraph separator
#   U+202A-U+202E : PDF/LRO/RLO/LRE/RLE bidi overrides
#   U+2060-U+2064 : word joiner, function-application, inv-times, etc.
#   U+2066-U+2069 : LRI, RLI, FSI, PDI bidi isolates
#   U+206A-U+206F : deprecated bidi controls (shape selectors)
#   U+FEFF        : BOM / zero-width no-break space
_DISALLOWED_KEY_CHAR_RE = re.compile(
    r"[\x00-\x1F\x7F"
    r"​-‏"
    r" - "
    r"‪-‮"
    r"⁠-⁤"
    r"⁦-⁩"
    r"⁪-⁯"
    r"﻿"
    r"]"
)


def has_unsafe_unicode_for_identifier(value: str) -> bool:
    """Return True if ``value`` contains a Unicode codepoint that we
    refuse in identifier-like fields (memory keys, etc.).

    See module-level comment for the rationale and ranges.

    Args:
        value: The candidate identifier string.

    Returns:
        True if the string contains a disallowed control / bidi /
        invisible character; False otherwise.

    Examples:
        >>> has_unsafe_unicode_for_identifier("normal_key")
        False
        >>> has_unsafe_unicode_for_identifier("café.config")
        False
        >>> has_unsafe_unicode_for_identifier("emoji.\U0001F680.key")
        False
        >>> has_unsafe_unicode_for_identifier("a\\x00b")
        True
        >>> has_unsafe_unicode_for_identifier("config‮drowssap")
        True
    """
    if not isinstance(value, str):
        return False
    return _DISALLOWED_KEY_CHAR_RE.search(value) is not None


# Reusable error envelope for handlers that reject an unsafe key.
# Keeping the message in one place means the test + the handler +
# any future REST surface (PUT, MCP-tool wrapper) speak with one
# voice. ``error`` is the machine code; ``message`` is the human
# explanation. The envelope matches the existing ``{"error": ...}``
# shape that other 400-rejecting handlers in ``routes.py`` use.
UNSAFE_KEY_ERROR = {
    "error": "invalid_key_character",
    "message": (
        "Memory key contains a disallowed character "
        "(Unicode control / bidi-override / invisible). "
        "Allowed: printable Unicode except "
        "U+0000-U+001F, U+007F, "
        "U+200B-U+200F, U+2028-U+2029, U+202A-U+202E, "
        "U+2060-U+2064, U+2066-U+2069, U+206A-U+206F, U+FEFF."
    ),
}


# --- Memory-key allowlist (positive charset) -------------------------------
#
# Beyond the invisible/bidi denylist above, memory keys are constrained to a
# small ASCII charset: letters, digits, and the four punctuation marks the
# existing keys already use (``.`` ``_`` ``/`` ``-``). ``/`` is deliberately
# allowed — it is the namespacing convention (``ios/repo``,
# ``backend-dev/status``) and the REST routes accept it via ``:path``.
#
# WHY an ASCII allowlist (tighter than the denylist): keys are URL path
# segments, so they must round-trip cleanly through the REST routes;
# ASCII-only also removes homograph/encoding ambiguity (``café`` vs ``cafe``).
# A scan of every project's keys found ZERO outside this set, so enforcing it
# renames nothing existing — it is a forward-looking invariant.
MEMORY_KEY_RE = re.compile(r"^[A-Za-z0-9._/-]+$")

#: The single character disallowed key chars are converted to by the
#: sanitizing migration.
MEMORY_KEY_REPLACEMENT = "_"


def is_valid_memory_key(value: object) -> bool:
    """True iff ``value`` is a non-empty string matching :data:`MEMORY_KEY_RE`
    (``A-Z a-z 0-9 . _ / -``). Empty / non-str / any other character → False."""
    if not isinstance(value, str) or not value:
        return False
    return MEMORY_KEY_RE.match(value) is not None


def sanitize_memory_key(value: str) -> str:
    """Return ``value`` with every character disallowed by
    :data:`MEMORY_KEY_RE` replaced by :data:`MEMORY_KEY_REPLACEMENT`
    (``_``). A conforming key is returned unchanged. Used by the
    key-sanitizing migration; never mutates in place.

    Examples:
        >>> sanitize_memory_key("ios/improvements-doc")
        'ios/improvements-doc'
        >>> sanitize_memory_key("ns:key with space")
        'ns_key_with_space'
        >>> sanitize_memory_key("caf\\u00e9.config")
        'caf_.config'
    """
    if not isinstance(value, str):
        return value
    return re.sub(r"[^A-Za-z0-9._/-]", MEMORY_KEY_REPLACEMENT, value)


# Positive-allowlist rejection envelope for the create/write surfaces
# (distinct from UNSAFE_KEY_ERROR, which is the narrower invisible/bidi
# denylist message). One voice across the REST router + MCP tool.
MEMORY_KEY_ERROR = {
    "error": "invalid_key_character",
    "message": (
        "Memory key may contain only letters, digits, and . _ / - "
        "(A-Z a-z 0-9 . _ / -)."
    ),
}

# R20-F2: settings.py's ``context_key`` sibling of MEMORY_KEY_ERROR.
# Same allowlist (MEMORY_KEY_RE / is_valid_memory_key) applied to
# project_settings' context_key -- the denylist in
# has_unsafe_unicode_for_identifier misses Unicode categories Lo/So
# (e.g. U+115F HANGUL CHOSEONG FILLER, U+2800 BRAILLE PATTERN BLANK),
# which render as blank/invisible glyphs but were never in the
# hand-enumerated range table above. config_* keys are internal toggle
# identifiers, not user-facing text, so the same ASCII-only positive
# allowlist memories.py already enforces is the right fit here too --
# just a settings-flavoured message so the surface a caller hits
# (memories vs settings) always names itself correctly.
SETTING_KEY_ERROR = {
    "error": "invalid_key_character",
    "message": (
        "Setting key may contain only letters, digits, and . _ / - "
        "(A-Z a-z 0-9 . _ / -)."
    ),
}


def camel_to_snake_case(camel_string: str) -> str:
    """
    Converts a camelCase string to snake_case.
    
    Args:
        camel_string: The camelCase string to convert.
        
    Returns:
        The converted snake_case string.
        
    Examples:
        >>> camel_to_snake_case("helloWorld")
        'hello_world'
        >>> camel_to_snake_case("HTTPResponse")
        'http_response'
    """
    import re
    # Insert underscore before uppercase letters and convert to lowercase
    snake_case = re.sub(r'(?<!^)(?=[A-Z])', '_', camel_string).lower()
    return snake_case


def snake_to_camel_case(snake_string: str, capitalize_first: bool = False) -> str:
    """
    Converts a snake_case string to camelCase.
    
    Args:
        snake_string: The snake_case string to convert.
        capitalize_first: Whether to capitalize the first letter (PascalCase).
        
    Returns:
        The converted camelCase string.
        
    Examples:
        >>> snake_to_camel_case("hello_world")
        'helloWorld'
        >>> snake_to_camel_case("http_response", capitalize_first=True)
        'HttpResponse'
    """
    # Split the string by underscores
    components = snake_string.split('_')
    
    # Capitalize each component except the first one (unless capitalize_first=True)
    if capitalize_first:
        return ''.join(x.title() for x in components)
    else:
        return components[0] + ''.join(x.title() for x in components[1:])


def truncate_string(text: str, max_length: int, ellipsis: str = '...') -> str:
    """
    Truncates a string to a specified length with optional ellipsis.
    
    Args:
        text: The string to truncate.
        max_length: The maximum length of the string.
        ellipsis: The string to append if truncation occurs. Defaults to '...'.
        
    Returns:
        The truncated string.
        
    Examples:
        >>> truncate_string("This is a long string", 10)
        'This is a...'
        >>> truncate_string("Short", 10)
        'Short'
    """
    if len(text) <= max_length:
        return text
    
    # Calculate truncation point to accommodate ellipsis
    truncate_at = max_length - len(ellipsis)
    return text[:truncate_at] + ellipsis 