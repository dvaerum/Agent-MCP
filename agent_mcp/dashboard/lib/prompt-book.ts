// Prompt Book — shared types + pure helper functions.
//
// History: this file used to inline both `promptCategories` and the
// 470-line `promptTemplates` array as TypeScript literals. PR #67
// hoisted the data into `agent_mcp/prompts/catalog.json` as the single
// source of truth, exposed via `GET /api/prompts/catalog`. The
// dashboard now reads via the zustand `promptsCatalog` slice in
// lib/stores/data-store.ts.
//
// What's left here:
//   * `PromptTemplate` / `PromptCategory` types — still the canonical
//     shape consumers import.
//   * Pure helper functions (`getPromptsByCategory`, `searchPrompts`,
//     `getPromptById`, `fillPromptTemplate`, `validatePromptVariables`,
//     `getRequiredVariables`). Each accepts the catalogue as an
//     explicit argument so consumers pull the catalogue from
//     `useDataStore(s => s.promptsCatalog)` and pass it in. This
//     keeps the helpers tree-shakeable and testable without a
//     zustand context.

export interface PromptTemplate {
  id: string
  title: string
  description: string
  category: string
  template: string
  variables: Array<{
    name: string
    description: string
    placeholder: string
    required: boolean
  }>
  usage: string
  examples?: string[]
  tags: string[]
}

export interface PromptCategory {
  id: string
  name: string
  description: string
  icon: string
}

// ---- Helpers ---------------------------------------------------------------

export function getPromptsByCategory(
  catalog: PromptTemplate[] | null | undefined,
  categoryId: string,
): PromptTemplate[] {
  if (!catalog) return []
  return catalog.filter(prompt => prompt.category === categoryId)
}

export function searchPrompts(
  catalog: PromptTemplate[] | null | undefined,
  query: string,
): PromptTemplate[] {
  if (!catalog) return []
  const lowercaseQuery = query.toLowerCase()
  return catalog.filter(prompt =>
    prompt.title.toLowerCase().includes(lowercaseQuery) ||
    prompt.description.toLowerCase().includes(lowercaseQuery) ||
    // Defensive `?? []` guard added 2026-06-17 alongside the
    // catalog.json backfill + store normalization. See the
    // `s.tags is undefined` Firefox-MCP regression for context.
    (prompt.tags ?? []).some(tag => tag.toLowerCase().includes(lowercaseQuery)) ||
    prompt.template.toLowerCase().includes(lowercaseQuery)
  )
}

export function getPromptById(
  catalog: PromptTemplate[] | null | undefined,
  id: string,
): PromptTemplate | undefined {
  if (!catalog) return undefined
  return catalog.find(prompt => prompt.id === id)
}

export function fillPromptTemplate(
  template: string,
  variables: Record<string, string>,
): string {
  let filled = template
  Object.entries(variables).forEach(([key, value]) => {
    const regex = new RegExp(`{{${key}}}`, 'g')
    filled = filled.replace(regex, value)
  })
  return filled
}

export function getRequiredVariables(prompt: PromptTemplate): string[] {
  return prompt.variables.filter(v => v.required).map(v => v.name)
}

export function validatePromptVariables(
  prompt: PromptTemplate,
  variables: Record<string, string>,
): string[] {
  const errors: string[] = []
  const required = getRequiredVariables(prompt)

  required.forEach(varName => {
    if (!variables[varName] || variables[varName].trim() === '') {
      errors.push(`Required variable "${varName}" is missing or empty`)
    }
  })

  return errors
}
