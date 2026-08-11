// Settings resource module — project-settings + settings-schema types
// and the settings-scoped client methods (ADR-0016 / ADR-0018).

import type { ApiClient } from './client'

// A project_settings row (ADR-0016: config_* keys live in the dedicated
// settings store, not project_context). `value` is the raw JSON-encoded
// string the store carries; secret keys arrive as the literal string
// "[redacted]" for non-confirmed tiers.
export interface ProjectSetting {
  context_key: string
  value: string
  description?: string | null
  created_at?: string | null
  created_by?: string | null
  updated_at: string
  updated_by: string
}

// A single setting's schema entry, as returned by
// GET /api/settings-schema (ADR-0018). The backend registry
// (agent_mcp/core/settings_schema.py) is the single source of truth
// for every setting's default / grouping / tier / copy; the Settings
// dashboard renders itself from this list via a type→widget registry
// rather than hardcoding the spec table.
export interface SettingsSchemaEntry {
  key: string
  type: 'bool' | 'int' | 'string' | 'secret'
  default: unknown
  tier: 'operator' | 'sysadmin'
  group: 'worker_permissions' | 'event_loop' | 'retention' | 'agent_profiles' | 'scheduling'
  title: string
  description: string
  widget:
    | 'switch'
    | 'int_days'
    | 'int_ms'
    | 'int_duration'
    | 'url'
    | 'secret'
    | 'secret_path'
    | 'template'
}

// GET /api/settings-schema envelope. `caller` reports the requesting
// operator's tier so the UI can render sysadmin-tier controls disabled
// for a plain operator (instead of letting the save 403).
export interface SettingsSchemaResponse {
  schema: SettingsSchemaEntry[]
  caller: {
    sysadmin: boolean
    confirmed_operator: boolean
  }
}

/**
 * Settings-scoped client methods bound to a shared request core.
 * Assembled onto the composed client by `createApiClient()`.
 *
 * Project settings endpoints (ADR-0016). The Settings tab's store —
 * config_* toggles/knobs live in project_settings, written via the
 * gated update/delete_project_settings tools (system.config.write
 * cap; config settings are uniformly operator-tier). Same
 * cookie-session auth story as the memory endpoints.
 */
export function settingsApi(core: ApiClient) {
  return {
    getSettingsData(): Promise<{ settings: ProjectSetting[] }> {
      return core.request('/settings-data')
    },

    // Settings schema (ADR-0018). The backend registry owns every
    // setting's default / grouping / tier / copy; the Settings dashboard
    // renders itself from this list. `caller.sysadmin` drives tier-aware
    // rendering (sysadmin-tier controls disabled for a plain operator).
    // Reads only — writes still go through create/update/deleteSetting.
    getSettingsSchema(): Promise<SettingsSchemaResponse> {
      return core.request('/settings-schema')
    },

    createSetting(data: {
      context_key: string
      context_value: unknown
      description?: string
    }): Promise<{ success: boolean; message: string }> {
      return core.request('/settings', {
        method: 'POST',
        body: JSON.stringify(data),
      })
    },

    updateSetting(context_key: string, data: {
      context_value: unknown
      description?: string
    }): Promise<{ success: boolean; message: string }> {
      return core.request(`/settings/${encodeURIComponent(context_key)}`, {
        method: 'PUT',
        body: JSON.stringify(data),
      })
    },

    deleteSetting(context_key: string): Promise<{ success: boolean; message: string }> {
      return core.request(`/settings/${encodeURIComponent(context_key)}`, {
        method: 'DELETE',
        body: JSON.stringify({}),
      })
    },
  }
}
