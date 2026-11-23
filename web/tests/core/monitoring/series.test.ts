// U-WEB-monitoring-series: ranges, aligned windows, series requests, counter rates and chart data (06-web.md section
// 9.1, 08-monitoring.md section 6, 03-api.md section 4.11).
import { describe, expect, it } from 'vitest';
import {
  CHARTS,
  chartData,
  counterRates,
  DEFAULT_RANGE,
  hasData,
  lastValue,
  parsePoints,
  RANGES,
  rangeOf,
  seriesKey,
  seriesTarget,
  seriesWindow,
} from '../../../src/core/monitoring/series';
import { HubError } from '../../../src/core/net/signedFetch';
import { DEV, iso, T0 } from './fixtures';

const RAW_RETENTION_S = 48 * 3600;

describe('ranges and windows', () => {
  it('offers the ranges of 08 section 6 within the hub limits', () => {
    expect(RANGES.map((range) => range.id)).toEqual(['15m', '1h', '6h', '24h', '7d', '30d', '90d']);
    for (const range of RANGES) {
      expect(range.rollup).toBe(range.seconds > RAW_RETENTION_S);
      expect(range.step).toBeGreaterThanOrEqual(range.rollup ? 300 : 15);
      if (range.rollup) expect(range.step % 300).toBe(0);
      expect(range.seconds / range.step).toBeLessThanOrEqual(400);
    }
    expect(rangeOf('7d').id).toBe('7d');
    expect(rangeOf(null).id).toBe(DEFAULT_RANGE);
    expect(rangeOf('2y').id).toBe(DEFAULT_RANGE);
  });

  it('aligns the window to the step and ends after now', () => {
    const range = rangeOf('1h');
    const now = T0 + 7_500;
    const window = seriesWindow(range, now);
    expect(window.to).toBe(T0 + 15_000);
    expect(window.to - window.from).toBe(3600_000);
    expect(window.step).toBe(15);
    expect(seriesWindow(range, T0 + 14_999)).toEqual(window);
  });

  it('builds the series request', () => {
    const window = seriesWindow(rangeOf('15m'), T0);
    const request = { kind: 'device' as const, id: DEV, metric: 'cpu_ratio' as const, window };
    const url = new URL(seriesTarget(request), 'https://hub.example');
    expect(url.pathname).toBe('/v1/monitoring/series');
    expect(Object.fromEntries(url.searchParams)).toEqual({
      entity_kind: 'device',
      entity_id: DEV,
      metric: 'cpu_ratio',
      from: iso(window.from),
      to: iso(window.to),
      step: '15',
    });
    expect(seriesKey(request)).toBe(`device ${DEV} cpu_ratio ${window.from} ${window.to} 15`);
  });
});

describe('points and chart data', () => {
  it('parses points and leaves malformed ones out', () => {
    expect(
      parsePoints({
        points: [
          [iso(T0), 1],
          [iso(T0 + 15_000), null],
          ['nope', 1],
          [5, 1],
          [iso(T0), 'x'],
          [iso(T0), Number.POSITIVE_INFINITY],
          'x',
        ],
      }),
    ).toEqual([
      [T0, 1],
      [T0 + 15_000, null],
    ]);
    expect(() => parsePoints({})).toThrow(HubError);
    expect(() => parsePoints(null)).toThrow(HubError);
  });

  it('turns counters into rates, averaged over gaps, and leaves resets empty', () => {
    expect(
      counterRates([
        [T0, null],
        [T0 + 10_000, 100],
        [T0 + 20_000, 1100],
        [T0 + 30_000, null],
        [T0 + 40_000, 2100],
        [T0 + 50_000, 500],
        [T0 + 60_000, 600],
      ]),
    ).toEqual([
      [T0, null],
      [T0 + 10_000, null],
      [T0 + 20_000, 100],
      [T0 + 30_000, null],
      [T0 + 40_000, 50],
      [T0 + 50_000, null],
      [T0 + 60_000, 10],
    ]);
  });

  it('aligns the metrics of a chart by bucket time', () => {
    const memory = CHARTS.find((chart) => chart.id === 'memory')!;
    const data = chartData(memory, {
      mem_used_bytes: [
        [T0, 1],
        [T0 + 15_000, 2],
      ],
      mem_limit_bytes: [[T0 + 15_000, 8]],
    });
    expect(data.x).toEqual([T0 / 1000, T0 / 1000 + 15]);
    expect(data.series).toEqual([
      { metric: 'mem_used_bytes', values: [1, 2] },
      { metric: 'mem_limit_bytes', values: [null, 8] },
    ]);
    const network = CHARTS.find((chart) => chart.id === 'network')!;
    const rates = chartData(network, {
      net_rx_bytes: [
        [T0, 0],
        [T0 + 15_000, 1500],
      ],
    });
    expect(rates.series).toEqual([
      { metric: 'net_rx_bytes', values: [null, 100] },
      { metric: 'net_tx_bytes', values: [null, null] },
    ]);
    expect(hasData(rates)).toBe(true);
    expect(hasData(chartData(network, {}))).toBe(false);
  });

  it('finds the newest value', () => {
    expect(lastValue([1, 2, null])).toBe(2);
    expect(lastValue([null])).toBeNull();
    expect(lastValue([])).toBeNull();
  });
});
