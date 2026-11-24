// The charts of one device, VM or session (06-web.md section 9.1, 08-monitoring.md section 6): a range switch (15 min,
// 1 h, 6 h, 24 h from raw samples; 7, 30, 90 days from rollups) and the charts CPU, memory, disk (workspace for a
// session) and network, each with legend, tooltip and data table. The series are read when the panel opens or the
// range changes; the slice keeps only the series of the shown window.
import { type KeyboardEvent, useEffect, useMemo, useState } from 'react';
import type { EntityKind } from '../../../core/monitoring/overview';
import {
  CHARTS,
  type ChartSpec,
  DEFAULT_RANGE,
  RANGES,
  rangeOf,
  seriesKey,
  type SeriesRequest,
  seriesWindow,
} from '../../../core/monitoring/series';
import { dropSeries, loadSeries, selectChart } from '../../../core/store/slices/monitoring';
import { useStore } from '../../hooks/useStore';
import { useI18n } from '../../i18n';
import { useAppEnv } from '../../shell/context';
import { Chart } from '../../charts/Chart';
import { useUnits } from './units';

function ChartOf(props: {
  readonly kind: EntityKind;
  readonly spec: ChartSpec;
  readonly requests: readonly SeriesRequest[];
}) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const units = useUnits();
  const select = useMemo(() => selectChart(props.spec, props.requests), [props.spec, props.requests]);
  const chart = useStore(select, app.store);
  const workspace = props.kind === 'session' && props.spec.id === 'disk';
  const title = t(`monitoring.chart.${props.spec.id}${workspace ? '.workspace' : ''}`);
  if (chart.status === 'error')
    return (
      <p className="text-sm text-red-800" role="alert" data-testid="monitoring.chart.error">
        {t('monitoring.chart.failed', { title, code: chart.code! })}
      </p>
    );
  if (chart.data === null) return <p className="text-sm text-gray-600">{t('monitoring.chart.loading')}</p>;
  return (
    <Chart
      id={props.spec.id}
      title={title}
      data={chart.data}
      labels={props.spec.metrics.map((metric) => t(`monitoring.metric.${metric}`))}
      format={units.of(props.spec.unit)}
      formatTime={(seconds) => units.time(seconds * 1000)}
      formatTick={units.tick}
    />
  );
}

export function EntityCharts(props: { readonly kind: EntityKind; readonly id: string }) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const units = useUnits();
  const [rangeId, setRangeId] = useState(DEFAULT_RANGE);
  const range = rangeOf(rangeId);
  // arrow keys move the choice and the focus (a radio group, 06-web.md section 12.3)
  const keys = (event: KeyboardEvent<HTMLDivElement>) => {
    const step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[event.key];
    if (step === undefined) return;
    event.preventDefault();
    const index = (RANGES.indexOf(range) + step + RANGES.length) % RANGES.length;
    setRangeId(RANGES[index]!.id);
    const buttons = event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="radio"]');
    buttons[index]!.focus();
  };
  // the window is fixed when the panel opens or the range changes (a new window means new requests)
  const window = useMemo(() => seriesWindow(range, app.tasks.now()), [range, app]);
  const requests = useMemo(
    () =>
      CHARTS.map((spec) => ({
        spec,
        requests: spec.metrics.map((metric) => ({ kind: props.kind, id: props.id, metric, window })),
      })),
    [props.kind, props.id, window],
  );
  useEffect(() => {
    const all = requests.flatMap((chart) => chart.requests);
    for (const request of all) void loadSeries(app.tasks, request);
    return () => dropSeries(app.store, all.map(seriesKey));
  }, [app, requests]);
  return (
    <div className="flex flex-col gap-3" data-testid="monitoring.charts" data-range={range.id}>
      <div role="radiogroup" aria-label={t('monitoring.range.label')} className="flex flex-wrap gap-1" onKeyDown={keys}>
        {RANGES.map((item) => (
          <button
            key={item.id}
            type="button"
            role="radio"
            aria-checked={item.id === range.id}
            tabIndex={item.id === range.id ? 0 : -1}
            className={`min-h-11 min-w-11 rounded border px-2 text-sm ${item.id === range.id ? 'border-blue-700 bg-blue-700 text-white' : 'border-gray-300'}`}
            onClick={() => setRangeId(item.id)}
            data-testid={`monitoring.range.${item.id}`}
          >
            {t(`monitoring.range.${item.id}`)}
          </button>
        ))}
      </div>
      <p className="text-xs text-gray-600" data-testid="monitoring.range.source">
        {t(range.rollup ? 'monitoring.range.rollups' : 'monitoring.range.raw', { step: units.duration(range.step) })}
      </p>
      <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
        {requests.map((chart) => (
          <ChartOf key={chart.spec.id} kind={props.kind} spec={chart.spec} requests={chart.requests} />
        ))}
      </div>
    </div>
  );
}
