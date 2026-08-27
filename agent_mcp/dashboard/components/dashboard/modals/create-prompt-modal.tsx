"use client"

import { useState } from "react"
import { Plus, Sparkles, Tag, Hash } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { Label } from "@/components/ui/label"
import { Badge } from "@/components/ui/badge"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { FormDialog } from "@/components/dashboard/shared/form-dialog"
import { useDataStore } from "@/lib/stores/data-store"

export interface CreatePromptData {
  title: string
  description: string
  category: string
  template: string
  usage: string
  variables: Array<{
    name: string
    description: string
    placeholder: string
    required: boolean
  }>
  tags: string[]
}

interface CreatePromptModalProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  onCreatePrompt: (data: CreatePromptData) => void
}

const EMPTY_FORM: CreatePromptData = {
  title: "",
  description: "",
  category: "coordination",
  template: "",
  usage: "",
  variables: [],
  tags: [],
}

/**
 * Create-prompt modal — adopts the shared <FormDialog> shell
 * (`wide` for its multi-column layout). `onCreatePrompt` is
 * synchronous local state (no real async failure mode today), so the
 * main value of this migration is the mobile-safe scroll chrome — the
 * variable/tag lists can grow long, the exact clipping shape this
 * effort targets — not the async plumbing. A synchronous throw from
 * `onCreatePrompt` still surfaces via `errorMessage` (an async
 * `onSubmit` wrapping a throwing call rejects the same as an awaited
 * one) — this migration also fixes that path, which previously only
 * `console.error`'d silently.
 */
export function CreatePromptModal({ open, onOpenChange, onCreatePrompt }: CreatePromptModalProps) {
  // Categories come from the REST-backed promptsCatalog slice now —
  // see lib/stores/data-store.ts. Fall back to an empty list while
  // the catalogue is still loading; the <Select> stays valid (the
  // submit gate rejects an empty form anyway).
  const promptCategories = useDataStore((s) => s.promptsCategories) ?? []
  const [formData, setFormData] = useState<CreatePromptData>(EMPTY_FORM)
  const [newVariable, setNewVariable] = useState({
    name: "",
    description: "",
    placeholder: "",
    required: false,
  })
  const [newTag, setNewTag] = useState("")

  const resetForm = () => {
    setFormData(EMPTY_FORM)
    setNewVariable({ name: "", description: "", placeholder: "", required: false })
    setNewTag("")
  }

  const handleSubmit = async () => {
    onCreatePrompt(formData)
    resetForm()
  }

  const addVariable = () => {
    if (newVariable.name.trim()) {
      setFormData((prev) => ({
        ...prev,
        variables: [...prev.variables, { ...newVariable }],
      }))
      setNewVariable({
        name: "",
        description: "",
        placeholder: "",
        required: false,
      })
    }
  }

  const removeVariable = (index: number) => {
    setFormData((prev) => ({
      ...prev,
      variables: prev.variables.filter((_, i) => i !== index),
    }))
  }

  const addTag = () => {
    if (newTag.trim() && !formData.tags.includes(newTag.trim())) {
      setFormData((prev) => ({
        ...prev,
        tags: [...prev.tags, newTag.trim()],
      }))
      setNewTag("")
    }
  }

  const removeTag = (tag: string) => {
    setFormData((prev) => ({
      ...prev,
      tags: prev.tags.filter((t) => t !== tag),
    }))
  }

  const detectVariables = () => {
    const template = formData.template
    const variableMatches = template.match(/{{([^}]+)}}/g)

    if (variableMatches) {
      const detectedVars = variableMatches
        .map((match) => match.slice(2, -2))
        .filter((varName) => !formData.variables.some((v) => v.name === varName))
        .map((varName) => ({
          name: varName,
          description: `Description for ${varName}`,
          placeholder: `Enter ${varName.toLowerCase()}`,
          required: true,
        }))

      setFormData((prev) => ({
        ...prev,
        variables: [...prev.variables, ...detectedVars],
      }))
    }
  }

  return (
    <FormDialog
      open={open}
      onOpenChange={onOpenChange}
      title="Create Custom Prompt"
      description="Create your own reusable prompt template for Agent-MCP workflows"
      icon={Sparkles}
      wide
      onSubmit={handleSubmit}
      submitLabel="Create Prompt"
      submittingLabel="Creating…"
      submitDisabled={!formData.title.trim() || !formData.template.trim()}
      errorMessage="Failed to create prompt"
    >
      {/* Basic Information */}
      <div className="space-y-4">
        <h3 className="text-sm font-medium text-foreground">Basic Information</h3>

        <div className="grid gap-4 sm:grid-cols-2">
          <div className="space-y-2">
            <Label htmlFor="title">
              Title <span className="text-destructive">*</span>
            </Label>
            <Input
              id="title"
              value={formData.title}
              onChange={(e) => setFormData((prev) => ({ ...prev, title: e.target.value }))}
              placeholder="e.g., Create API Worker Agent"
              required
            />
          </div>

          <div className="space-y-2">
            <Label htmlFor="category">Category</Label>
            <Select value={formData.category} onValueChange={(value) => setFormData((prev) => ({ ...prev, category: value }))}>
              <SelectTrigger id="category">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {promptCategories.map((category) => (
                  <SelectItem key={category.id} value={category.id}>
                    {category.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </div>

        <div className="space-y-2">
          <Label htmlFor="description">Description</Label>
          <Textarea
            id="description"
            value={formData.description}
            onChange={(e) => setFormData((prev) => ({ ...prev, description: e.target.value }))}
            placeholder="Brief description of what this prompt does..."
            className="h-20"
            rows={3}
          />
        </div>
      </div>

      {/* Template */}
      <div className="space-y-4">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-medium text-foreground">Prompt Template</h3>
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={detectVariables}
            className="text-xs"
          >
            <Hash className="mr-1 h-3 w-3" />
            Detect Variables
          </Button>
        </div>

        <div className="space-y-2">
          <Label htmlFor="template">
            Template <span className="text-destructive">*</span>
          </Label>
          <Textarea
            id="template"
            value={formData.template}
            onChange={(e) => setFormData((prev) => ({ ...prev, template: e.target.value }))}
            placeholder="Create a worker agent with ID {{AGENT_ID}} to {{TASK_DESCRIPTION}}..."
            className="h-32 font-mono text-sm"
            rows={6}
            required
          />
          <div className="text-xs text-muted-foreground">
            Use <code className="rounded bg-muted px-1">{"{{VARIABLE_NAME}}"}</code> syntax for dynamic variables
          </div>
        </div>

        <div className="space-y-2">
          <Label htmlFor="usage">Usage Instructions</Label>
          <Textarea
            id="usage"
            value={formData.usage}
            onChange={(e) => setFormData((prev) => ({ ...prev, usage: e.target.value }))}
            placeholder="Explain when and how to use this prompt..."
            className="h-20"
            rows={3}
          />
        </div>
      </div>

      {/* Variables */}
      <div className="space-y-4">
        <h3 className="text-sm font-medium text-foreground">Variables</h3>

        {formData.variables.length > 0 && (
          <div className="max-h-32 space-y-2 overflow-y-auto">
            {formData.variables.map((variable, index) => (
              <div key={variable.name} className="flex items-center gap-2 rounded-lg bg-muted/30 p-2">
                <div className="flex-1 text-sm">
                  <span className="font-mono text-primary">{variable.name}</span>
                  {variable.required && <span className="ml-1 text-destructive">*</span>}
                  {variable.description && (
                    <span className="ml-2 text-muted-foreground">- {variable.description}</span>
                  )}
                </div>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  onClick={() => removeVariable(index)}
                  aria-label="Remove variable"
                  className="h-6 w-6 p-0 text-destructive hover:text-destructive/80"
                >
                  ×
                </Button>
              </div>
            ))}
          </div>
        )}

        <div className="grid gap-2 sm:grid-cols-3">
          <Input
            value={newVariable.name}
            onChange={(e) => setNewVariable((prev) => ({ ...prev, name: e.target.value }))}
            placeholder="Variable name"
            className="font-mono text-sm"
          />
          <Input
            value={newVariable.description}
            onChange={(e) => setNewVariable((prev) => ({ ...prev, description: e.target.value }))}
            placeholder="Description"
            className="text-sm"
          />
          <div className="flex gap-1">
            <Input
              value={newVariable.placeholder}
              onChange={(e) => setNewVariable((prev) => ({ ...prev, placeholder: e.target.value }))}
              placeholder="Placeholder"
              className="flex-1 text-sm"
            />
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={addVariable}
              className="px-2"
            >
              <Plus className="h-3 w-3" />
            </Button>
          </div>
        </div>
      </div>

      {/* Tags */}
      <div className="space-y-4">
        <h3 className="text-sm font-medium text-foreground">Tags</h3>

        {formData.tags.length > 0 && (
          <div className="mb-2 flex flex-wrap gap-1">
            {formData.tags.map((tag) => (
              <Badge
                key={tag}
                variant="secondary"
                className="cursor-pointer text-xs hover:bg-destructive hover:text-destructive-foreground"
                onClick={() => removeTag(tag)}
              >
                {tag} ×
              </Badge>
            ))}
          </div>
        )}

        <div className="flex gap-2">
          <Input
            value={newTag}
            onChange={(e) => setNewTag(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                // stopPropagation: FormDialog's shell also listens for
                // Enter (onEnterSubmit) to submit the whole form — this
                // field's Enter means "add this tag", not "submit".
                e.preventDefault()
                e.stopPropagation()
                addTag()
              }
            }}
            placeholder="Add tag..."
            className="flex-1 text-sm"
          />
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={addTag}
            className="px-3"
          >
            <Tag className="h-3 w-3" />
          </Button>
        </div>
      </div>
    </FormDialog>
  )
}
