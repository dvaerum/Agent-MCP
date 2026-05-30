<!--
Thanks for the PR. Fill in every section that applies.
"I'll add tests later" is a no — see CONTRIBUTING.md.
-->

## What changed

<!-- One or two sentences. The diff already shows the code. -->

## Why

<!--
The problem this fixes, the capability this adds, or the constraint
this relieves. If there's an issue (in this repo or upstream's),
link it.
-->

## Red → green

<!--
Required for any PR with behavioral change. Build-only PRs (Nix
build hygiene, CI config, font/asset changes) can write "n/a — build
only" here.

- Failing test commit: <sha or test name>
- Passing test commit: <sha or test name>

If the tests live in `tests/test_<thing>.py`, name them so a reviewer
can run them locally.
-->

## Upstream issue link (if applicable)

<!--
- This PR resolves issue X in `docs/UPSTREAM_ISSUES.md` of the
  deployment repo: <link to the relevant section>
- Upstream issue / PR (rinadelph/Agent-MCP): <URL if filed>
-->

## Router side-effect (if applicable)

<!--
Does this PR enable retiring a router workaround in
`nixos-developer-system/users/dennis/agent-mcp/router.py`? Name the
function(s) that can go away when this lands. Skip if no router
effect.

Examples:
- Retires `_redact_tokens_in_event` (issue I worker→admin escalation
  fix in `view_project_context`).
- Retires synthetic `send_peer_message` (issue K worker→worker
  messaging fix).
-->

## Checklist

- [ ] Tests added (or "n/a — build only" above)
- [ ] `ruff check .` clean
- [ ] `pytest` green locally
- [ ] Dashboard build green (`cd agent_mcp/dashboard && npm run build`) — if touching dashboard
- [ ] CI green on this PR
- [ ] Branch name matches `fix/…`, `feat/…`, `chore/…`, or `upstream/…`
- [ ] Targeting `main` (not directly pushing)
