import type { KeyboardEvent } from "react"

// Returns an onKeyDown handler that fires `action` when Enter is pressed
// AND `ready` is true. Ignores Enter during IME composition (isComposing
// / keyCode 229) so CJK/dead-key input isn't hijacked. preventDefault so
// the keypress doesn't also do anything else.
export function onEnterSubmit(ready: boolean, action: () => void) {
  return (e: KeyboardEvent) => {
    if (e.key !== "Enter") return
    if (e.nativeEvent.isComposing || e.keyCode === 229) return
    if (!ready) return
    e.preventDefault()
    action()
  }
}
