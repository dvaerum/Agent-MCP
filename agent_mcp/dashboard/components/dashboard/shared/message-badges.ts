// Colour helpers for message priority + type badges. Mirrors the
// priorityBadgeClass / statusBadgeClass pattern in tasks-dashboard.tsx
// so the two pages read as one system. Kept in one shared module so the
// desktop table, the mobile card list, and the detail modal all tint
// the same way (one canonical home — no per-file copies to drift).
//
// Message priorities are low | normal | high | urgent (distinct from
// tasks' low | medium | high). high + urgent must visually stand out:
// urgent gets the destructive tint, high a warning/orange tint.

export const priorityBadgeClass = (priority: string): string => {
  const map: Record<string, string> = {
    urgent:
      "bg-destructive/10 text-destructive border-destructive/30",
    high: "bg-orange-500/10 text-orange-500 dark:text-orange-300 border-orange-500/20",
    normal: "bg-muted text-muted-foreground border-border",
    low: "bg-muted text-muted-foreground/70 border-border",
  }
  return map[priority] || map.normal || ""
}

export const messageTypeBadgeClass = (type: string): string => {
  const map: Record<string, string> = {
    text: "bg-muted text-muted-foreground border-border",
    system: "bg-sky-500/10 text-sky-500 dark:text-sky-300 border-sky-500/20",
    notification:
      "bg-violet-500/10 text-violet-500 dark:text-violet-300 border-violet-500/20",
    task_update:
      "bg-emerald-500/10 text-emerald-500 dark:text-emerald-300 border-emerald-500/20",
    assistance_request:
      "bg-amber-500/10 text-amber-500 dark:text-amber-300 border-amber-500/20",
  }
  return map[type] || map.text || ""
}
