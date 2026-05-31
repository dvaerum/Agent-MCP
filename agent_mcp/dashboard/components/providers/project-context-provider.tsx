"use client"

/**
 * Client wrapper that hosts `<ProjectContext.Provider>` for the
 * dashboard's PathPrefix singleton (Candidate C, architecture review
 * 2026-06-01).
 *
 * Exists because:
 *   - `lib/project-context.ts` is a "use client" module (it calls
 *     React.createContext, which is client-only).
 *   - `app/layout.tsx` is a server component (it exports `metadata`
 *     and `viewport`, both server-only).
 *   - Next.js's Server/Client boundary requires the Provider itself
 *     to be inside a "use client" module before a server component
 *     can render it.
 *
 * The Provider value is the module-level singleton `projectContext`,
 * computed at import time from `window.location.pathname` (or its
 * SSR fallback). No state, no effects, no re-renders triggered by
 * this wrapper.
 */

import { ProjectContext, projectContext } from "@/lib/project-context"

export function ProjectContextProvider({
  children,
}: {
  children: React.ReactNode
}) {
  return (
    <ProjectContext.Provider value={projectContext}>
      {children}
    </ProjectContext.Provider>
  )
}
