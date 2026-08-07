import userEvent from "@testing-library/user-event"

/**
 * The dashboard's standard `user-event` session.
 *
 * `delay: null` — the important bit
 * ---------------------------------
 * user-event v14 defaults to `delay: 0`, which is NOT "no delay": it
 * awaits a real `setTimeout(…, 0)` between every synthetic keystroke
 * and pointer step, so typing `DELETE` costs six real macrotask
 * round-trips through the event loop. On an idle box that is free; on
 * a box where Vitest has already fanned one worker per core, every
 * round-trip is a scheduling hop and the cost is unbounded. That is
 * real-time waiting inside a unit test — the classic reason a jsdom
 * suite that should finish in milliseconds instead drifts into
 * seconds and starts tripping `testTimeout` under parallel load.
 *
 * `delay: null` dispatches the whole sequence synchronously (still
 * inside `act()`, so React state settles as normal). Measured across
 * `components/**`: worst-case test 1633ms → 820ms, and the whole
 * distribution roughly halves. Nothing here asserts on intermediate
 * per-keystroke state, so there is nothing to observe in the gaps.
 *
 * `pointerEventsCheck: 0` — Radix renders its dialog content in a
 * portal with `pointer-events: none` on the body while the overlay is
 * up; jsdom has no layout engine, so user-event's pointer-events
 * assertion has nothing meaningful to check and would reject clicks
 * the real browser accepts.
 *
 * Use `setupUser()` for anything that drives a Radix dialog/portal;
 * `setupUserPlain()` for flat DOM where the pointer-events check still
 * carries signal.
 */
export const setupUser = () =>
  userEvent.setup({ pointerEventsCheck: 0, delay: null })

/** As `setupUser`, but keeping user-event's pointer-events assertion. */
export const setupUserPlain = () => userEvent.setup({ delay: null })
