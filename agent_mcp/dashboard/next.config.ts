import type { NextConfig } from "next";
import { readFileSync } from "fs";
import { join } from "path";

// Bundle analyzer
const withBundleAnalyzer = require('@next/bundle-analyzer')({
  enabled: process.env.ANALYZE === 'true',
})

// Product version shown in the UI (sidebar footer). pyproject.toml is the
// single source of truth; this NEVER hardcodes a literal (the sidebar used
// to carry a frozen "v3.4.0" that drifted far behind the real version).
//
// Resolution order:
//   1. NEXT_PUBLIC_AGENT_MCP_VERSION from the environment — the Nix build
//      passes it (sourced from pyproject) so the sandboxed build, which has
//      no repo-root pyproject in scope, still gets the right number.
//   2. Read ../../pyproject.toml at build time — covers plain `npm run
//      dev` / `npm run build` from the dashboard dir (pyproject is two
//      levels up: agent_mcp/dashboard -> repo root).
//   3. "dev" — last-resort fallback if neither is available.
function resolveVersion(): string {
  const fromEnv = process.env.NEXT_PUBLIC_AGENT_MCP_VERSION
  if (fromEnv) return fromEnv
  try {
    const pyproject = readFileSync(join(__dirname, "..", "..", "pyproject.toml"), "utf8")
    const m = pyproject.match(/^\s*version\s*=\s*"([^"]+)"/m)
    if (m) return m[1]
  } catch {
    // pyproject not reachable (e.g. sandboxed build without the env var) —
    // fall through to the dev sentinel.
  }
  return "dev"
}

const AGENT_MCP_VERSION = resolveVersion()

const nextConfig: NextConfig = {
  // Enable static export for serving through Python backend (only in production)
  output: process.env.NODE_ENV === 'production' ? 'export' : undefined,
  
  // Output directory for the production static export. Keep the build
  // artifact INSIDE the dashboard tree (Next.js default convention) so
  // downstream packagers (Nix, Docker, etc.) don't have to chase a
  // sibling-relative path. Prior value `../static` wrote outside the
  // dashboard directory and broke sandboxed builds.
  distDir: process.env.NODE_ENV === 'production' ? 'out' : '.next',

  // Disable image optimization for static export
  images: {
    unoptimized: true
  },

  // Configure trailing slash for better static serving
  trailingSlash: true,

  // Base path configuration (can be updated if needed)
  basePath: '',

  // Asset prefix for deployments behind a path-prefixed reverse proxy.
  //
  // Phase 4 (prancy-napping-pie): the default is now a literal
  // *sentinel* string (`__AGENT_MCP_ASSET_PREFIX__`) instead of an
  // empty string or a baked-in path. Next.js embeds whatever this
  // resolves to in HTML, JS chunks, and CSS files at build time; the
  // sentinel is then substituted at *serve* time by
  // `agent_mcp/router/asset_prefix.py` with the operator's configured
  // runtime prefix.
  //
  // This inversion (prefix-agnostic build + runtime substitution)
  // means one build artifact serves every deployment URL — operators
  // wanting `/tools/` instead of `/agent-mcp/__dashboard/` just point
  // the router at the new prefix; no rebuild.
  //
  // The ASSET_PREFIX env var is still honoured as an escape hatch (a
  // local dev workflow that wants real Next-baked URLs can set it
  // before `npm run build`). Nix / CI / production builds MUST NOT
  // set the env var — let the sentinel default fire so the router
  // can do its job.
  assetPrefix: process.env.ASSET_PREFIX || '__AGENT_MCP_ASSET_PREFIX__',

  // Inline the resolved product version so client components (the sidebar
  // footer) read it via process.env at build time. See resolveVersion().
  env: {
    NEXT_PUBLIC_AGENT_MCP_VERSION: AGENT_MCP_VERSION,
  },

  eslint: {
    ignoreDuringBuilds: true,
  },
};

export default withBundleAnalyzer(nextConfig);
