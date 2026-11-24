// The opened state of a device request while a flow shows it (`core/requests/watch.ts`: pulled on `device_response`
// frames and on a timer, stopped on unmount or when the request ended). `null` request: nothing is watched.
import { useEffect, useState } from 'react';
import type { PreparedRequest, RequestView } from '../../../../core/requests/client';
import { watchRequest } from '../../../../core/requests/watch';
import { useAppEnv } from '../../../shell/context';

export interface Watched {
  readonly view: RequestView | null;
  /** The code of the last failed read (cleared by the next good one). */
  readonly readError: string | null;
}

export function useRequestView(prepared: PreparedRequest | null): Watched {
  const { app } = useAppEnv();
  const [watched, setWatched] = useState<Watched>({ view: null, readError: null });
  useEffect(() => {
    if (prepared === null) return;
    return watchRequest(app.tasks, prepared, {
      onView: (view) => setWatched({ view, readError: null }),
      onError: (code) => setWatched((before) => ({ ...before, readError: code })),
    });
  }, [app, prepared]);
  return watched;
}
