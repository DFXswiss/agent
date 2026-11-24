// Focus handling of modal dialogs and sheets (06-web.md section 12.3): keyboard operable with a focus trap – Tab and
// Shift+Tab cycle through the focusable elements of the box, Escape closes it; focus moves to the first control on
// open and back to the opener on close.
import { type KeyboardEvent, type RefObject, useEffect } from 'react';

export const FOCUSABLE =
  'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), a[href], ' +
  '[tabindex="0"]';

/** Tab and Shift+Tab cycle through the focusable elements of `box`; Escape calls `close`. */
export function trapKey(event: KeyboardEvent<HTMLElement>, box: HTMLElement, close: () => void): void {
  if (event.key === 'Escape') {
    event.preventDefault();
    close();
    return;
  }
  if (event.key !== 'Tab') return;
  const items = [...box.querySelectorAll<HTMLElement>(FOCUSABLE)];
  const first = items[0];
  const last = items[items.length - 1];
  if (first === undefined || last === undefined) return;
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}

/** On mount: focus the first focusable element of `box`; on unmount: focus the element that was focused before. */
export function useInitialFocus(box: RefObject<HTMLElement | null>): void {
  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null;
    box.current!.querySelector<HTMLElement>(FOCUSABLE)?.focus();
    return () => opener?.focus();
  }, [box]);
}
