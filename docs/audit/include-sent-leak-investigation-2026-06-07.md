# Audit — `include_sent=False` leak investigation (2026-06-07)

## Reported bug (v5.0.21)

A worker agent reported that calling
`get_agent_messages(include_sent=False, include_received=True)` still
returned messages the calling agent had sent.

## Surfaces audited

1. **MCP tool — `get_agent_messages_tool_impl`**
   (`agent_mcp/tools/agent_communication_tools.py:292-422`)

   The branch at lines 326-336 routes filter combinations:
   ```
   if include_received and include_sent:
       WHERE (recipient_id = ? OR sender_id = ?)
   elif include_received:
       WHERE recipient_id = ?
   elif include_sent:
       WHERE sender_id = ?
   else:
       Error: must include sent or received
   ```
   The `elif include_received:` branch is `WHERE recipient_id = ?` —
   correct on inspection. There is no post-query Python filter that
   could leak sent rows.

2. **REST endpoint — `POST /api/messages/query`**
   (`agent_mcp/app/routes.py:1922-2036`, `list_messages_api_route`)

   This endpoint does **not** expose `include_sent` / `include_received`.
   Its filter surface is `from`/`to`/`between`/`type`/`priority`/
   `read`/`since`/`until`/`q`. The `to` filter translates to
   `recipient_id = ?` and the `from` filter to `sender_id = ?` — both
   applied at the SQL layer in `WHERE`. There is no implicit
   "include sent" leak path.

3. **Dashboard filter — `messages-dashboard.tsx`**

   `grep -rn "include_sent\|includeSent" agent_mcp/dashboard/` returns
   no matches. The dashboard does not interact with the
   `include_sent` concept at all — it builds REST queries with
   `from`/`to`.

4. **Tool registration schema** (lines 1109-1156)

   `include_sent` has `"default": False`. A caller that omits the key
   gets `False` — matching the documented behavior. No silent
   coercion.

## Reproduction attempt

The companion test file
`tests/test_get_messages_include_sent_filter.py` seeds two known
agents (`alice`, `bob`), inserts a single message `alice → bob`, and
runs three assertions:

1. **MCP tool, as the sender (Alice):**
   `get_agent_messages(include_sent=False, include_received=True)`
   must NOT return the message Alice sent.

2. **MCP tool, as the recipient (Bob):**
   the same call must STILL return the message.

3. **REST endpoint, `to=alice` filter:**
   must return messages addressed to Alice but NOT messages Alice
   herself sent.

**All three assertions pass on `origin/main`** (no fix applied).
The bug does not reproduce on the v5.0.21 codebase via the
in-process test harness that drives the same registered MCP request
handler real SSE/JSON-RPC clients hit.

## Conclusion

No leak was identified in any of the three surfaces. The MCP tool's
SQL is correct; the REST endpoint has no `include_sent` concept;
the dashboard does not interact with the flag. The test we wrote
to reproduce the bug instead pins the **expected** contract so any
future regression at any of these surfaces is caught.

## Hypothesis on the original report

Plausible explanations for the original report (not verified):

- The reporter may have been looking at a conversation thread view
  in the dashboard that intentionally shows both directions,
  unrelated to the MCP tool's `include_sent` flag.
- The reporter may have resolved a different `agent_id` than they
  expected (e.g. a stale agent record), making messages they sent
  to themselves visible as "received".
- The reporter may have been on a pre-v5.0.21 build that briefly
  had a regression on this path (no such commit is visible in
  recent history).

## Action items

- Tests checked in as a regression guard — see
  `tests/test_get_messages_include_sent_filter.py`.
- A TaskCreate has been opened asking Dennis to forward the exact
  reproduction steps to the original reporter; if a new repro
  surfaces, this audit doc is the starting point for the next pass.
