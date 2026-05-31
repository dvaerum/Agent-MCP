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
  // Driven by the ASSET_PREFIX env var at build time. Default empty
  // preserves upstream behavior (assets served at site root). Path-
  // prefixed deployments set ASSET_PREFIX (e.g. ASSET_PREFIX=/agent-mcp/__dashboard)
  // so the webpack runtime's public path matches where the proxy
  // serves the static tree.
  assetPrefix: process.env.ASSET_PREFIX || '',

  eslint: {
    ignoreDuringBuilds: true,
  },
};

export default withBundleAnalyzer(nextConfig);
