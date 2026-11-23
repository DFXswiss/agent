// A clock for the countdown panels: the browser time, re-read every `interval` ms while `active` (the panel stops it
// once the start is over, so a finished panel causes no renders).
import { useEffect, useState } from 'react';

export const TICK_MS = 100;

export function useNow(active: boolean, interval: number = TICK_MS): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    const timer = setInterval(() => setNow(Date.now()), interval);
    return () => clearInterval(timer);
  }, [active, interval]);
  return now;
}
