// Time series of the monitoring charts (03-api.md section 4.11, 08-monitoring.md section 6): ranges 15 min, 1 h, 6 h,
// 24 h from the raw samples (48 h retention, `step` >= 15 s) and 7, 30, 90 days from the 5-minute rollups
// (`step` >= 300 s). Each range has a fixed step (at most a few hundred points per series); the window is aligned to
// the step so that repeated requests ask for the same buckets. A chart shows one or two metrics of one entity, each
// read with its own request; the network counters are shown as rates (bytes per second between buckets).
import { HubError } from '../net/signedFetch';
import { formatTime } from '../encoding/time';
import { isRecord } from '../registry';
import type { EntityKind } from './overview';

export interface Range {
  readonly id: string;
  readonly seconds: number;
  /** Bucket size in seconds. */
  readonly step: number;
  /** Read from the rollups (older than the raw retention). */
  readonly rollup: boolean;
}

export const RANGES: readonly Range[] = [
  { id: '15m', seconds: 900, step: 15, rollup: false },
  { id: '1h', seconds: 3600, step: 15, rollup: false },
  { id: '6h', seconds: 21_600, step: 60, rollup: false },
  { id: '24h', seconds: 86_400, step: 300, rollup: false },
  { id: '7d', seconds: 604_800, step: 1800, rollup: true },
  { id: '30d', seconds: 2_592_000, step: 7200, rollup: true },
  { id: '90d', seconds: 7_776_000, step: 21_600, rollup: true },
];

export const DEFAULT_RANGE = '1h';

/** The range with `id` (the default range for an unknown one). */
export function rangeOf(id: string | null): Range {
  return RANGES.find((range) => range.id === id) ?? RANGES.find((range) => range.id === DEFAULT_RANGE)!;
}

export interface SeriesWindow {
  /** Unix ms, inclusive. */
  readonly from: number;
  /** Unix ms, exclusive. */
  readonly to: number;
  readonly step: number;
}

/** The window of `range` that ends with the bucket holding `now`. */
export function seriesWindow(range: Range, now: number): SeriesWindow {
  const stepMs = range.step * 1000;
  const to = (Math.floor(now / stepMs) + 1) * stepMs;
  return { from: to - range.seconds * 1000, to, step: range.step };
}

export type Metric =
  | 'cpu_ratio'
  | 'mem_used_bytes'
  | 'mem_limit_bytes'
  | 'disk_used_bytes'
  | 'disk_limit_bytes'
  | 'net_rx_bytes'
  | 'net_tx_bytes';

export type ChartUnit = 'cores' | 'bytes' | 'rate';

export interface ChartSpec {
  readonly id: 'cpu' | 'memory' | 'disk' | 'network';
  readonly unit: ChartUnit;
  readonly metrics: readonly Metric[];
}

export const CHARTS: readonly ChartSpec[] = [
  { id: 'cpu', unit: 'cores', metrics: ['cpu_ratio'] },
  { id: 'memory', unit: 'bytes', metrics: ['mem_used_bytes', 'mem_limit_bytes'] },
  { id: 'disk', unit: 'bytes', metrics: ['disk_used_bytes', 'disk_limit_bytes'] },
  { id: 'network', unit: 'rate', metrics: ['net_rx_bytes', 'net_tx_bytes'] },
];

export interface SeriesRequest {
  readonly kind: EntityKind;
  readonly id: string;
  readonly metric: Metric;
  readonly window: SeriesWindow;
}

/** The store key of a series request. */
export const seriesKey = (request: SeriesRequest): string =>
  [request.kind, request.id, request.metric, request.window.from, request.window.to, request.window.step].join(' ');

export function seriesTarget(request: SeriesRequest): string {
  const query = new URLSearchParams({
    entity_kind: request.kind,
    entity_id: request.id,
    metric: request.metric,
    from: formatTime(request.window.from),
    to: formatTime(request.window.to),
    step: String(request.window.step),
  });
  return `/v1/monitoring/series?${query.toString()}`;
}

/** `[unix ms, value]`. */
export type Point = readonly [number, number | null];

/** The points of a series response; a malformed body is `unavailable`, malformed points are left out. */
export function parsePoints(body: unknown): Point[] {
  if (!isRecord(body) || !Array.isArray(body.points)) throw new HubError('unavailable', 200);
  const points: Point[] = [];
  for (const item of body.points) {
    if (!Array.isArray(item) || typeof item[0] !== 'string') continue;
    const at = Date.parse(item[0]);
    const value: unknown = item[1];
    if (Number.isNaN(at) || (value !== null && (typeof value !== 'number' || !Number.isFinite(value)))) continue;
    points.push([at, value as number | null]);
  }
  return points;
}

/**
 * Counter values → bytes per second since the previous point with a value (the average over a gap); null for the
 * first value, for buckets without one and after a counter reset (a smaller value: the device restarted).
 */
export function counterRates(points: readonly Point[]): Point[] {
  let before: readonly [number, number] | null = null;
  return points.map(([at, value]): Point => {
    if (value === null) return [at, null];
    const rate = before === null || value < before[1] ? null : (value - before[1]) / ((at - before[0]) / 1000);
    before = [at, value];
    return [at, rate];
  });
}

export interface ChartSeries {
  readonly metric: Metric;
  readonly values: readonly (number | null)[];
}

export interface ChartData {
  /** Unix seconds of the buckets (uPlot's x axis). */
  readonly x: readonly number[];
  readonly series: readonly ChartSeries[];
}

/**
 * The data of a chart from the points of its metrics (same window, so the same buckets): x from the first metric,
 * values aligned by bucket time (rates for the `rate` unit).
 */
export function chartData(spec: ChartSpec, points: Readonly<Partial<Record<Metric, readonly Point[]>>>): ChartData {
  const shown = spec.metrics.map((metric) => {
    const raw = points[metric] ?? [];
    return { metric, points: spec.unit === 'rate' ? counterRates(raw) : raw };
  });
  const times = [...new Set(shown.flatMap((item) => item.points.map(([at]) => at)))].sort((a, b) => a - b);
  return {
    x: times.map((at) => at / 1000),
    series: shown.map((item) => {
      const byTime = new Map(item.points.map(([at, value]) => [at, value]));
      return { metric: item.metric, values: times.map((at) => byTime.get(at) ?? null) };
    }),
  };
}

/** The newest non-null value of a series, null if none. */
export function lastValue(values: readonly (number | null)[]): number | null {
  for (let index = values.length - 1; index >= 0; index -= 1) {
    const value = values[index]!;
    if (value !== null) return value;
  }
  return null;
}

/** Whether any series has a value. */
export const hasData = (data: ChartData): boolean => data.series.some((item) => lastValue(item.values) !== null);
