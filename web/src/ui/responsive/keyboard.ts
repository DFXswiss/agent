// The on-screen keyboard (06-web.md section 12.1: the composer stays above it). Phones shrink the visual viewport, not
// the layout viewport, when the keyboard opens; the part of the layout viewport below the visual one is covered. The
// shell hides the bottom navigation and pads the page by that much, and a focused composer scrolls itself into view.
import { type RefObject, useEffect, useSyncExternalStore } from 'react';

/** Below this many CSS pixels the difference is browser chrome (address bar), not a keyboard. */
export const KEYBOARD_MIN = 80;

export interface ViewportScope {
  readonly innerHeight: number;
  readonly visualViewport?: (EventTarget & { readonly height: number; readonly offsetTop: number }) | null;
}

/** CSS pixels of the layout viewport covered below the visual viewport (0 without a keyboard). */
export function keyboardInset(scope: ViewportScope): number {
  const viewport = scope.visualViewport;
  if (!viewport) return 0;
  const covered = Math.round(scope.innerHeight - viewport.height - viewport.offsetTop);
  return covered >= KEYBOARD_MIN ? covered : 0;
}

/** The keyboard inset of `scope` (default: the page), following the visual viewport. */
export function useKeyboardInset(scope: ViewportScope = window): number {
  return useSyncExternalStore(
    (changed) => {
      const viewport = scope.visualViewport;
      viewport?.addEventListener('resize', changed);
      viewport?.addEventListener('scroll', changed);
      return () => {
        viewport?.removeEventListener('resize', changed);
        viewport?.removeEventListener('scroll', changed);
      };
    },
    () => keyboardInset(scope),
  );
}

/** While the keyboard is open and focus is inside `box`, keeps `box` in view above the keyboard. */
export function useAboveKeyboard(box: RefObject<HTMLElement | null>, scope: ViewportScope = window): void {
  const inset = useKeyboardInset(scope);
  useEffect(() => {
    const element = box.current;
    if (inset > 0 && element !== null && element.contains(document.activeElement)) {
      element.scrollIntoView({ block: 'nearest' });
    }
  }, [box, inset]);
}
