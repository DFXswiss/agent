// Loads rows again after every (re)connect or resync of the live channel (KOR-11, AUD-02 NA-05): row frames the hub
// sent while the channel was down, or dropped before a `resync`, are not replayed, so a screen that shows rows (tasks,
// sessions with their usage, notifications, the unread count) loads them again for each new `connection.round`. Not
// when the screen mounts: it loads its rows there itself.
import { useEffect, useRef } from 'react';
import type { Store } from '../../core/store/store';
import { useStore } from './useStore';

export function useLiveReload(store: Store, reload: () => void): void {
  const round = useStore((state) => state.connection.round, store);
  const seen = useRef(round);
  const latest = useRef(reload);
  // the newest closure (it names the screen's current task, filter, …); runs before the effect below
  useEffect(() => {
    latest.current = reload;
  });
  useEffect(() => {
    // only a new round (a sign-out resets the store and with it the count)
    if (round <= seen.current) {
      seen.current = round;
      return;
    }
    seen.current = round;
    latest.current();
  }, [round]);
}
