# Dashboard dialogs clipping on mobile — `dvh`, not `vh`, and one shared shell

## What happened

A real production screenshot showed the schedules "Edit schedule" dialog
with its bottom fields (`Interval (seconds)`, `Max runs`, `Until`) and
its Save/Cancel buttons cut off behind the mobile browser's own address
bar / tab bar, with no way to scroll down to reach them.

Root cause: `agent_mcp/dashboard/components/dashboard/schedules-dashboard.tsx`
never adopted the shared `<FormDialog>` shell
(`components/dashboard/shared/form-dialog.tsx`) that tasks, groups, and
messages already use — it was a hand-rolled `<Dialog>`/`<DialogContent>`
with no height cap and no scroll region at all. `<DialogContent>` in this
codebase (`components/ui/dialog.tsx`, the shadcn primitive) has **no
default height treatment** — every caller has to opt in per-instance,
which is exactly the kind of thing that gets missed on a dialog nobody's
touched since it was first written.

## The fix, in two parts

1. **The pattern**: `max-h-[calc(100dvh-2rem)] flex flex-col
   overflow-hidden` on `<DialogContent>`, with the header and footer
   marked `flex-shrink-0` and the fields wrapped in a `flex-1 min-h-0
   overflow-y-auto` div. Header and footer stay pinned; the middle
   scrolls. `dvh` (dynamic viewport height), not `vh` — `vh` is computed
   against the *largest* possible viewport, not the one actually visible
   once a mobile browser's chrome (address bar, bottom bar) collapses or
   expands; `dvh` tracks the real visible area. This project's own prior
   fix (`4efda72`, "message-detail popup too tall") already established
   this for `view-message-modal.tsx` — the bug here was that the pattern
   never spread past that one dialog.

2. **The sweep**: fixing the reported dialog alone reproduces the exact
   whack-a-mole problem this fix exists to end. A repo-wide grep for
   every `<DialogContent>` (there's no registry — dialogs are just
   scattered JSX) found the schedules dialog plus: `DeleteConfirmModal`
   (a confirmed *regression* — `PurgeAgentDialog` had this fix directly
   before a later refactor routed it through the shared modal, which
   never got the same treatment), `project-memberships-modal.tsx` (both
   its dialogs), `users-dashboard.tsx` (both), `add/remove/rename-
   project-modal.tsx`, `send-directive-modal.tsx`, and a wider family of
   dialogs that had *some* height cap but the older, insufficient `vh`
   unit (`agent-detail-dialog.tsx`, `edit-agent-dialog.tsx`,
   `register-agent-modal.tsx`, `confirm-action-modal.tsx`, `create-
   memory-modal.tsx`, `create-prompt-modal.tsx`, `edit-memory-modal.tsx`,
   `view-memory-modal.tsx`, `prompt-book-tutorial.tsx`, `prompt-book-
   dashboard.tsx`, `view-task-dialog.tsx`, `server-management-modal.tsx`,
   `task-details-dialog.tsx`). All of them got the `dvh` treatment in the
   same pass.

## The permanent guard

`tests/test_dashboard_polish_mobile_pass.py::test_every_dialog_content_caps_height_for_mobile`
globs every `<DialogContent>` in the tree and asserts a `dvh`-based
`max-h` is present in its className — the same glob-not-hardcoded-list
idiom the file's older `test_every_dialog_content_has_mobile_width_fallback`
already uses for the `w-[calc(100vw-2rem)]` fallback, so a dialog added
tomorrow is covered automatically, and this specific bug class can't
reappear silently the way it did here.

## Prefer `<FormDialog>` over hand-rolling the pattern

The schedules dialog was migrated onto `<FormDialog>` rather than just
patched inline — it's a plain create/edit form, exactly what the shared
shell exists for (see `form-dialog.tsx`'s own docstring; tasks/groups/
messages are its other adopters). Dialogs that aren't a create/edit form
(confirmations, read-only views, the multi-step memberships modal) got
the pattern applied directly since `<FormDialog>` doesn't fit their
shape — but wherever a dialog IS a plain form, adopting the shared shell
is the fix that can't drift out of sync again, because there's only one
place the mobile treatment lives.
