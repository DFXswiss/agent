// The counters of the task dashboard (E-43, ADR-101 section 1): per status and per urgency the number of tasks of the
// shown organizations under the hub filters of the list (`core/tasks/counts.ts`). A click on a counter sets that
// filter, a click on the chosen one clears it. They load again when a filter changes, when a row of the shown
// workgroups appears or changes its status or urgency (a live row frame), and after a reconnect (KOR-11).
import { useEffect, useMemo, useState } from 'react';
import { formatNumber } from '../../../core/i18n';
import { loadCounts, selectCountsSignature, sumCounts, type TaskCounts } from '../../../core/tasks/counts';
import { STATUSES, URGENCIES } from '../../../core/tasks/tickets';
import { useLiveReload } from '../../hooks/useLiveReload';
import { useStore } from '../../hooks/useStore';
import { useI18n } from '../../i18n';
import { useAppEnv } from '../../shell/context';

interface Loaded {
  readonly counts: TaskCounts | null;
  readonly failed: boolean;
}

/** The summed counters of `orgs` for the list key `filter` (one workgroup: `wg`); null until the first answer. */
export function useTaskCounts(
  orgs: readonly string[],
  filter: string | null,
  wg: string | null,
  wgs: readonly string[],
): Loaded {
  const { app } = useAppEnv();
  const key = orgs.join(',');
  const wgsKey = wgs.join(',');
  const select = useMemo(() => selectCountsSignature(wgsKey), [wgsKey]);
  const signature = useStore(select, app.store);
  const [round, setRound] = useState(0);
  const [loaded, setLoaded] = useState<Loaded>({ counts: null, failed: false });
  useLiveReload(app.store, () => setRound((value) => value + 1));
  useEffect(() => {
    let current = true;
    Promise.all((key === '' ? [] : key.split(',')).map((org) => loadCounts(app.tasks, org, filter, wg))).then(
      (all) => {
        if (current) setLoaded({ counts: sumCounts(all), failed: false });
      },
      () => {
        if (current) setLoaded((before) => ({ counts: before.counts, failed: true }));
      },
    );
    return () => {
      current = false;
    };
  }, [app, key, filter, wg, signature, round]);
  return loaded;
}

const BUTTON =
  'flex min-h-11 min-w-11 items-center gap-2 rounded border px-3 text-sm aria-pressed:border-blue-700 ' +
  'aria-pressed:bg-blue-50 aria-pressed:font-semibold border-gray-300';

export interface TaskCountersProps {
  readonly loaded: Loaded;
  /** The chosen status and urgency ("" = any). */
  readonly status: string;
  readonly urgency: string;
  readonly onStatus: (status: string) => void;
  readonly onUrgency: (urgency: string) => void;
}

export function TaskCounters({ loaded, status, urgency, onStatus, onUrgency }: TaskCountersProps) {
  const { t, locale } = useI18n();
  const { counts, failed } = loaded;
  const count = (value: number | undefined) => (value === undefined ? '–' : formatNumber(value, locale));
  const group = (
    name: 'status' | 'urgency',
    values: readonly string[],
    chosen: string,
    numbers: Readonly<Record<string, number>> | undefined,
    choose: (value: string) => void,
  ) => (
    <div
      role="group"
      aria-label={t(`tasks.dashboard.by-${name}`)}
      className="flex flex-wrap gap-2"
      data-testid={`dashboard.counts.${name}`}
    >
      {values.map((value) => (
        <button
          key={value}
          type="button"
          className={BUTTON}
          aria-pressed={chosen === value}
          onClick={() => choose(chosen === value ? '' : value)}
          data-testid={`dashboard.count.${name}.${value}`}
          data-count={numbers?.[value] ?? ''}
        >
          <span>{t(`tickets.${name}.${value}`)}</span>
          <span className="font-semibold tabular-nums">{count(numbers?.[value])}</span>
        </button>
      ))}
    </div>
  );
  return (
    <section className="flex flex-col gap-2" aria-busy={counts === null && !failed} data-testid="dashboard.counts">
      <p className="text-sm text-gray-700" data-testid="dashboard.counts.total" data-count={counts?.total ?? ''}>
        {t('tasks.dashboard.total', { count: count(counts?.total) })}
      </p>
      {group('status', STATUSES, status, counts?.byStatus, onStatus)}
      {group('urgency', URGENCIES, urgency, counts?.byUrgency, onUrgency)}
      {failed && (
        <p role="alert" className="text-sm text-red-800" data-testid="dashboard.counts.error">
          {t('tasks.dashboard.counts-failed')}
        </p>
      )}
    </section>
  );
}
