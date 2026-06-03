import type { NextConfig } from "next";

// Bundle analyzer
const withBundleAnalyzer = require('@next/bundle-analyzer')({
  enabled: process.env.ANALYZE === 'true',
})

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

  eslint: {
    ignoreDuringBuilds: true,
  },
};

export default withBundleAnalyzer(nextConfig);
