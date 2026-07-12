"""R6-F3: null byte in a dashboard/asset path must 404, not 500.

``_safe_dashboard_path`` resolves a request-derived tail against the
dashboard root. ``pathlib.Path.resolve()`` raises
``ValueError: embedded null byte`` on a NUL in the path. Before the
fix that ``ValueError`` escaped the helper unhandled and aiohttp turned
it into a generic ``500``. Every other traversal variant (``..``,
``%2e%2e``, backslash, absolute, …) already returns a clean 404, so a
null byte must join them: unsafe → ``None`` → ``HTTPNotFound``.

The helper backs THREE handlers — ``dashboard_assets_handler`` (the
unauth ``/assets/`` route), ``dashboard_handler`` and
``overview_dashboard_handler`` — so fixing the one helper closes all
three. We assert both the helper (unit) and the unauth assets handler
(the live-confirmed 500 path).
"""

from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request


def test_safe_dashboard_path_null_byte_returns_none(router_module) -> None:
    """The helper maps an embedded null byte to its "unsafe → None"
    path instead of letting ValueError escape."""
    assert router_module._safe_dashboard_path("index.html\x00.png") is None
    assert router_module._safe_dashboard_path("\x00") is None


@pytest.mark.asyncio
async def test_assets_handler_null_byte_is_404_not_500(router_module) -> None:
    """The unauth assets route returns 404 (HTTPNotFound), not an
    unhandled 500, for a null-byte path — matching every other
    traversal variant."""
    req = make_mocked_request("GET", "/agent-mcp/assets/x")
    # The route's `{rest:.*}` capture is what a `--path-as-is` request
    # with a %00 lands in; drive the handler at that seam directly so
    # the test doesn't depend on client-side %00 normalisation.
    req._match_info = {"rest": "index.html\x00.png"}
    with pytest.raises(web.HTTPNotFound):
        await router_module.dashboard_assets_handler(req)
