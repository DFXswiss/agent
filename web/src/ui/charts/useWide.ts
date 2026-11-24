// Whether the viewport is at least the `md` breakpoint (768 px, 06-web.md section 12.1): full charts there, sparklines
// below. Without `matchMedia` (not a supported browser, section 12.6) the full charts are shown.
import { useEffect, useState } from 'react';

export const WIDE_QUERY = '(min-width: 768px)';

const query = (): MediaQueryList | null =>
  typeof window.matchMedia === 'function' ? window.matchMedia(WIDE_QUERY) : null;

export function useWide(): boolean {
  const [wide, setWide] = useState(() => query()?.matches ?? true);
  useEffect(() => {
    const list = query();
    if (list === null) return;
    const change = () => setWide(list.matches);
    list.addEventListener('change', change);
    return () => list.removeEventListener('change', change);
  }, []);
  return wide;
}
