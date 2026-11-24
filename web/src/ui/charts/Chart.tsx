// One chart of the monitoring screen (06-web.md section 9.1): the uPlot chart (lazy chunk) on wide screens, a sparkline
// on phones (section 12.1), "No data" for a series without values, and the "Data table" toggle with the accessible
// table of all buckets.
import { lazy, Suspense, useId, useState } from 'react';
import { hasData } from '../../core/monitoring/series';
import { useI18n } from '../i18n';
import { ChunkBoundary } from '../lazy/ChunkBoundary';
import { DataTable } from './DataTable';
import { Sparkline } from './Sparkline';
import type { ChartProps } from './types';
import { useWide } from './useWide';

const UPlotChart = lazy(() => import('./UPlotChart'));

export function Chart(props: ChartProps & { readonly id: string }) {
  const { t } = useI18n();
  const wide = useWide();
  const [table, setTable] = useState(false);
  const titleId = useId();
  const empty = !hasData(props.data);
  return (
    <section
      className="flex flex-col gap-2 rounded border border-gray-200 p-3"
      aria-labelledby={titleId}
      data-testid="monitoring.chart"
      data-chart={props.id}
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 id={titleId} className="font-medium">
          {props.title}
        </h3>
        <button
          type="button"
          className="min-h-11 rounded border border-gray-300 px-3 text-sm"
          aria-pressed={table}
          onClick={() => setTable((shown) => !shown)}
          data-testid="monitoring.chart.table-toggle"
        >
          {t('monitoring.chart.table')}
        </button>
      </div>
      {empty ? (
        <p className="text-sm text-gray-600" data-testid="monitoring.chart.empty">
          {t('monitoring.chart.empty')}
        </p>
      ) : wide ? (
        <ChunkBoundary fallback={<Sparkline {...props} emptyLabel={t('monitoring.value.none')} />}>
          <Suspense fallback={<p className="text-sm text-gray-600">{t('monitoring.chart.loading')}</p>}>
            <UPlotChart {...props} />
          </Suspense>
        </ChunkBoundary>
      ) : (
        <Sparkline {...props} emptyLabel={t('monitoring.value.none')} />
      )}
      {table && <DataTable {...props} timeLabel={t('monitoring.chart.time')} />}
    </section>
  );
}
