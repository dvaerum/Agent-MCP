"use client"

/**
 * Mounts the dashboard's single shared TanStack Query client (Wave 6
 * keystone increment 1). The client itself is a module singleton in
 * `lib/query-client.ts` — see the docblock there for why it lives at
 * module scope (the SSE dispatcher invalidates the same cache the React
 * tree reads). This provider is a thin "use client" wrapper so the
 * server-component root layout can render it as part of the tree.
 */

import { QueryClientProvider } from "@tanstack/react-query"
import { queryClient } from "@/lib/query-client"

export function QueryProvider({
  children,
}: {
  children: React.ReactNode
}) {
  return (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  )
}
