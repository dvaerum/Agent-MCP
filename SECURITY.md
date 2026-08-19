# Security Policy

## Reporting a Vulnerability

Please **do not** open a public GitHub issue or pull request for a
security vulnerability — that discloses it to everyone before a fix
exists.

Instead, use GitHub's private reporting flow:

**[Report a vulnerability](https://github.com/dvaerum/Agent-MCP/security/advisories/new)**
(Security tab → "Report a vulnerability")

This opens a private advisory visible only to the maintainer, where
you can describe the issue, and (once a fix is ready) coordinate a
disclosure timeline and get credited in the published advisory.

If you're unable to use the GitHub flow for some reason, open a
regular issue asking the maintainer to reach out for a private
channel — don't include vulnerability details in that issue itself.

## What's in scope

This repo (`dvaerum/Agent-MCP`) is a maintained fork of
[`rinadelph/Agent-MCP`](https://github.com/rinadelph/Agent-MCP) — see
[CONTRIBUTING.md](CONTRIBUTING.md) for the fork's background. Security
reports against this fork's own code (router, backend, dashboard,
`aoe-bridge`, nix packaging) are in scope. Issues that only reproduce
against the dormant upstream and aren't present here should go to
upstream instead.

## Response expectations

This is a small maintained fork, not a project with a dedicated
security team — there's no guaranteed SLA. Reports are read and
triaged as soon as reasonably possible; expect an initial response
within a few days, not hours.
