// Loads "My subscriptions" (`loadSubscriptions`) when shown and every `REFRESH_MS` (the devices read the quotas every
// 15 minutes; a faster refresh shows nothing new), with the state of the load and a reload.
import { useCallback, useEffect, useState } from 'react';
import { requestErrorCode } from '../../core/requests/client';
import { loadSubscriptions, type SubscriptionRow } from '../../core/subscriptions/subscriptions';
import { useAppEnv } from '../shell/context';

export const REFRESH_MS = 60_000;

export interface SubscriptionsState {
  /** Null until the first load. */
  readonly rows: readonly SubscriptionRow[] | null;
  readonly error: string | null;
  readonly reload: () => void;
}

export function useSubscriptions(): SubscriptionsState {
  const { app } = useAppEnv();
  const [rows, setRows] = useState<readonly SubscriptionRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [round, setRound] = useState(0);
  const reload = useCallback(() => setRound((value) => value + 1), []);
  useEffect(() => {
    let live = true;
    const read = () =>
      loadSubscriptions(app.tasks).then(
        (found) => {
          if (!live) return;
          setRows(found);
          setError(null);
        },
        (failure: unknown) => live && setError(requestErrorCode(failure)),
      );
    void read();
    const timer = setInterval(() => void read(), REFRESH_MS);
    return () => {
      live = false;
      clearInterval(timer);
    };
  }, [app, round]);
  return { rows, error, reload };
}
