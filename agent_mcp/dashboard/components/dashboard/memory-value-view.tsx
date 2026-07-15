"use client"

/**
 * Rich, human-readable renderer for a memory value (Wave 13).
 *
 * SECURITY: memory content is agent-authored → untrusted. This component
 * NEVER uses `dangerouslySetInnerHTML`. Markdown goes through
 * `react-markdown` WITHOUT `rehype-raw`, so embedded raw HTML
 * (`<script>`, `<img onerror>`) renders as inert escaped text. Links are
 * gated through the `isSafeHref` allowlist (http/https/mailto only);
 * anything else (`javascript:`, `data:`) is dropped to plain text.
 */

import React, { useMemo, useState } from "react"
import Markdown from "react-markdown"
import { Copy, Check, ExternalLink } from "lucide-react"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"
import {
  decodeMemoryValue,
  isSafeHref,
  type MemoryFormat,
} from "@/lib/memory-value"

/** The Raw view is the always-available escape hatch. */
export type ViewFormat = "auto" | MemoryFormat | "raw"

// The user-selectable overrides. `url` is auto-detect-only (a URL is a
// degenerate string; when overriding, the user picks Text). Auto still
// resolves to the `url` renderer.
const SELECTOR_OPTIONS: { value: ViewFormat; label: string }[] = [
  { value: "auto", label: "Auto" },
  { value: "json", label: "JSON" },
  { value: "markdown", label: "Markdown" },
  { value: "text", label: "Text" },
  { value: "raw", label: "Raw" },
]

const linkClass =
  "text-primary underline underline-offset-2 hover:text-primary/80 break-words"

/**
 * A single sanitized anchor: forces noopener/noreferrer + _blank and drops
 * disallowed schemes to inert text.
 */
function SafeLink({
  href,
  children,
  className,
}: {
  href?: string | null
  children: React.ReactNode
  className?: string
}) {
  if (!isSafeHref(href)) {
    // Unsafe/relative/scheme-less → render the visible text, no link.
    return <span className={className}>{children}</span>
  }
  return (
    <a
      href={href as string}
      target="_blank"
      rel="noopener noreferrer"
      className={cn(linkClass, className)}
    >
      {children}
    </a>
  )
}

/**
 * Render markdown safely. Exported so tests can render it in isolation
 * (via react-dom/server) and assert XSS inertness.
 */
export function SafeMarkdown({ source }: { source: string }) {
  return (
    <div className="text-sm text-foreground space-y-2 break-words">
      <Markdown
        components={{
          a: ({ href, children }) => <SafeLink href={href}>{children}</SafeLink>,
          h1: ({ children }) => <h1 className="text-lg font-semibold mt-2">{children}</h1>,
          h2: ({ children }) => <h2 className="text-base font-semibold mt-2">{children}</h2>,
          h3: ({ children }) => <h3 className="text-sm font-semibold mt-2">{children}</h3>,
          ul: ({ children }) => <ul className="list-disc pl-5 space-y-1">{children}</ul>,
          ol: ({ children }) => <ol className="list-decimal pl-5 space-y-1">{children}</ol>,
          code: ({ children }) => (
            <code className="font-mono text-xs bg-muted/60 rounded px-1 py-0.5">{children}</code>
          ),
          pre: ({ children }) => (
            <pre className="font-mono text-xs bg-muted/40 border border-border rounded-lg p-3 overflow-x-auto">
              {children}
            </pre>
          ),
          p: ({ children }) => <p className="leading-relaxed">{children}</p>,
          table: ({ children }) => (
            <div className="overflow-x-auto">
              <table className="text-xs border-collapse">{children}</table>
            </div>
          ),
          th: ({ children }) => <th className="border border-border px-2 py-1 text-left">{children}</th>,
          td: ({ children }) => <td className="border border-border px-2 py-1">{children}</td>,
        }}
      >
        {source}
      </Markdown>
    </div>
  )
}

function CopyButton({ text, label = "Copy" }: { text: string; label?: string }) {
  const [copied, setCopied] = useState(false)
  return (
    <Button
      variant="ghost"
      size="sm"
      className="h-6 gap-1 px-2 text-xs"
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text)
          setCopied(true)
          setTimeout(() => setCopied(false), 1500)
        } catch (err) {
          console.error("Failed to copy:", err)
        }
      }}
    >
      {copied ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}
      {copied ? "Copied" : label}
    </Button>
  )
}

interface MemoryValueViewProps {
  /** The raw stored value (JSON-encoded string, or an already-parsed value). */
  value: unknown
  className?: string
}

/**
 * Full memory-value surface: a format selector (Auto/JSON/Markdown/Text/Raw)
 * over the auto-detected rich renderer, with a Copy button.
 */
export function MemoryValueView({ value, className }: MemoryValueViewProps) {
  const decoded = useMemo(() => decodeMemoryValue(value), [value])
  const [format, setFormat] = useState<ViewFormat>("auto")

  const effective: MemoryFormat | "raw" = format === "auto" ? decoded.format : format

  // The text copied by the Copy button matches what the user is looking at.
  const copyText = useMemo(() => {
    if (effective === "raw") return decoded.raw
    if (effective === "json") {
      const payload = decoded.format === "json" ? decoded.payload : decoded.raw
      try {
        return JSON.stringify(payload, null, 2)
      } catch {
        return decoded.raw
      }
    }
    return typeof decoded.payload === "string" ? decoded.payload : decoded.raw
  }, [decoded, effective])

  return (
    <div className={cn("space-y-2", className)}>
      {/* Format selector + Copy */}
      <div className="flex items-center justify-between gap-2">
        <div className="inline-flex rounded-lg border border-border bg-muted/30 p-0.5 text-xs">
          {SELECTOR_OPTIONS.map((opt) => (
            <button
              key={opt.value}
              type="button"
              onClick={() => setFormat(opt.value)}
              aria-pressed={format === opt.value}
              className={cn(
                "px-2.5 py-1 rounded-md transition-colors",
                format === opt.value
                  ? "bg-background text-foreground shadow-sm font-medium"
                  : "text-muted-foreground hover:text-foreground",
              )}
            >
              {opt.label}
              {opt.value === "auto" && (
                <span className="ml-1 uppercase opacity-60">{decoded.format}</span>
              )}
            </button>
          ))}
        </div>
        <CopyButton text={copyText} />
      </div>

      {/* Body — no inner vertical scroll: the value block grows to its
          natural height and the modal (DialogContent max-h-[90vh]
          overflow-y-auto) is the single vertical scroller, so large JSON
          no longer produces a nested "double scroll". Wide lines still
          scroll horizontally inside each <pre> (overflow-x-auto), which
          preserves JSON indentation. */}
      <div className="bg-muted/30 border border-border rounded-lg p-3">
        <MemoryValueBody decoded={decoded} effective={effective} />
      </div>
    </div>
  )
}

function MemoryValueBody({
  decoded,
  effective,
}: {
  decoded: ReturnType<typeof decodeMemoryValue>
  effective: MemoryFormat | "raw"
}) {
  // Raw ALWAYS shows the exact stored string verbatim.
  if (effective === "raw") {
    return (
      <pre className="font-mono text-xs text-foreground overflow-x-auto whitespace-pre">
        {decoded.raw}
      </pre>
    )
  }

  if (effective === "json") {
    // If the value isn't really JSON but the user forced JSON, fall back
    // to the raw string rather than throwing.
    const payload = decoded.format === "json" ? decoded.payload : decoded.raw
    let pretty: string
    try {
      pretty = JSON.stringify(payload, null, 2)
    } catch {
      pretty = decoded.raw
    }
    return (
      <pre className="font-mono text-xs text-foreground overflow-x-auto whitespace-pre">
        {pretty}
      </pre>
    )
  }

  if (effective === "url") {
    const url = String(decoded.payload)
    return (
      <SafeLink href={url} className="inline-flex items-center gap-1 text-sm">
        {url}
        {isSafeHref(url) && <ExternalLink className="h-3 w-3 flex-shrink-0" />}
      </SafeLink>
    )
  }

  if (effective === "markdown") {
    const source =
      typeof decoded.payload === "string" ? decoded.payload : decoded.raw
    return <SafeMarkdown source={source} />
  }

  // text — preserve newlines.
  const text = typeof decoded.payload === "string" ? decoded.payload : decoded.raw
  return (
    <div className="text-sm text-foreground whitespace-pre-wrap break-words">{text}</div>
  )
}
