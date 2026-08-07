"use client"

import * as React from "react"
import { Eye, Pencil, Trash2, Brain } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import type { Memory } from "@/lib/api"

/**
 * Mobile card rendering of a single memory row (CC-7 audit 2026-06-02).
 *
 * Desktop has 5 columns (Key / Value / Status / Updated / Actions).
 * Mobile collapses to a stacked card: key + brain icon on top,
 * description below, status badges + updated meta, action row.
 *
 * Value is intentionally OMITTED on mobile — values can be long
 * stringified JSON and don't fit; the user taps "View" to see them
 * in the dialog (which has its own scrollable region).
 *
 * This is a *single card* (`<li>`); the `<ul>` wrapper is provided by
 * <ResponsiveDataTable>'s `renderMobileCard` slot. Pre-foundation this
 * file exported a whole-list `<MemoriesMobileList>` — that role now
 * belongs to the shared scaffold, leaving only the per-row markup here.
 */

const SECRET_KEY_RE = /(token|secret|password|api[_-]?key|priv(?:ate)?[_-]?key)/i
const NON_SECRET_RE = /(token[_-]?(count|limit|usage|stats|description|name|kind))/i
const isSecretKey = (key: string): boolean =>
  SECRET_KEY_RE.test(key) && !NON_SECRET_RE.test(key)

interface MemoryMobileCardProps {
  memory: Memory
  onView: (memory: Memory) => void
  onEdit: (memory: Memory) => void
  onDelete: (memory: Memory) => void
}

export function MemoryMobileCard({
  memory,
  onView,
  onEdit,
  onDelete,
}: MemoryMobileCardProps): React.ReactElement {
  const meta = memory._metadata
  return (
    <li
      onClick={() => onView(memory)}
      className="px-4 py-3 hover:bg-muted/30 active:bg-muted/50 transition-colors duration-150 cursor-pointer"
    >
            <div className="flex items-start gap-3">
              <Brain className="h-4 w-4 text-primary mt-0.5 shrink-0" />
              <div className="min-w-0 flex-1">
                <div className="font-medium text-sm text-foreground break-all">
                  {memory.context_key}
                  {isSecretKey(memory.context_key) && (
                    <Badge
                      variant="outline"
                      className="ml-2 text-[10px] px-1 py-0 align-middle bg-amber-500/10 text-amber-600 border-amber-500/30"
                    >
                      redacted
                    </Badge>
                  )}
                </div>
                {memory.description && (
                  <p className="text-xs text-muted-foreground mt-1 line-clamp-2">
                    {memory.description}
                  </p>
                )}
                <div className="flex items-center flex-wrap gap-1.5 mt-2 text-[11px] text-muted-foreground">
                  {meta?.size_kb && meta.size_kb > 1 && (
                    <Badge
                      variant="outline"
                      className="text-[10px] px-1.5 py-0 tabular-nums"
                    >
                      {meta.size_kb} KB
                    </Badge>
                  )}
                  {meta?.is_stale && (
                    <Badge
                      variant="outline"
                      className="text-[10px] px-1.5 py-0 bg-orange-500/10 text-orange-600 border-orange-500/30"
                    >
                      Stale
                    </Badge>
                  )}
                  {meta?.is_large && (
                    <Badge
                      variant="outline"
                      className="text-[10px] px-1.5 py-0 bg-red-500/10 text-red-600 border-red-500/30"
                    >
                      Large
                    </Badge>
                  )}
                  <span className="ml-auto tabular-nums">
                    {memory.updated_by}
                    {" · "}
                    {memory.updated_at
                      ? new Date(memory.updated_at).toLocaleDateString(
                          undefined,
                          { month: "short", day: "numeric" },
                        )
                      : "?"}
                  </span>
                </div>
                <div className="flex items-center justify-end gap-1 mt-3">
                  <Button
                    variant="ghost"
                    size="sm"
                    title="View memory"
                    aria-label="View memory"
                    className="h-9 w-9 p-0 text-muted-foreground hover:text-foreground"
                    onClick={(e) => { e.stopPropagation(); onView(memory) }}
                  >
                    <Eye className="h-4 w-4" />
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    title="Edit memory"
                    aria-label="Edit memory"
                    className="h-9 w-9 p-0 text-primary hover:text-primary hover:bg-primary/10"
                    onClick={(e) => { e.stopPropagation(); onEdit(memory) }}
                  >
                    <Pencil className="h-4 w-4" />
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    title="Delete memory"
                    aria-label="Delete memory"
                    className="h-9 w-9 p-0 text-destructive hover:text-destructive hover:bg-destructive/10"
                    onClick={(e) => { e.stopPropagation(); onDelete(memory) }}
                  >
                    <Trash2 className="h-4 w-4" />
                  </Button>
                </div>
              </div>
            </div>
    </li>
  )
}
