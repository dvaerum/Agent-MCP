# Agent-MCP/agent_mcp/core/schema_limits.py
"""Shared string-length bounds for MCP tool-argument JSON schemas.

R8-F1 (round-8 protocol/fuzz pentest, confirmed live against vm-dev
@ e68e36a): every ``register_tool(...)`` input_schema across the ~17
tool-registration modules (``agent_mcp/tools/*.py``) declared
string-typed properties with NO ``maxLength`` anywhere. A
200,000-char ``register_agent`` ``name`` minted a 200,000-char
agent_id; a 500,000-char ``create_task`` ``task_title`` was stored
and echoed verbatim in ``view_tasks``. Both landed as 200 OK, bounded
only by the router's blanket 1MB request-body cap
(``agent_mcp/router/app.py::_MCP_MAX_BODY_BYTES``) — a resource-
exhaustion / stored-bloat gap, repeatable across separate requests.

Precedent for a length gate already existed for router-level project
names (``router/app.py::_NAME_MAX = 64`` / ``_validate_name``); this
module gives MCP tool arguments the same discipline, via TWO
complementary mechanisms rather than ~130 hand-copied integer
literals:

1.  A handful of identifier/title/path-shaped fields that the fuzz
    lane specifically fired at (``register_agent`` name/agent_id,
    ``edit_agent`` color/working_directory, ``create_task`` /
    ``assign_task`` / batch ``tasks[].title``, and their siblings on
    the same tool definitions) get an EXPLICIT, tight ``maxLength``
    drawn from the constants below — ``jsonschema.validate`` (already
    run by the dispatcher on every call, see
    ``tools/registry.py::dispatch_tool_call``) enforces those
    directly, same as any other schema keyword.

2.  EVERY OTHER string-typed property — including ones this sweep
    didn't hand-touch, and any a future tool author adds without
    thinking about it — is bounded by ``DEFAULT_STRING_MAX_LEN`` via
    a generic recursive pass the dispatcher runs on any string leaf
    whose schema node has no explicit ``maxLength``
    (``tools/registry.py::_first_oversized_string_path``). This is
    the actual "sweep ALL of them" lever: the cap applies globally
    without requiring every schema author to remember to declare one.
"""

# Short identifier / single-token fields: agent_id, task_id, color,
# hostnames, project/backup names, session ids. Mirrors
# router/app.py's _NAME_MAX=64 precedent, widened to comfortably fit
# real-world values that precedent didn't have to (e.g.
# "worker@some-long-hostname.tailnetXXXX.ts.net" agent_ids, FQDNs)
# without inviting abuse -- legitimate identifiers are two orders of
# magnitude under this.
IDENTIFIER_MAX_LEN = 256

# Human-typed single-line titles (task_title / title fields).
TITLE_MAX_LEN = 512

# Agent-to-agent / broadcast message bodies. Matches the limit
# `send_agent_message`'s own schema description has always *claimed*
# ("Message content (max 4000 characters)") without ever having
# enforced it -- this makes that claim true.
MESSAGE_MAX_LEN = 4000

# Filesystem paths. Matches Linux's PATH_MAX (4096 bytes incl. NUL).
PATH_MAX_LEN = 4096

# Free-form multi-paragraph text: task/context descriptions, notes,
# RAG queries/documents, coordination notes, agent profiles. 64 KiB
# is generous for legitimate prose/markdown while still bounding
# storage and rendering blowup -- a single request is already capped
# at 1 MiB by the router (_MCP_MAX_BODY_BYTES), so this leaves room
# for several such fields in one call while remaining well inside
# that budget.
LONG_TEXT_MAX_LEN = 65536

# Applied by the dispatcher (tools/registry.py) to any string-typed
# schema node that does not declare its own "maxLength" -- the safety
# net that makes the cap apply to EVERY tool argument, present and
# future, without every author remembering to declare one.
DEFAULT_STRING_MAX_LEN = LONG_TEXT_MAX_LEN
