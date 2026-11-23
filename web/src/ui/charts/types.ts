// What a chart of the monitoring screen draws (06-web.md section 9.1): the aligned data of core/monitoring/series.ts,
// a label and a color per series, and the formatter of the values with their unit.
import type { ChartData } from '../../core/monitoring/series';

export interface ChartProps {
  readonly title: string;
  readonly data: ChartData;
  /** Label per series, in the order of `data.series`. */
  readonly labels: readonly string[];
  readonly format: (value: number) => string;
  /** Formats the bucket time (unix seconds) of the tooltip and the data table. */
  readonly formatTime: (seconds: number) => string;
  /** Formats an x-axis tick (unix seconds) of a chart spanning `spanSeconds`. */
  readonly formatTick: (seconds: number, spanSeconds: number) => string;
}

/** Stroke colors of the series (canvas colors, not CSS: contrast ≥ 4.5 on white). */
export const SERIES_COLORS = ['#1d4ed8', '#b45309', '#047857', '#7c3aed'] as const;

export const colorOf = (index: number): string => SERIES_COLORS[index % SERIES_COLORS.length]!;
