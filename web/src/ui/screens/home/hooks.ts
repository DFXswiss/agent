// Hooks of the situation view (06-web.md section 4.0): the counters of an org, kept current – loaded when shown, after
// every batch of live `attention` frames and after every reconnect, at most once per second (`refreshSummary`).
import { useEffect } from 'react';
import { refreshSummary } from '../../../core/attention/load';
import type { AttentionSummary } from '../../../core/store/slices/attention';
import { useLiveReload } from '../../hooks/useLiveReload';
import { useStore } from '../../hooks/useStore';
import { useAppEnv } from '../../shell/context';

export function useSummary(org: string): AttentionSummary | null {
  const { app } = useAppEnv();
  const changes = useStore((state) => state.attention.changes, app.store);
  useEffect(() => {
    refreshSummary(app.tasks, org);
  }, [app, org, changes]);
  useLiveReload(app.store, () => refreshSummary(app.tasks, org));
  return useStore((state) => state.attention.summaries[org] ?? null, app.store);
}
