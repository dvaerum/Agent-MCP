"use client"

import React, { useEffect } from 'react'
import { useTheme } from '@/lib/store'

interface ThemeProviderProps {
  children: React.ReactNode
}

export function ThemeProvider({ children }: ThemeProviderProps) {
  const { theme, setTheme } = useTheme()

  useEffect(() => {
    // AX-4: the blocking inline script in app/layout.tsx already applied
    // the correct `dark` class before first paint, so this provider no
    // longer withholds its children until mount (that blank-render was
    // the cause of the flash). This effect only keeps the class in sync
    // with later theme changes and OS media-query changes — it
    // re-applies idempotently on mount, a no-op against the script.
    const initializeTheme = () => {
      const isDark = theme === 'dark' || 
        (theme === 'system' && window.matchMedia('(prefers-color-scheme: dark)').matches)
      
      if (isDark) {
        document.documentElement.classList.add('dark')
      } else {
        document.documentElement.classList.remove('dark')
      }
    }
    
    initializeTheme()
    
    // Listen for system theme changes
    if (theme === 'system') {
      const mediaQuery = window.matchMedia('(prefers-color-scheme: dark)')
      const handleChange = () => {
        setTheme('system') // This will re-trigger the theme calculation
      }
      
      mediaQuery.addEventListener('change', handleChange)
      return () => mediaQuery.removeEventListener('change', handleChange)
    }
  }, [theme, setTheme])

  return <>{children}</>
}