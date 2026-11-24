// Small parts of the monitoring screen: a labelled value, the freshness note (stale values greyed with their age,
// 08-monitoring.md section 6) counted by the shared second clock, and the toggle buttons of the drill-down and the
// charts.
import { memo, type ReactNode } from 'react';
import { type Freshness, freshness, type Sampled } from '../../../core/monitoring/overview';
import { useI18n } from '../../i18n';
import { useSecond } from './clock';
import { useUnits } from './units';

export function Metric(props: { readonly label: string; readonly testId: string; readonly children: ReactNode }) {
  return (
    <div className="flex flex-col" data-testid={props.testId}>
      <dt className="text-xs text-gray-600">{props.label}</dt>
      <dd className="tabular-nums">{props.children}</dd>
    </div>
  );
}

/** The value or "–" when there is none. */
export const orNone = (value: number | null, format: (value: number) => string): string =>
  value === null ? '–' : format(value);

export function FreshnessNote(props: { readonly freshness: Freshness; readonly testId: string }) {
  const { t } = useI18n();
  const units = useUnits();
  const { stale, ageMs } = props.freshness;
  return (
    <span
      className={`text-xs ${stale ? 'rounded bg-gray-100 px-1 text-gray-700' : 'text-gray-600'}`}
      data-testid={props.testId}
      data-stale={String(stale)}
    >
      {ageMs === null
        ? t('monitoring.no-data')
        : t(stale ? 'monitoring.stale' : 'monitoring.updated', { age: units.duration(ageMs / 1000) })}
    </span>
  );
}

/** The age of an entity's newest value: the only cell that re-renders every second. */
export const Age = memo(function Age(props: { readonly item: Sampled; readonly testId: string }) {
  const now = useSecond();
  return <FreshnessNote freshness={freshness(props.item, now)} testId={props.testId} />;
});

export function Toggle(props: {
  readonly open: boolean;
  readonly onToggle: () => void;
  readonly testId: string;
  /** The panel the button shows (a chart panel); a drill-down adds rows to the list instead. */
  readonly controls?: string;
  readonly children: ReactNode;
}) {
  return (
    <button
      type="button"
      className="min-h-11 rounded border border-gray-300 px-3 text-sm"
      aria-expanded={props.open}
      aria-controls={props.controls}
      onClick={props.onToggle}
      data-testid={props.testId}
    >
      {props.children}
    </button>
  );
}
