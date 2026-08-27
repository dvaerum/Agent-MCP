"""Runtime asset-prefix substitution (Phase 4).

The dashboard build (``agent_mcp/dashboard``) emits a literal sentinel
string wherever Next.js would normally bake in the build-time
``assetPrefix``. The router substitutes the configured deployment
prefix into served HTML/JS/CSS bodies on the fly, so a single
dashboard build artifact can be deployed at any URL prefix without
rebuilding.

Why a sentinel and not a literal default prefix in the build:

* Previously the build hard-coded ``/agent-mcp/__dashboard`` via the
  ``ASSET_PREFIX`` env var at ``npm run build`` time. Operators who
  wanted to deploy under a different mount path (e.g. a reverse proxy
  serving the dashboard at ``/tools/``) had to rebuild from source —
  defeating the purpose of shipping a pre-built static export in the
  nix derivation.
* A sentinel-and-substitute approach inverts the dependency: the
  build is now prefix-agnostic, and the router is responsible for
  binding the prefix to served bytes.

The sentinel ``__AGENT_MCP_ASSET_PREFIX__`` is chosen to be:

* Unique enough to never appear by accident in legitimate JS/CSS/HTML
  (no real identifier or URL uses that exact double-underscore-wrapped
  shape).
* Plain ASCII so it survives any text encoding the dashboard build
  emits.
* Greppable, so an operator can quickly verify "is the sentinel still
  leaking through to the served HTML?" via a one-liner.

Public surface:

* ``SENTINEL`` — the literal string the build emits.
* ``substitute_asset_prefix(body, prefix)`` — the byte-level rewrite.
* ``content_type_needs_substitution(ctype)`` — Content-Type gate
  predicate. The substitution only fires on HTML/JS/CSS bodies; JSON
  API responses and binary assets (images, fonts) pass through
  unchanged so substitution can't corrupt them.
* ``substitute_file_bytes(path, prefix)`` — memoised disk-read +
  substitution, keyed on ``(path, mtime_ns, prefix)``. Used by the
  router's dashboard handlers so each (file, prefix) combination is
  rewritten at most once over the process's lifetime.

Performance note: the cache is process-local and lives for the router
process's lifetime. The cache key includes the file's ``st_mtime_ns``
so a redeploy that overwrites the dashboard's static files in place
will invalidate the cache automatically on next read.
"""

from __future__ import annotations

import os
import re
from pathlib import Path


# The literal sentinel emitted by the dashboard build at every spot
# Next.js would normally bake in ``assetPrefix``. Substituted at serve
# time by the router. See module docstring for the choice rationale.
SENTINEL: str = "__AGENT_MCP_ASSET_PREFIX__"
SENTINEL_BYTES: bytes = SENTINEL.encode("ascii")

# Next.js's flight-streaming serializer can flush its output buffer
# mid-string, closing one ``self.__next_f.push([N, "..."])`` call and
# opening the next at a byte offset that depends on the surrounding
# page content — not on anything to do with the sentinel itself. When
# a flush happens to land inside the sentinel, the whole-string
# byte-replace in ``substitute_asset_prefix`` never sees a contiguous
# match, so the sentinel (or its unmatched remainder) survives
# verbatim into the served bytes; the browser then requests it as a
# relative URL and gets a MIME-type mismatch off the SPA-fallback
# response. Confirmed live: an ``index.html`` containing
# ``...:HL["__AGENT_MCP_ASSET_PREFIX_"])</script><script>self.__next_f.push([1,"_/_next/static/css/<hash>.css",...``
# — the sentinel split one character before its end. Surfaced via
# Firefox-MCP click-through against the Schedules page, 2026-08-27.
#
# Fixed by matching the sentinel with this exact flush boundary
# allowed (optionally) between ANY two of its characters — the flush
# offset is build/content-dependent, so a fix pinned to one specific
# split position wouldn't generalize to a different page or a future
# rebuild.
_FLIGHT_FLUSH_BOUNDARY = rb'"\]\)</script><script>self\.__next_f\.push\(\[\d+,"'
_SENTINEL_WITH_OPTIONAL_SPLIT = re.compile(
    (b"(?:" + _FLIGHT_FLUSH_BOUNDARY + b")?").join(
        re.escape(bytes([b])) for b in SENTINEL_BYTES
    )
)


# Content-Type prefixes that need substitution. Anything else passes
# through unchanged. Tuple-of-startswith rather than equality so we
# match charset suffixes like ``text/html; charset=utf-8``.
#
# ``text/plain`` and ``text/x-component`` cover Next.js's RSC (React
# Server Components) flight payloads: the static export emits these
# alongside each page as ``<page>.txt`` and the browser fetches them
# during client-side navigation to render the new route. The payload
# encodes the page's CSS-preload links and the runtime ``assetPrefix``
# value, both as plain strings containing the build-time sentinel — so
# if the response body is not run through substitution the browser
# constructs CSS/JS URLs with the literal ``__AGENT_MCP_ASSET_PREFIX__``
# segment and the load fails with a MIME mismatch (the SPA fallback
# returns the index.html which is text/html, not text/css). Surfaced
# via Firefox-MCP click-through on 2026-06-17 against the post-PR-#164
# build.
_SUBSTITUTABLE_CTYPE_PREFIXES: tuple[str, ...] = (
    "text/html",
    "text/css",
    "application/javascript",
    "text/javascript",  # legacy alias some tooling still emits
    "text/plain",       # Next.js RSC flight payloads (.txt extension)
    "text/x-component", # canonical RSC MIME if a future build uses it
)


def substitute_asset_prefix(body: bytes, prefix: str) -> bytes:
    """Replace every occurrence of ``SENTINEL`` in ``body`` with
    ``prefix``.

    Pure: takes bytes, returns bytes. No I/O, no caching.

    Matches the sentinel with the flight-flush boundary optionally
    interposed between any two of its characters (see
    ``_SENTINEL_WITH_OPTIONAL_SPLIT`` above), so both a normal
    contiguous occurrence AND one split across a
    ``self.__next_f.push(...)`` boundary get replaced. This subsumes a
    plain substring match — when nothing splits the sentinel, every
    optional group simply matches zero characters and the pattern
    degenerates to the sentinel's literal bytes — so there is no
    separate non-split code path to keep in sync.

    Safe on arbitrary text-shaped payloads regardless of encoding
    details, as long as the encoding is ASCII-compatible (which
    UTF-8 / Latin-1 / ASCII all are; the dashboard build emits UTF-8).

    ``prefix`` is the configured runtime prefix (e.g.
    ``/agent-mcp/__dashboard``). It is encoded as UTF-8 for the
    replacement. An empty ``prefix`` is legal and produces site-root-
    relative URLs (useful for a single-tenant deploy at the host root).
    """
    if SENTINEL_BYTES not in body and b"__next_f.push" not in body:
        # Neither a plain nor a split occurrence can be present: a
        # split occurrence always straddles a self.__next_f.push(...)
        # boundary (see module docstring), so its absence rules that
        # case out too. Skips the regex on the common case (most
        # served bodies carry neither).
        return body
    return _SENTINEL_WITH_OPTIONAL_SPLIT.sub(
        prefix.encode("utf-8"), body
    )


def content_type_needs_substitution(content_type: str | None) -> bool:
    """True iff a response with this ``Content-Type`` should have its
    body passed through ``substitute_asset_prefix``.

    Defensive: ``None`` / empty / unknown Content-Types return False,
    so the default behavior is "pass through unchanged" — substitution
    is opt-in per type, never opt-out.
    """
    if not content_type:
        return False
    ct = content_type.lower()
    return any(ct.startswith(p) for p in _SUBSTITUTABLE_CTYPE_PREFIXES)


# ── On-disk cache for dashboard files ───────────────────────────────
#
# The router serves the dashboard's static export on every request.
# Re-reading + re-substituting the same file on every request would
# burn CPU and disk I/O for no value: the dashboard rebuild produces
# a new on-disk tree, the router stays up. Cache the substituted
# bytes keyed on ``(path, mtime_ns, prefix)`` so:
#
#   * The same (file, prefix) combo is rewritten at most once.
#   * A redeploy that touches the file invalidates the entry on the
#     next read (mtime changes).
#   * A live re-config (router restart with a different prefix; or a
#     test that monkeypatches ``ASSET_PREFIX``) blows past the cache
#     because the prefix is part of the key.
#
# The cache lives at module scope so the router's restart wipes it —
# no need for explicit invalidation calls anywhere.

_CacheKey = tuple[str, int, str]
_CACHE: dict[_CacheKey, bytes] = {}


def substitute_file_bytes(path: Path, prefix: str) -> bytes:
    """Read ``path`` from disk and return its bytes with the sentinel
    substituted to ``prefix``, memoised.

    Cache key is ``(path, st_mtime_ns, prefix)``. The mtime arm makes
    in-place redeploys safe; the prefix arm makes per-test prefix
    overrides safe.

    Reads the file fresh if no cache hit. Stores the substituted bytes
    on hit-miss so repeated reads are a dict lookup.
    """
    st = os.stat(path)
    key: _CacheKey = (str(path), st.st_mtime_ns, prefix)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    raw = Path(path).read_bytes()
    out = substitute_asset_prefix(raw, prefix)
    _CACHE[key] = out
    return out
