"use client"

import React, { useCallback, useEffect, useMemo, useState } from 'react'
import {
  BookOpen, Search, Copy, CheckCircle2,
  UserPlus, CheckSquare, Database, Bug, Users, Sparkles, Plus, HelpCircle, X
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Textarea } from '@/components/ui/textarea'
import { Label } from '@/components/ui/label'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import {
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from '@/components/ui/tabs'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Separator } from '@/components/ui/separator'
import { useDialog } from '@/hooks/use-dialog'
import {
  searchPrompts,
  fillPromptTemplate,
  validatePromptVariables,
  type PromptTemplate,
  type PromptCategory
} from '@/lib/prompt-book'
import { useDataStore } from '@/lib/stores/data-store'
import { AgentSelect } from '@/components/dashboard/shared/agent-select'
import { CreatePromptModal, type CreatePromptData } from './modals/create-prompt-modal'
import { PromptBookTutorial, usePromptBookTutorial } from './onboarding/prompt-book-tutorial'
// CC-3 audit 2026-06-02: imported Skeleton primitive for the
// initial-mount fade so the empty UI renders briefly while
// localStorage hydration is in flight rather than flashing the
// final list at first paint. See <InitialSkeleton/> below.
import { Skeleton } from "@/components/ui/skeleton"
import { EmptyState } from "@/components/dashboard/shared/empty-state"

// Icon mapping for categories
const categoryIcons = {
  UserPlus,
  CheckSquare,
  Database,
  Bug,
  Users
}

// Component for displaying a single prompt card
const PromptCard = ({ prompt, onSelect, onDelete, isCustom }: { 
  prompt: PromptTemplate; 
  onSelect: (prompt: PromptTemplate) => void;
  onDelete?: (promptId: string) => void;
  isCustom?: boolean;
}) => {
  const [copied, setCopied] = useState(false)

  const handleCopy = async (e: React.MouseEvent) => {
    e.stopPropagation()
    await navigator.clipboard.writeText(prompt.template)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  const handleDelete = (e: React.MouseEvent) => {
    e.stopPropagation()
    if (onDelete && isCustom) {
      onDelete(prompt.id)
    }
  }

  return (
    <Card className="cursor-pointer hover:shadow-md transition-all duration-200 group relative">
      <CardHeader className="pb-3">
        <div className="flex items-start justify-between">
          <div className="flex-1">
            <div className="flex items-center gap-2">
              <CardTitle className="text-lg font-semibold group-hover:text-primary transition-colors">
                {prompt.title}
              </CardTitle>
              {isCustom && (
                <Badge variant="secondary" className="text-xs">
                  Custom
                </Badge>
              )}
            </div>
            <CardDescription className="text-sm text-muted-foreground mt-1">
              {prompt.description}
            </CardDescription>
          </div>
          <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
            <Button
              variant="ghost"
              size="sm"
              onClick={handleCopy}
              className="h-8 w-8 p-0"
            >
              {copied ? (
                <CheckCircle2 className="h-4 w-4 text-green-500" />
              ) : (
                <Copy className="h-4 w-4" />
              )}
            </Button>
            {isCustom && onDelete && (
              <Button
                variant="ghost"
                size="sm"
                onClick={handleDelete}
                className="h-8 w-8 p-0 text-destructive hover:text-destructive/80"
              >
                <X className="h-4 w-4" />
              </Button>
            )}
          </div>
        </div>
        
        <div className="flex flex-wrap gap-1 mt-2">
          {/* Defensive `?? []` guard added 2026-06-17: catalog.json
              and the zustand store both normalize `tags` to an
              array, but if this component is ever rendered with a
              prompt that bypasses the store (e.g. a future direct
              import or a test harness) the read sites must still
              survive `tags: undefined`. See PR following #166. */}
          {(prompt.tags ?? []).slice(0, 3).map(tag => (
            <Badge key={tag} variant="secondary" className="text-xs">
              {tag}
            </Badge>
          ))}
          {(prompt.tags ?? []).length > 3 && (
            <Badge variant="outline" className="text-xs">
              +{(prompt.tags ?? []).length - 3}
            </Badge>
          )}
        </div>
      </CardHeader>
      
      <CardContent className="pt-0">
        <div className="bg-muted/30 rounded-lg p-3 mb-3">
          <code className="text-xs font-mono text-foreground line-clamp-3">
            {prompt.template.length > 120 
              ? prompt.template.substring(0, 120) + '...' 
              : prompt.template
            }
          </code>
        </div>
        
        <div className="flex items-center justify-between">
          <div className="text-xs text-muted-foreground">
            {prompt.variables.length} variable{prompt.variables.length !== 1 ? 's' : ''}
          </div>
          <Button 
            variant="outline" 
            size="sm"
            onClick={() => onSelect(prompt)}
            className="text-xs"
          >
            Use Template
          </Button>
        </div>
      </CardContent>
    </Card>
  )
}

// Component for the prompt builder/editor
const PromptBuilder = ({ prompt }: {
  prompt: PromptTemplate;
  onClose: () => void;
}) => {
  const [variables, setVariables] = useState<Record<string, string>>(() => {
    const initial: Record<string, string> = {}
    prompt.variables.forEach(v => {
      initial[v.name] = ''
    })
    return initial
  })
  const [generatedPrompt, setGeneratedPrompt] = useState('')
  const [copied, setCopied] = useState(false)
  const [errors, setErrors] = useState<string[]>([])

  // Variable names tagged `source: 'agent-token'` — auto-filled from
  // the chosen sibling agent's token so the operator never pastes a
  // token by hand (UX-01).
  const agentTokenVarNames = useMemo(
    () => prompt.variables.filter(v => v.source === 'agent-token').map(v => v.name),
    [prompt.variables],
  )

  const updateVariable = (name: string, value: string) => {
    setVariables(prev => ({ ...prev, [name]: value }))
  }

  // Selecting an agent-source variable also derives any sibling
  // agent-token variable(s) from the live data store. If the agent
  // has no token yet we leave the token field untouched so the
  // operator can still fill it manually.
  const handleAgentChange = (name: string, agentId: string) => {
    setVariables(prev => {
      const next = { ...prev, [name]: agentId }
      if (agentId && agentTokenVarNames.length > 0) {
        const token = useDataStore.getState().getAgentToken(agentId)
        if (token) {
          for (const tokenVar of agentTokenVarNames) next[tokenVar] = token
        }
      }
      return next
    })
  }

  const generatePrompt = () => {
    const validationErrors = validatePromptVariables(prompt, variables)
    setErrors(validationErrors)
    
    if (validationErrors.length === 0) {
      const filled = fillPromptTemplate(prompt.template, variables)
      setGeneratedPrompt(filled)
    }
  }

  const copyPrompt = async () => {
    if (generatedPrompt) {
      await navigator.clipboard.writeText(generatedPrompt)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    }
  }

  return (
    <div className="space-y-6">
      <div>
        <h3 className="text-lg font-semibold mb-2">{prompt.title}</h3>
        <p className="text-muted-foreground text-sm mb-4">{prompt.description}</p>
        
        {/* CC-17 audit 2026-06-02: dropped the hardcoded blue
            palette (was the only theme-bypass hardcode in this file).
            Modern-minimal calls for monochrome surfaces with a single
            accent; the muted token works under both themes. */}
        <div className="bg-muted/50 border border-border rounded-md p-3">
          <p className="text-sm text-foreground">
            <strong>Usage:</strong> {prompt.usage}
          </p>
        </div>
      </div>

      <Separator />

      <div>
        <h4 className="font-medium mb-3">Configure Variables</h4>
        <div className="space-y-4">
          {prompt.variables.map(variable => (
            <div key={variable.name} className="space-y-2">
              <Label htmlFor={variable.name} className="text-sm font-medium">
                {variable.name}
                {variable.required && <span className="text-destructive ml-1">*</span>}
              </Label>
              {variable.source === 'agent' ? (
                <AgentSelect
                  id={variable.name}
                  value={variables[variable.name] || null}
                  onChange={(v) => handleAgentChange(variable.name, v ?? '')}
                  placeholder={variable.placeholder}
                />
              ) : variable.type === 'enum' && variable.options ? (
                <Select
                  value={variables[variable.name] || ''}
                  onValueChange={(v) => updateVariable(variable.name, v)}
                >
                  <SelectTrigger id={variable.name} className="font-mono text-sm">
                    <SelectValue placeholder={variable.placeholder} />
                  </SelectTrigger>
                  <SelectContent>
                    {variable.options.map(option => (
                      <SelectItem key={option} value={option}>
                        {option}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              ) : (
                <Input
                  id={variable.name}
                  value={variables[variable.name] || ''}
                  onChange={(e) => updateVariable(variable.name, e.target.value)}
                  placeholder={variable.placeholder}
                  className="font-mono text-sm"
                />
              )}
              <p className="text-xs text-muted-foreground">{variable.description}</p>
            </div>
          ))}
        </div>
        
        {errors.length > 0 && (
          <div className="mt-4 space-y-1">
            {errors.map((error, index) => (
              <p key={index} className="text-xs text-destructive">{error}</p>
            ))}
          </div>
        )}
        
        <Button onClick={generatePrompt} className="mt-4 w-full">
          <Sparkles className="h-4 w-4 mr-2" />
          Generate Prompt
        </Button>
      </div>

      {generatedPrompt && (
        <>
          <Separator />
          <div>
            <div className="flex items-center justify-between mb-3">
              <h4 className="font-medium">Generated Prompt</h4>
              <Button variant="outline" size="sm" onClick={copyPrompt}>
                {copied ? (
                  <>
                    <CheckCircle2 className="h-4 w-4 mr-2" />
                    Copied!
                  </>
                ) : (
                  <>
                    <Copy className="h-4 w-4 mr-2" />
                    Copy
                  </>
                )}
              </Button>
            </div>
            <Textarea
              value={generatedPrompt}
              readOnly
              className="font-mono text-sm h-32 bg-muted/30"
              rows={6}
            />
          </div>
        </>
      )}

      {prompt.examples && prompt.examples.length > 0 && (
        <>
          <Separator />
          <div>
            <h4 className="font-medium mb-3">Examples</h4>
            <div className="space-y-2">
              {prompt.examples.map((example, index) => (
                <div key={index} className="bg-muted/30 rounded-lg p-3">
                  <code className="text-xs font-mono text-foreground whitespace-pre-wrap">
                    {example}
                  </code>
                </div>
              ))}
            </div>
          </div>
        </>
      )}
    </div>
  )
}

export function PromptBookDashboard() {
  const [searchTerm, setSearchTerm] = useState('')
  const [selectedCategory, setSelectedCategory] = useState<string>('all')
  const [customPrompts, setCustomPrompts] = useState<PromptTemplate[]>([])
  const [createModalOpen, setCreateModalOpen] = useState(false)
  const { showTutorial, setShowTutorial } = usePromptBookTutorial()
  // CC-3 audit 2026-06-02: hydrated gate so first paint can show a
  // <Skeleton> shape rather than rendering the empty-then-populated
  // prompts list (which flashes). Flips to true on the same tick the
  // localStorage useEffect runs.
  const [hydrated, setHydrated] = useState(false)

  // Prompts catalogue from the REST-backed zustand slice. The store
  // fetches on mount via fetchPromptsCatalog() below; until the
  // response lands `promptsCatalog` is null and the skeleton renders.
  const promptsCatalog = useDataStore(s => s.promptsCatalog)
  const promptsCategories = useDataStore(s => s.promptsCategories)
  const fetchPromptsCatalog = useDataStore(s => s.fetchPromptsCatalog)
  // Memoized so the `?? []` fallback doesn't mint a fresh array every
  // render — that reference churn would re-run promptSelector's
  // useCallback and the filtered-list useMemo below on every render.
  const promptTemplates: PromptTemplate[] = useMemo(() => promptsCatalog ?? [], [promptsCatalog])
  const promptCategories: PromptCategory[] = promptsCategories ?? []

  // Prompt builder dialog. Live-lookup useDialog (Candidate D,
  // 2026-06-02) stores the prompt id and asks the selector for the
  // current row on every render — so a catalog refresh or a
  // localStorage write to customPrompts flows into the open builder
  // automatically.
  const promptSelector = useCallback(
    (id: string | null) => {
      if (!id) return null
      const all = [...promptTemplates, ...customPrompts]
      return all.find((p) => p.id === id) ?? null
    },
    [promptTemplates, customPrompts],
  )
  const builderDialog = useDialog<PromptTemplate>(promptSelector)

  // Auto-close if the prompt is removed from both catalog and
  // localStorage while the dialog is open. Depending on the stable
  // fields (.isOpen/.data/.close) rather than the whole builderDialog
  // object is deliberate — useDialog returns a fresh object each render,
  // so listing it would re-run this every render for no behavioural gain.
  useEffect(() => {
    if (builderDialog.isOpen && builderDialog.data === null) builderDialog.close()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [builderDialog.isOpen, builderDialog.data, builderDialog.close])

  // Boot the catalogue fetch on first mount.
  useEffect(() => {
    void fetchPromptsCatalog()
  }, [fetchPromptsCatalog])

  // Load custom prompts from localStorage on mount
  useEffect(() => {
    const stored = localStorage.getItem('custom-prompts')
    if (stored) {
      try {
        setCustomPrompts(JSON.parse(stored))
      } catch (error) {
        console.error('Failed to load custom prompts:', error)
      }
    }
    setHydrated(true)
  }, [])

  // Save custom prompts to localStorage when they change
  useEffect(() => {
    localStorage.setItem('custom-prompts', JSON.stringify(customPrompts))
  }, [customPrompts])

  // Filter prompts based on search and category
  const filteredPrompts = useMemo(() => {
    // Combine standard and custom prompts
    let prompts = [...promptTemplates, ...customPrompts]

    if (searchTerm) {
      // Use the search function for standard prompts, then filter custom prompts
      const standardResults = searchPrompts(promptTemplates, searchTerm)
      const customResults = customPrompts.filter(p =>
        p.title.toLowerCase().includes(searchTerm.toLowerCase()) ||
        p.description.toLowerCase().includes(searchTerm.toLowerCase()) ||
        (p.tags ?? []).some(tag => tag.toLowerCase().includes(searchTerm.toLowerCase()))
      )
      prompts = [...standardResults, ...customResults]
    }

    if (selectedCategory !== 'all') {
      prompts = prompts.filter(p => p.category === selectedCategory)
    }

    return prompts
  }, [searchTerm, selectedCategory, customPrompts, promptTemplates])

  // Group prompts by category for display
  const promptsByCategory = useMemo(() => {
    const grouped: Record<string, PromptTemplate[]> = {}
    
    filteredPrompts.forEach(prompt => {
      const list = grouped[prompt.category] ?? (grouped[prompt.category] = [])
      list.push(prompt)
    })
    
    return grouped
  }, [filteredPrompts])

  const handleSelectPrompt = (prompt: PromptTemplate) => {
    builderDialog.open(prompt.id)
  }

  const handleCreatePrompt = (promptData: CreatePromptData) => {
    const newPrompt: PromptTemplate = {
      id: `custom-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`,
      title: promptData.title,
      description: promptData.description,
      category: promptData.category,
      template: promptData.template,
      variables: promptData.variables || [],
      usage: promptData.usage || '',
      examples: [],
      tags: promptData.tags || []
    }
    setCustomPrompts(prev => [...prev, newPrompt])
    setCreateModalOpen(false)
  }

  const handleDeleteCustomPrompt = (promptId: string) => {
    setCustomPrompts(prev => prev.filter(p => p.id !== promptId))
  }

  return (
    <div className="w-full space-y-[var(--space-fluid-lg)] -mx-[var(--container-padding)] px-[var(--container-padding)] -my-[var(--space-fluid-lg)] py-[var(--space-fluid-lg)]">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
        <div>
          <h1 className="text-fluid-2xl font-bold text-foreground flex items-center gap-2">
            <BookOpen className="h-8 w-8 text-primary" />
            Prompt Book
          </h1>
          <p className="text-muted-foreground text-fluid-base mt-1">
            Standardized prompts and workflows for Agent-MCP
          </p>
        </div>
        {/* CC-23 audit 2026-06-02: added flex-wrap so the 4 badges +
            2 buttons can break to multiple rows at <sm: instead of
            overflowing the right edge at 375px. Also stripped the
            noisy shadow-lg on the primary action — modern-minimal
            calls for no shadows except on elevated surfaces (CC-5). */}
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant="outline" className="text-xs tabular-nums">
            {promptTemplates.length + customPrompts.length} prompts
          </Badge>
          <Badge variant="outline" className="text-xs tabular-nums">
            {promptCategories.length} categories
          </Badge>
          {customPrompts.length > 0 && (
            <Badge variant="secondary" className="text-xs tabular-nums">
              {customPrompts.length} custom
            </Badge>
          )}
          <Button
            size="sm"
            onClick={() => setCreateModalOpen(true)}
            className="bg-primary hover:bg-primary/90 text-primary-foreground transition-colors duration-150"
          >
            <Plus className="h-4 w-4 mr-1.5" />
            Create Prompt
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={() => setShowTutorial(true)}
            className="text-xs"
          >
            <HelpCircle className="h-4 w-4 mr-1.5" />
            Help
          </Button>
        </div>
      </div>

      {/* Search and Filter Controls */}
      <div className="flex flex-col sm:flex-row gap-4">
        <div className="relative flex-1">
          <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            placeholder="Search prompts by title, description, or tags..."
            value={searchTerm}
            onChange={(e) => setSearchTerm(e.target.value)}
            className="pl-10"
          />
        </div>
        <Select value={selectedCategory} onValueChange={setSelectedCategory}>
          <SelectTrigger className="w-full sm:w-48">
            <SelectValue placeholder="All Categories" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All Categories</SelectItem>
            {promptCategories.map(category => (
              <SelectItem key={category.id} value={category.id}>
                {category.name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      {/* Quick Start Guide */}
      <Card className="border-primary/20 bg-primary/5">
        <CardHeader>
          <CardTitle className="text-lg flex items-center gap-2">
            <Sparkles className="h-5 w-5 text-primary" />
            Quick Start
          </CardTitle>
          <CardDescription>
            Essential prompts to get started with Agent-MCP
          </CardDescription>
        </CardHeader>
        <CardContent>
          {/* Wave 7 PR 2 — coordinator transition. Step copy
              rewritten to match the register-only flow: agent-mcp
              mints the token + ready-to-paste .mcp.json snippet,
              the user owns the claude process. */}
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            <div className="flex items-center gap-2 text-sm">
              <Badge variant="secondary" className="text-xs">1</Badge>
              <span>Add Project Context</span>
            </div>
            <div className="flex items-center gap-2 text-sm">
              <Badge variant="secondary" className="text-xs">2</Badge>
              <span>Register Worker Agents</span>
            </div>
            <div className="flex items-center gap-2 text-sm">
              <Badge variant="secondary" className="text-xs">3</Badge>
              <span>Paste snippet &amp; start workers</span>
            </div>
            <div className="flex items-center gap-2 text-sm">
              <Badge variant="secondary" className="text-xs">4</Badge>
              <span>Assign Tasks</span>
            </div>
            <div className="flex items-center gap-2 text-sm">
              <Badge variant="secondary" className="text-xs">5</Badge>
              <span>Monitor &amp; Debug</span>
            </div>
          </div>
        </CardContent>
      </Card>

      {/* Category Tabs — CC-24 audit 2026-06-02: rendered the full
          category name (was `name.split(' ')[0]` which truncated
          "Agent Initialization" + "Agent Coordination" to two
          identical "Agent" tabs in the 375 px screenshot). With full
          names the tabs are wider, so the TabsList now uses
          `inline-flex w-auto overflow-x-auto` to scroll horizontally
          on narrow viewports instead of squeezing into the grid. */}
      <Tabs value={selectedCategory} onValueChange={setSelectedCategory} className="w-full">
        <TabsList className="inline-flex w-full sm:w-auto max-w-full overflow-x-auto justify-start">
          <TabsTrigger value="all" className="text-xs whitespace-nowrap">All</TabsTrigger>
          {promptCategories.map(category => (
            <TabsTrigger key={category.id} value={category.id} className="text-xs whitespace-nowrap">
              {category.name}
            </TabsTrigger>
          ))}
        </TabsList>

        <TabsContent value="all" className="mt-6">
          <div className="space-y-6">
            {promptCategories.map(category => {
              const categoryPrompts = promptsByCategory[category.id] || []
              if (categoryPrompts.length === 0) return null

              const IconComponent = categoryIcons[category.icon as keyof typeof categoryIcons] || BookOpen

              return (
                <div key={category.id}>
                  <div className="flex items-center gap-2 mb-4">
                    <IconComponent className="h-5 w-5 text-primary" />
                    <h2 className="text-xl font-semibold">{category.name}</h2>
                    <Badge variant="outline" className="text-xs">
                      {categoryPrompts.length}
                    </Badge>
                  </div>
                  <p className="text-muted-foreground text-sm mb-4">{category.description}</p>
                  
                  <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
                    {categoryPrompts.map(prompt => (
                      <PromptCard
                        key={prompt.id}
                        prompt={prompt}
                        onSelect={handleSelectPrompt}
                        onDelete={handleDeleteCustomPrompt}
                        isCustom={prompt.id.startsWith('custom-')}
                      />
                    ))}
                  </div>
                </div>
              )
            })}
          </div>
        </TabsContent>

        {promptCategories.map(category => (
          <TabsContent key={category.id} value={category.id} className="mt-6">
            <div className="space-y-4">
              <div className="flex items-center gap-2">
                <div className="p-2 rounded-lg bg-primary/10">
                  {React.createElement(categoryIcons[category.icon as keyof typeof categoryIcons] || BookOpen, {
                    className: "h-5 w-5 text-primary"
                  })}
                </div>
                <div>
                  <h2 className="text-xl font-semibold">{category.name}</h2>
                  <p className="text-muted-foreground text-sm">{category.description}</p>
                </div>
              </div>
              
              <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
                {(promptsByCategory[category.id] || []).map(prompt => (
                  <PromptCard
                    key={prompt.id}
                    prompt={prompt}
                    onSelect={handleSelectPrompt}
                    onDelete={handleDeleteCustomPrompt}
                    isCustom={prompt.id.startsWith('custom-')}
                  />
                ))}
              </div>
            </div>
          </TabsContent>
        ))}
      </Tabs>

      {/* No Results — CC-3/CC-6 audit 2026-06-02: shared EmptyState
          primitive when filtered list is empty. While hydrating
          (custom prompts not yet read from localStorage), show a
          Skeleton card grid so first paint isn't a flash of the
          final list. */}
      {!hydrated ? (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {Array.from({ length: 6 }).map((_, i) => (
            <Skeleton key={i} className="h-40 w-full" />
          ))}
        </div>
      ) : filteredPrompts.length === 0 ? (
        <EmptyState
          icon={BookOpen}
          title="No prompts found"
          description="Try adjusting your search terms or category filter."
        />
      ) : null}

      {/* Prompt Builder Dialog */}
      <Dialog
        open={builderDialog.isOpen}
        onOpenChange={(open) => { if (!open) builderDialog.close() }}
      >
        <DialogContent className="w-[calc(100vw-2rem)] sm:!max-w-4xl max-h-[90vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>Prompt Builder</DialogTitle>
            <DialogDescription>
              Customize and generate your prompt with the required variables
            </DialogDescription>
          </DialogHeader>

          {builderDialog.data && (
            <PromptBuilder
              prompt={builderDialog.data}
              onClose={() => builderDialog.close()}
            />
          )}
        </DialogContent>
      </Dialog>

      {/* Create Prompt Modal */}
      <CreatePromptModal
        open={createModalOpen}
        onOpenChange={setCreateModalOpen}
        onCreatePrompt={handleCreatePrompt}
      />

      {/* Tutorial */}
      <PromptBookTutorial
        open={showTutorial}
        onOpenChange={setShowTutorial}
      />
    </div>
  )
}