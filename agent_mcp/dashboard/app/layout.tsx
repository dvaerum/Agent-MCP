import type { Metadata } from "next";
import "./globals.css";
import { ThemeProvider } from "@/components/providers/theme-provider";
import { ApiClientInitializer } from "@/components/providers/api-client-initializer";

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

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <meta name="theme-color" content="#3b82f6" />
      </head>
      <body
        className={`${inter.variable} ${jetbrainsMono.variable} font-sans antialiased`}
        suppressHydrationWarning
      >
        <ThemeProvider>
          <ApiClientInitializer />
          {children}
        </ThemeProvider>
      </body>
    </html>
  );
}
