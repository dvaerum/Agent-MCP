"""Shared wire-level type guards for the per-resource APIRouters.

arch-r4 #10: ``require_str`` was defined byte-identically 4x
(``tasks.py``, ``agents.py``, ``memories.py``, ``composition.py``) and
``require_str_list`` 2x (``tasks.py``, ``agents.py``). Each copy carried
a "SEC round-9 ... kept local to this router, do NOT consolidate"
comment — that was a deliberate scope boundary for the round-9
type-confusion security fix, not a statement that the functions must
stay separate forever. The round-9 boundary is settled; consolidating
now is safe.

Why these guards exist at all: several REST handlers bypass the
schema-validating MCP tool dispatch (they write the DB directly, or
they pre-validate before dispatch so a bad type is a clean 400 instead
of a 500 or a silently-swallowed write). A structured JSON value
(dict/list) landing in a string-typed field would otherwise reach a
SQL bind or a string method and blow up — or worse, be silently
coerced into bad stored data. These two guards close that gap at the
wire boundary, once.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi.responses import JSONResponse


def require_str(value: Any, field: str) -> Optional[JSONResponse]:
    """Return a 400 JSONResponse if ``value`` is present but not a str.

    ``None`` (an absent / cleared optional field) is allowed; callers
    that require presence check truthiness separately.
    """
    if value is not None and not isinstance(value, str):
        return JSONResponse(
            {"error": f"{field} must be a string"}, status_code=400
        )
    return None


def require_str_list(
    value: Any, field: str, *, allow_none: bool = True
) -> Optional[JSONResponse]:
    """Return a 400 JSONResponse unless ``value`` is a list[str].

    ``allow_none`` (default True) lets a caller opt out of treating
    ``None`` as valid — ``agents.py``'s ``capabilities`` guard has
    always rejected ``None`` (the field, once present in the update
    dict, must be a genuine list), while ``tasks.py``'s
    ``required_capabilities`` guard has always treated ``None`` as
    "not supplied" and let it through.
    """
    if value is None:
        if allow_none:
            return None
        return JSONResponse(
            {"error": f"{field} must be a list of strings"},
            status_code=400,
        )
    if not isinstance(value, list) or not all(
        isinstance(x, str) for x in value
    ):
        return JSONResponse(
            {"error": f"{field} must be a list of strings"},
            status_code=400,
        )
    return None


__all__ = ["require_str", "require_str_list"]
