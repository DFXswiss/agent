// The accessible data table of a chart (06-web.md section 9.1): one row per bucket, newest first (repository rule),
// times as `HH:MM:SS.mmm` (section 12.4), values with units, "–" for a bucket without data.
//
// Bounded (ADR-063 section 1): every range asks for at most `TABLE_ROWS` buckets (its length divided by its step,
// `core/monitoring/series.ts`), so the table stays a real table; points beyond that (a hub answering with more, up to
// its 20 000) are not rendered and the table says how many it shows.
import { useMemo } from 'react';
import { formatNumber } from '../../core/i18n';
import { RANGES } from '../../core/monitoring/series';
import { useI18n } from '../i18n';
import type { ChartProps } from './types';

/** The most buckets a range asks for. */
export const TABLE_ROWS = Math.max(...RANGES.map((range) => Math.ceil(range.seconds / range.step)));

export function DataTable(props: ChartProps & { readonly timeLabel: string }) {
  const { t, locale } = useI18n();
  const { x } = props.data;
  const rows = useMemo(() => {
    const newest: { seconds: number; index: number }[] = [];
    for (let index = x.length - 1; index >= 0 && newest.length < TABLE_ROWS; index -= 1) {
      newest.push({ seconds: x[index]!, index });
    }
    return newest;
  }, [x]);
  return (
    <div className="max-h-64 overflow-auto" data-testid="monitoring.chart.table">
      {x.length > rows.length && (
        <p className="px-2 py-1 text-sm text-gray-600" data-testid="monitoring.chart.table.cut">
          {t('monitoring.chart.table.cut', {
            shown: formatNumber(rows.length, locale),
            total: formatNumber(x.length, locale),
          })}
        </p>
      )}
      <table className="w-full text-left text-sm">
        <caption className="sr-only">{props.title}</caption>
        <thead>
          <tr>
            <th scope="col" className="px-2 py-1">
              {props.timeLabel}
            </th>
            {props.labels.map((label) => (
              <th key={label} scope="col" className="px-2 py-1">
                {label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map(({ seconds, index }) => (
            <tr key={seconds} className="border-t border-gray-100" data-testid="monitoring.chart.table.row">
              <th scope="row" className="px-2 py-1 font-normal tabular-nums">
                {props.formatTime(seconds)}
              </th>
              {props.data.series.map((series) => {
                const value = series.values[index]!;
                return (
                  <td key={series.metric} className="px-2 py-1 tabular-nums">
                    {value === null ? '–' : props.format(value)}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
