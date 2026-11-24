// A sparkline for small screens (06-web.md section 12.1, 08-monitoring.md section 6): the first series as an SVG line
// (no chart library, nothing loaded), labelled with the newest value; gaps break the line.
import { lastValue } from '../../core/monitoring/series';
import { colorOf, type ChartProps } from './types';

const WIDTH = 120;
const HEIGHT = 32;

/** SVG path segments of `values` scaled into the sparkline box; a null value starts a new segment. */
export function sparkPath(values: readonly (number | null)[]): string {
  const max = Math.max(0, ...values.map((value) => value ?? 0)) || 1;
  const step = values.length > 1 ? WIDTH / (values.length - 1) : 0;
  let path = '';
  let open = false;
  values.forEach((value, index) => {
    if (value === null) {
      open = false;
      return;
    }
    const x = (index * step).toFixed(1);
    const y = (HEIGHT - (value / max) * HEIGHT).toFixed(1);
    path += `${open ? 'L' : 'M'}${x} ${y} `;
    open = true;
  });
  return path.trim();
}

export function Sparkline(props: ChartProps & { readonly emptyLabel: string }) {
  const first = props.data.series[0]!;
  const newest = lastValue(first.values);
  const label = `${props.labels[0]}: ${newest === null ? props.emptyLabel : props.format(newest)}`;
  return (
    <div className="flex items-center gap-2" data-testid="monitoring.chart.sparkline">
      <svg viewBox={`0 0 ${WIDTH} ${HEIGHT}`} width={WIDTH} height={HEIGHT} role="img" aria-label={label}>
        <path d={sparkPath(first.values)} fill="none" stroke={colorOf(0)} strokeWidth="2" />
      </svg>
      <span className="text-sm tabular-nums">{label}</span>
    </div>
  );
}
