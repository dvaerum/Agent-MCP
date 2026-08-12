"use client"

import React, { useEffect } from 'react'
import { useTheme } from '@/lib/store'

interface ThemeProviderProps {
  children: React.ReactNode
}

export function ThemeProvider({ children }: ThemeProviderProps) {
  const { theme } = useTheme()

  useEffect(() => {
    // AX-4: the blocking inline script in app/layout.tsx already applied
    // the correct `dark` class before first paint, so this provider no
    // longer withholds its children until mount (that blank-render was
    // the cause of the flash). This effect only keeps the class in sync
    // with later theme changes and OS media-query changes — it
    // re-applies idempotently on mount, a no-op against the script.
    const applyTheme = () => {
      const isDark = theme === 'dark' ||
        (theme === 'system' && window.matchMedia('(prefers-color-scheme: dark)').matches)

      document.documentElement.classList.toggle('dark', isDark)
    }

    applyTheme()

    // In `system` mode, follow OS theme flips live. AF-B fix: apply the
    // `dark` class directly here — the previous `setTheme('system')` set
    // the store to its *current* value, so the effect never re-ran and
    // the class never re-toggled when the OS theme changed under us.
    if (theme === 'system') {
      const mediaQuery = window.matchMedia('(prefers-color-scheme: dark)')
      mediaQuery.addEventListener('change', applyTheme)
      return () => mediaQuery.removeEventListener('change', applyTheme)
    }
  }, [theme])

  return <>{children}</>
}