// U-WEB-ui-charts: the chart frame (uPlot on wide screens – replaced here, drawn in the browser specs –, sparklines on
// phones), the media query hook and the sparkline path.
import { act, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createI18n } from '../../src/core/i18n';
import { Chart } from '../../src/ui/charts/Chart';
import { Sparkline, sparkPath } from '../../src/ui/charts/Sparkline';
import { colorOf, SERIES_COLORS } from '../../src/ui/charts/types';
import { I18nProvider } from '../../src/ui/i18n';

vi.mock('../../src/ui/charts/UPlotChart', () => ({
  default: (props: { title: string }) => <p data-testid="uplot">{props.title}</p>,
}));

afterEach(() => {
  delete (window as { matchMedia?: unknown }).matchMedia;
});

const props = {
  title: 'CPU',
  data: { x: [1, 2, 3], series: [{ metric: 'cpu_ratio' as const, values: [1, null, 2] }] },
  labels: ['CPU'],
  format: (value: number) => `${value} c`,
  formatTime: (seconds: number) => `t${seconds}`,
  formatTick: (seconds: number) => `k${seconds}`,
};

const wrap = (node: React.ReactNode) => <I18nProvider value={createI18n('en')}>{node}</I18nProvider>;

describe('chart frame', () => {
  it('loads the uPlot chart on wide screens and without matchMedia', async () => {
    render(wrap(<Chart id="cpu" {...props} />));
    expect(await screen.findByTestId('uplot')).toHaveTextContent('CPU');
  });

  it('follows the media query', async () => {
    let listener: () => void = () => undefined;
    const list = {
      matches: true,
      addEventListener: vi.fn((_type: string, change: () => void) => (listener = change)),
      removeEventListener: vi.fn(),
    };
    window.matchMedia = vi.fn(() => list) as never;
    const view = render(wrap(<Chart id="cpu" {...props} />));
    expect(await screen.findByTestId('uplot')).toBeInTheDocument();
    list.matches = false;
    act(() => listener());
    expect(screen.getByTestId('monitoring.chart.sparkline')).toHaveTextContent('CPU: 2 c');
    view.unmount();
    expect(list.removeEventListener).toHaveBeenCalledWith('change', listener);
  });
});

describe('sparkline', () => {
  it('draws segments, breaks at gaps and scales to the maximum', () => {
    expect(sparkPath([1, null, 2])).toBe('M0.0 16.0 M120.0 0.0');
    expect(sparkPath([0, 0])).toBe('M0.0 32.0 L120.0 32.0');
    expect(sparkPath([5])).toBe('M0.0 0.0');
    expect(sparkPath([])).toBe('');
  });

  it('labels the newest value or none', () => {
    render(
      wrap(
        <Sparkline
          {...props}
          data={{ x: [1], series: [{ metric: 'cpu_ratio', values: [null] }] }}
          emptyLabel="no data"
        />,
      ),
    );
    expect(screen.getByTestId('monitoring.chart.sparkline')).toHaveTextContent('CPU: no data');
  });

  it('cycles the series colors', () => {
    expect(colorOf(0)).toBe(SERIES_COLORS[0]);
    expect(colorOf(SERIES_COLORS.length + 1)).toBe(SERIES_COLORS[1]);
  });
});
