#!/usr/bin/env bash
# Precompact-hook URL-derivation contract.
#
# The daemon-agent precompact hook
# (`nix/agent-mcp-daemon-agent-precompact-hook.sh.in`) posts a tiny
# resume-pointer back to the project_context REST endpoint. To do that
# it needs the REST API root, which it derives from `AGENT_MCP_MCP_URL`.
#
# Pre-PR-D (URL redesign), MCP URLs were:
#     https://host/agent-mcp/<name>/mcp
# so `${mcp_url%/mcp}/api` stripped the trailing `/mcp` and gave
#     https://host/agent-mcp/<name>/api
# which is NOT the REST root anyway -- but for the project_context POST
# that path coincidentally worked because the old REST surface lived
# under `/agent-mcp/<name>/...`.
#
# Post-PR-D the URL is:
#     https://host/agent-mcp/mcp/<name>
# `${mcp_url%/mcp}` is now a NO-OP (the suffix doesn't match), so the
# old line produced `https://host/agent-mcp/mcp/<name>/api` -- which
# doesn't exist and 404s.
#
# The correct derivation: strip the trailing `/mcp/<name>` and append
# `/api/projects/<name>`. The hook then POSTs to
# `/api/projects/<name>/project-context` which is the canonical
# REST-shape resource.
#
# This test extracts the derivation block from the .sh.in template
# (rather than duplicating the bash expression here) so a future tweak
# to the template is automatically reflected in the test, and runs it
# in isolation against representative URL shapes so the contract is
# locked.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
HOOK_TEMPLATE="$REPO_ROOT/nix/agent-mcp-daemon-agent-precompact-hook.sh.in"

# Pull out the api_root derivation block. Anchored on the comment
# "Derive the REST API root from the MCP URL" (a stable marker that
# survives template churn) and runs until the first blank line after
# at least one non-comment, non-blank assignment -- by convention the
# block ends with a blank line before the curl invocation.
extract_derivation_block() {
    awk '
        /Derive the REST API root from the MCP URL/ { found=1 }
        found {
            if ($0 ~ /^[[:space:]]*$/ && seen_code) exit
            print
            if ($0 !~ /^[[:space:]]*#/ && $0 !~ /^[[:space:]]*$/) seen_code=1
        }
    ' "$HOOK_TEMPLATE"
}

DERIV="$(extract_derivation_block)"
if [ -z "$DERIV" ]; then
    echo "FAIL: could not find api_root derivation marker comment in hook template" >&2
    exit 1
fi
if ! grep -q 'api_root=' <<<"$DERIV"; then
    echo "FAIL: extracted block does not assign api_root: $DERIV" >&2
    exit 1
fi

derive_api_root() {
    local mcp_url="$1"
    bash -c "
set -euo pipefail
mcp_url=$(printf '%q' "$mcp_url")
$DERIV
printf '%s' \"\$api_root\"
"
}

assert_api_root() {
    local label="$1" mcp_url="$2" expected="$3"
    local actual
    actual="$(derive_api_root "$mcp_url")"
    if [ "$actual" != "$expected" ]; then
        echo "FAIL: $label" >&2
        echo "  mcp_url:  $mcp_url" >&2
        echo "  expected: $expected" >&2
        echo "  actual:   $actual" >&2
        exit 1
    fi
}

# New URL shape (post-PR-D, what production actually emits).

# Daemon wrapper builds http://127.0.0.1:1337/agent-mcp/mcp/<name>.
# The hook must derive the matching REST root.
assert_api_root \
    "api_root for loopback mcp url" \
    "http://127.0.0.1:1337/agent-mcp/mcp/washing-brothers" \
    "http://127.0.0.1:1337/agent-mcp/api/projects/washing-brothers"

# If an operator points the daemon at the public tailnet URL the same
# derivation must work -- `_validate_name` reserves `mcp` so the
# pattern is unambiguous.
assert_api_root \
    "api_root for tailnet mcp url" \
    "https://nixos-developer-system.tailfdae0.ts.net/agent-mcp/mcp/washing-brothers" \
    "https://nixos-developer-system.tailfdae0.ts.net/agent-mcp/api/projects/washing-brothers"

# Project names allow single hyphens. The derivation must not eat them.
assert_api_root \
    "api_root handles single-hyphen project name" \
    "http://127.0.0.1:1337/agent-mcp/mcp/my-project" \
    "http://127.0.0.1:1337/agent-mcp/api/projects/my-project"

assert_api_root \
    "api_root handles non-default port" \
    "http://127.0.0.1:8080/agent-mcp/mcp/proj" \
    "http://127.0.0.1:8080/agent-mcp/api/projects/proj"

echo "ok: precompact-hook-api-root"
