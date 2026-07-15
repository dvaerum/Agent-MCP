"use client"

import { useState } from 'react'
import { Trash2, AlertTriangle, Lock } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Badge } from '@/components/ui/badge'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { cn } from '@/lib/utils'
import type { Message } from '@/lib/api'

interface DeleteMessageModalProps {
  // Single-message delete (row action / in-modal Delete): pass the
  // message so the confirm dialog can show sender → recipient + a
  // content preview. Leave null for a bulk delete (see `count`).
  message?: Message | null
  // Bulk delete: number of selected messages. When >1 (and `message`
  // is null) the modal renders the "Delete N messages" variant.
  count?: number
  open: boolean
  onOpenChange: (open: boolean) => void
  // Parent owns the actual DELETE call(s). Re-throw to keep the modal
  // open on failure (mirrors DeleteMemoryModal).
  onConfirm: () => Promise<void>
}

export function DeleteMessageModal({
  message,
  count,
  open,
  onOpenChange,
  onConfirm,
}: DeleteMessageModalProps) {
  const [loading, setLoading] = useState(false)
  const [confirmationText, setConfirmationText] = useState('')
  const [error, setError] = useState<string | null>(null)

  const isBulk = !message && typeof count === 'number' && count > 0
  // Nothing to delete — don't render (mirrors DeleteMemoryModal's
  // `if (!memory) return null`).
  if (!message && !isBulk) return null

  const isConfirmed = confirmationText.toLowerCase() === 'delete'
  const requiredConfirmation = 'DELETE'

  const handleDelete = async () => {
    if (!isConfirmed) return
    setLoading(true)
    setError(null)
    try {
      await onConfirm()
      setConfirmationText('')
      onOpenChange(false)
    } catch (error) {
      console.error('Failed to delete message(s):', error)
      setError(error instanceof Error ? error.message : 'Failed to delete message')
    } finally {
      setLoading(false)
    }
  }

  const handleCancel = () => {
    setConfirmationText('')
    setError(null)
    onOpenChange(false)
  }

  const formatContent = (value: string) => {
    return value.length > 120 ? value.substring(0, 120) + '…' : value
  }

  const title = isBulk ? `Delete ${count} Messages` : 'Delete Message'

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="w-[calc(100vw-2rem)] sm:!max-w-lg bg-card border-border text-card-foreground">
        <DialogHeader>
          <div className="flex items-center gap-2">
            <div className="flex h-8 w-8 items-center justify-center rounded-full bg-destructive/15">
              <Trash2 className="h-4 w-4 text-destructive" />
            </div>
            <DialogTitle className="text-lg text-foreground">{title}</DialogTitle>
          </div>
          <DialogDescription className="text-muted-foreground">
            This action cannot be undone.{' '}
            {isBulk
              ? `The ${count} selected messages will be permanently deleted.`
              : 'The message will be permanently deleted.'}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          {/* Warning Banner */}
          <div className="flex items-start gap-3 p-3 bg-destructive/10 border border-destructive/20 rounded-lg">
            <AlertTriangle className="h-5 w-5 text-destructive mt-0.5 flex-shrink-0" />
            <div className="space-y-1">
              <div className="text-sm font-medium text-destructive">
                Permanent Data Loss Warning
              </div>
              <div className="text-xs text-destructive/80">
                {isBulk
                  ? `All ${count} selected messages will be permanently removed. This action cannot be reversed.`
                  : 'This message and its associated data will be permanently removed. This action cannot be reversed.'}
              </div>
            </div>
          </div>

          {/* Message Details — single-message variant only. */}
          {message && (
            <div className="space-y-3">
              <div className="text-sm font-medium text-foreground">Message to be deleted:</div>

              <div className="bg-muted/30 border border-border rounded-lg p-3 space-y-3">
                {/* Participants */}
                <div className="flex items-center gap-2 text-sm">
                  <Badge variant="outline">{message.sender_id}</Badge>
                  <span aria-hidden className="text-muted-foreground">→</span>
                  <Badge variant="outline">{message.recipient_id}</Badge>
                </div>

                {/* Subject */}
                {message.subject && (
                  <div>
                    <div className="text-xs font-medium text-muted-foreground mb-1">SUBJECT</div>
                    <div className="text-sm text-foreground">{message.subject}</div>
                  </div>
                )}

                {/* Content Preview */}
                <div>
                  <div className="text-xs font-medium text-muted-foreground mb-1">CONTENT PREVIEW</div>
                  <div className="text-sm text-muted-foreground bg-background border border-border rounded px-2 py-1 font-mono max-h-16 overflow-hidden">
                    {formatContent(message.message_content)}
                  </div>
                </div>

                {/* Metadata */}
                <div className="flex items-center gap-4 text-xs text-muted-foreground pt-2 border-t border-border">
                  <span>{message.timestamp.slice(0, 19)}</span>
                  <span>Type: {message.message_type}</span>
                  <span>Priority: {message.priority}</span>
                </div>
              </div>
            </div>
          )}

          {/* Confirmation Input */}
          <div className="space-y-2">
            <Label htmlFor="confirmation" className="text-sm font-medium text-foreground">
              Type <span className="font-mono font-bold text-destructive">{requiredConfirmation}</span> to confirm deletion
            </Label>
            <div className="relative">
              <Input
                id="confirmation"
                value={confirmationText}
                onChange={(e) => setConfirmationText(e.target.value)}
                placeholder={`Type "${requiredConfirmation}" to confirm`}
                className={cn(
                  "bg-background border-border text-foreground font-mono",
                  "focus:border-destructive focus:ring-destructive/20",
                  !isConfirmed && confirmationText.length > 0 && "border-destructive/50"
                )}
                disabled={loading}
              />
              <div className="absolute right-3 top-1/2 -translate-y-1/2">
                {isConfirmed ? (
                  <div className="h-2 w-2 rounded-full bg-destructive" />
                ) : (
                  <Lock className="h-3 w-3 text-muted-foreground" />
                )}
              </div>
            </div>
            {confirmationText.length > 0 && !isConfirmed && (
              <div className="text-xs text-destructive">
                Please type "{requiredConfirmation}" exactly to confirm deletion
              </div>
            )}
          </div>

          {/* Error Message */}
          {error && (
            <div className="flex items-start gap-2 p-3 bg-destructive/10 border border-destructive/20 rounded-lg">
              <AlertTriangle className="h-4 w-4 text-destructive mt-0.5 flex-shrink-0" />
              <div className="text-sm text-destructive">{error}</div>
            </div>
          )}
        </div>

        <DialogFooter className="gap-2 pt-4">
          <Button
            type="button"
            variant="outline"
            onClick={handleCancel}
            size="sm"
            disabled={loading}
          >
            Cancel
          </Button>
          <Button
            type="button"
            variant="destructive"
            onClick={handleDelete}
            size="sm"
            disabled={loading || !isConfirmed}
            className="bg-destructive hover:bg-destructive/90 text-destructive-foreground"
          >
            {loading ? (
              <>
                <div className="h-3 w-3 border-2 border-destructive-foreground border-t-transparent rounded-full animate-spin mr-2" />
                Deleting...
              </>
            ) : (
              <>
                <Trash2 className="h-3 w-3 mr-2" />
                {isBulk ? `Delete ${count} Messages` : 'Delete Message'}
              </>
            )}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
