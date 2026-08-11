import type { Metadata } from "next";
import "./globals.css";
import { ThemeProvider } from "@/components/providers/theme-provider";
import { QueryProvider } from "@/components/providers/query-provider";
import { ProjectContextProvider } from "@/components/providers/project-context-provider";
import { McpNotificationsProvider } from "@/components/providers/mcp-notifications-provider";
import { Toaster } from "@/components/ui/toast";

// Stub the font hooks so sandboxed / offline builds (Nix, Docker
// without network egress, isolated CI) don't fail when next/font/google
// tries to fetch Inter + JetBrains_Mono from Google Fonts at compile
// time. The page falls back to system sans/mono via the CSS variables
// — visually less polished but functionally identical.
//
// Upstream-quality fix is vendoring the fonts with next/font/local
// (works online + offline + no third-party fetches). Tracked as a
// follow-up; out of scope for this PR.
const inter = { variable: "--font-sans" } as const;
const jetbrainsMono = { variable: "--font-mono" } as const;

export const metadata: Metadata = {
  title: "Agent MCP Dashboard",
  description: "Premium multi-agent system dashboard with real-time monitoring and control capabilities",
  keywords: ["agent", "mcp", "dashboard", "multi-agent", "ai", "automation"],
  authors: [{ name: "Agent MCP Team" }],
};

export const viewport = {
  width: "device-width",
  initialScale: 1,
};

// AX-4: blocking, pre-paint theme script. Runs synchronously in <head>
// before the body renders, so the correct `dark` class is on
// <html> for the very first paint — no flash of the wrong theme (FOUC).
// Mirrors the resolution logic in lib/store.ts (theme setter): the
// zustand `persist` middleware stores under localStorage key
// "theme-storage" as {state:{theme}}, and "system" follows the OS
// media query. Kept dependency-free and wrapped in try/catch so a
// disabled/again-throwing localStorage can never blank the page.
const THEME_INIT_SCRIPT = `(function(){try{var t="system";var raw=localStorage.getItem("theme-storage");if(raw){var p=JSON.parse(raw);if(p&&p.state&&p.state.theme){t=p.state.theme;}}var dark=t==="dark"||(t==="system"&&window.matchMedia("(prefers-color-scheme: dark)").matches);var c=document.documentElement.classList;if(dark){c.add("dark");}else{c.remove("dark");}}catch(e){}})();`;

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  // The PathPrefix derivation lives in `lib/project-context.ts` as a
  // module-level singleton computed at import time from
  // `window.location.pathname` (Candidate C, architecture review
  // 2026-06-01). `<ProjectContextProvider>` is a thin "use client"
  // wrapper around `<ProjectContext.Provider value={projectContext}>`
  // — required because this layout is a server component (exports
  // `metadata` + `viewport`) and `createContext` is a client-only
  // React API.
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        {/* Must run before first paint — see THEME_INIT_SCRIPT above. */}
        <script dangerouslySetInnerHTML={{ __html: THEME_INIT_SCRIPT }} />
        <meta name="theme-color" content="#3b82f6" />
      </head>
      <body
        className={`${inter.variable} ${jetbrainsMono.variable} font-sans antialiased`}
        suppressHydrationWarning
      >
        <ThemeProvider>
          <QueryProvider>
            <ProjectContextProvider>
              <McpNotificationsProvider>{children}</McpNotificationsProvider>
            </ProjectContextProvider>
          </QueryProvider>
          <Toaster />
        </ThemeProvider>
      </body>
    </html>
  );
}
