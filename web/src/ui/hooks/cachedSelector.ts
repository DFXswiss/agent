// A selector that keeps its result while its inputs keep their identity (ADR-086 section 1): a stable value for
// `useStore`. Used by the audit-like lists and the notifications of the first load (`screens/notifications/text.tsx`).
import type { Selector, StoreState } from '../../core/store';

/**
 * A selector that runs `compute` only when one of the values `inputs` reads from the state changed (by identity), and
 * otherwise returns the previous result: a stable value for `useStore`.
 */
export function cachedSelector<T>(
  inputs: (state: StoreState) => readonly unknown[],
  compute: (state: StoreState) => T,
): Selector<T> {
  let known: readonly unknown[] | null = null;
  let value: T;
  return (state) => {
    const now = inputs(state);
    if (known === null || now.some((input, index) => input !== known![index])) {
      known = now;
      value = compute(state);
    }
    return value;
  };
}
