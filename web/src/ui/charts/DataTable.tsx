// The accessible data table of a chart (06-web.md section 9.1): one row per bucket, newest first (repository rule),
// times as `HH:MM:SS.mmm` (section 12.4), values with units, "–" for a bucket without data.
import type { ChartProps } from './types';

export function DataTable(props: ChartProps & { readonly timeLabel: string }) {
  const rows = props.data.x.map((seconds, index) => ({ seconds, index })).reverse();
  return (
    <div className="max-h-64 overflow-auto" data-testid="monitoring.chart.table">
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
