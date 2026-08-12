import { create } from 'zustand'
import { persist } from 'zustand/middleware'

interface ThemeState {
  theme: 'light' | 'dark' | 'system'
  setTheme: (theme: 'light' | 'dark' | 'system') => void
  isDark: boolean
  toggleTheme: () => void
}

export const useTheme = create<ThemeState>()(
  persist(
    (set, get) => ({
      theme: 'system',
      isDark: false,
      setTheme: (theme) => {
        set({ theme })
        
        // Only run in browser environment
        if (typeof window !== 'undefined') {
          const isDark = theme === 'dark' || 
            (theme === 'system' && window.matchMedia('(prefers-color-scheme: dark)').matches)
          set({ isDark })
          
          // Update document class
          if (isDark) {
            document.documentElement.classList.add('dark')
          } else {
            document.documentElement.classList.remove('dark')
          }
        }
      },
      toggleTheme: () => {
        const currentTheme = get().theme
        const newTheme = currentTheme === 'light' ? 'dark' : 'light'
        get().setTheme(newTheme)
      }
    }),
    {
      name: 'theme-storage',
    }
  )
)

interface SidebarState {
  isCollapsed: boolean
  setCollapsed: (collapsed: boolean) => void
  toggle: () => void
}

export const useSidebar = create<SidebarState>()((set, get) => ({
  isCollapsed: false,
  setCollapsed: (collapsed) => set({ isCollapsed: collapsed }),
  toggle: () => set({ isCollapsed: !get().isCollapsed })
}))

interface DashboardState {
  currentView: 'overview' | 'agents' | 'tasks' | 'memories' | 'messages' | 'schedules' | 'prompts' | 'settings' | 'system'
  setCurrentView: (view: 'overview' | 'agents' | 'tasks' | 'memories' | 'messages' | 'schedules' | 'prompts' | 'settings' | 'system') => void
  isLoading: boolean
  setLoading: (loading: boolean) => void
  lastUpdated: Date | null
  setLastUpdated: (date: Date) => void
}

export const useDashboard = create<DashboardState>()((set) => ({
  currentView: 'overview',
  setCurrentView: (view) => set({ currentView: view }),
  isLoading: false,
  setLoading: (loading) => set({ isLoading: loading }),
  lastUpdated: null,
  setLastUpdated: (date) => set({ lastUpdated: date })
}))