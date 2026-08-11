"use client"

import React, { useEffect, useState } from "react"
import { ChevronDown, ChevronRight, Loader2, Shield } from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { toastError, toastSuccess } from "@/components/ui/toast"
import { routerGroupCapabilitiesUrl } from "@/lib/urls"
import {
  CAPABILITY_DESCRIPTIONS,
  CAPABILITY_RESOURCE_LABELS,
  groupCapabilitiesByResource,
} from "@/lib/capability-descriptions"
import { routerApi } from "@/lib/router-api"
import { ApiError } from "@/lib/api"
import { useGroupCapabilitiesQuery } from "@/lib/queries/group-capabilities"
import { invalidateGroupCapabilities } from "@/lib/query-client"

// Capabilities (Wave 9 PR 5) — extracted in Wave 5 into the groups/
// subfolder alongside the rest of the members/capabilities detail UI.

export function GroupCapabilitiesSection({
  groupId,
  groupName,
}: {
  groupId: string
  groupName: string
}): React.ReactElement {
  // Three load states:
  //   * ``loaded``    GET succeeded → render the checklist.
  //   * ``forbidden`` GET returned 403 → we are not sysadmin; show
  //                   the read-only / disabled message (plan: "show
  //                   but don't allow edit; tooltip 'requires sysadmin'").
  //   * ``loading`` — transient banner. Errors go to the shared toast.
  //
  // ``loaded`` / ``selected`` / ``forbidden`` stay LOCAL state (not
  // query-owned) rather than reading straight off ``useGroupCapabilitiesQuery``'s
  // ``data`` — ``save()`` below writes an OPTIMISTIC result into them
  // straight from the PUT response (no extra GET round-trip), and the
  // checklist's dirty-tracking (``selected``) needs to be
  // independently editable. The query still owns the GET's own
  // loading/error/forbidden bookkeeping; a sync effect below folds its
  // outcome into the local state exactly the way the old inline
  // ``load()`` used to.
  const [loaded, setLoaded] = React.useState<string[] | null>(null)
  const [selected, setSelected] = React.useState<Set<string>>(new Set())
  const [forbidden, setForbidden] = React.useState(false)
  const [saving, setSaving] = React.useState(false)

  // useRouterQuery folded a 403 into a `forbidden` flag; useQuery surfaces
  // it as a thrown ApiError on `error`, so we re-derive `loadForbidden`
  // (mirrors groups-dashboard.tsx). `isPending` (not `isFetching`) drives
  // the loading banner: the post-save invalidateGroupCapabilities() below
  // triggers a background refetch, and using `isFetching` would flash the
  // whole checklist away behind "Loading capabilities…" on every save.
  const query = useGroupCapabilitiesQuery(groupId)
  const loading = query.isPending
  const fetchedCaps = query.data ?? null
  const loadForbidden =
    query.error instanceof ApiError && query.error.status === 403
  const loadError = loadForbidden ? null : (query.error ?? null)

  // Mirrors the old ``load()``'s synchronous reset — as soon as a
  // fetch starts, any stale forbidden from a previous attempt is
  // cleared.
  useEffect(() => {
    if (loading) {
      setForbidden(false)
    }
  }, [loading])

  useEffect(() => {
    if (fetchedCaps !== null) {
      setLoaded(fetchedCaps)
      setSelected(new Set(fetchedCaps))
    }
  }, [fetchedCaps])

  useEffect(() => {
    // 403 = not sysadmin: render the read-only message, not an error.
    if (loadForbidden) {
      setForbidden(true)
      setLoaded([])
      setSelected(new Set())
    }
  }, [loadForbidden])

  useEffect(() => {
    if (loadError) {
      toastError(loadError, "Failed to load capabilities")
    }
  }, [loadError])

  const dirty = React.useMemo(() => {
    if (loaded === null) return false
    if (loaded.length !== selected.size) return true
    for (const cap of loaded) {
      if (!selected.has(cap)) return true
    }
    return false
  }, [loaded, selected])

  const toggleCap = (cap: string) => {
    setSelected((cur) => {
      const next = new Set(cur)
      if (next.has(cap)) next.delete(cap)
      else next.add(cap)
      return next
    })
  }

  const cancel = () => {
    if (loaded !== null) {
      setSelected(new Set(loaded))
    }
  }

  const save = async () => {
    setSaving(true)
    try {
      const body = await routerApi.request<{
        success: true
        capabilities: string[]
      }>(routerGroupCapabilitiesUrl(groupId), {
        method: "PUT",
        body: JSON.stringify({ capabilities: [...selected] }),
      })
      const newCaps = body.capabilities ?? []
      setLoaded(newCaps)
      setSelected(new Set(newCaps))
      // Reconcile the query cache with the PUT result so a remount reads
      // the fresh set — the router-admin analog of useRouterQuery's
      // post-mutation refresh() (no SSE at the overview). The optimistic
      // setLoaded/setSelected above keep the UI instant; this refetch
      // confirms in the background (isPending stays false, so no flicker).
      void invalidateGroupCapabilities(groupId)
      // Was the page's REINVENTED toast (a local `setToast` + green
      // <div>); same message, now the shared toast module.
      toastSuccess(
        `Saved — ${groupName} now has ${newCaps.length} capabilit${
          newCaps.length === 1 ? "y" : "ies"
        }`,
      )
    } catch (e) {
      // 403 = not sysadmin: flag forbidden + a specific hint.
      if (e instanceof ApiError && e.status === 403) {
        setForbidden(true)
        toastError(
          "requires sysadmin — group capabilities are sysadmin-only",
          "Failed to save capabilities",
        )
      } else {
        toastError(e, "Failed to save capabilities")
      }
    } finally {
      setSaving(false)
    }
  }

  const grouped = React.useMemo(() => {
    // Render every KNOWN cap (sourced from the description registry —
    // the build-time test ``capability-descriptions-complete`` keeps
    // it in lockstep with ``core/capabilities.py::KNOWN_CAPABILITIES``).
    // We bucket by resource so the UI matches the mental model
    // operators have when reading the bundle table.
    const allKnown = Object.keys(CAPABILITY_DESCRIPTIONS)
    return groupCapabilitiesByResource(allKnown)
  }, [])

  return (
    <div className="border-t pt-3 mt-3 space-y-2">
      <div className="flex items-center justify-between">
        <div className="text-xs font-semibold text-muted-foreground uppercase flex items-center gap-2">
          <Shield className="h-3 w-3" /> Capabilities
        </div>
        {dirty && !forbidden && (
          <div className="flex items-center gap-1">
            <Button
              size="sm"
              variant="ghost"
              onClick={cancel}
              disabled={saving}
            >
              Cancel
            </Button>
            <Button
              size="sm"
              onClick={save}
              disabled={saving}
            >
              {saving && <Loader2 className="h-3 w-3 mr-1 animate-spin" />}
              Save
            </Button>
          </div>
        )}
      </div>
      <p className="text-xs text-muted-foreground italic">
        Capabilities here are added on top of what each user&apos;s project
        role already grants. To remove a baseline capability, change
        PROJECT_ROLE_BUNDLES in source.
      </p>
      {loading && (
        <div className="flex items-center gap-2 text-muted-foreground text-sm">
          <Loader2 className="h-3 w-3 animate-spin" /> Loading capabilities…
        </div>
      )}
      {forbidden && !loading && (
        <div
          className="text-sm text-muted-foreground bg-muted/40 rounded px-2 py-1"
          title="requires sysadmin"
        >
          Requires sysadmin to view or edit capabilities for this group.
        </div>
      )}
      {!loading && !forbidden && (
        <div className="space-y-1">
          {grouped.map(({ resource, caps }) => (
            <CapabilityResourceSection
              key={resource}
              resource={resource}
              caps={caps}
              selected={selected}
              disabled={saving}
              onToggle={toggleCap}
            />
          ))}
        </div>
      )}
    </div>
  )
}

function CapabilityResourceSection({
  resource,
  caps,
  selected,
  disabled,
  onToggle,
}: {
  resource: string
  caps: string[]
  selected: Set<string>
  disabled: boolean
  onToggle: (cap: string) => void
}): React.ReactElement {
  const [open, setOpen] = useState(true)
  const label = CAPABILITY_RESOURCE_LABELS[resource] ?? resource
  const onCount = caps.filter((c) => selected.has(c)).length
  return (
    <div className="border rounded bg-background">
      <button
        type="button"
        className="w-full flex items-center justify-between px-2 py-1 text-left text-sm"
        onClick={() => setOpen((v) => !v)}
      >
        <span className="flex items-center gap-1 font-medium">
          {open ? (
            <ChevronDown className="h-3 w-3" />
          ) : (
            <ChevronRight className="h-3 w-3" />
          )}
          {label}
        </span>
        <Badge variant="secondary" className="text-xs">
          {onCount} / {caps.length}
        </Badge>
      </button>
      {open && (
        <div className="px-3 py-2 space-y-1 border-t">
          {caps.map((cap) => (
            <label
              key={cap}
              className="flex items-start gap-2 text-xs cursor-pointer"
              title={CAPABILITY_DESCRIPTIONS[cap]}
            >
              <input
                type="checkbox"
                checked={selected.has(cap)}
                disabled={disabled}
                onChange={() => onToggle(cap)}
                className="mt-0.5"
              />
              <span className="flex-1">
                <code className="font-mono text-[11px] mr-1">{cap}</code>
                <span className="text-muted-foreground">
                  {CAPABILITY_DESCRIPTIONS[cap]}
                </span>
              </span>
            </label>
          ))}
        </div>
      )}
    </div>
  )
}
