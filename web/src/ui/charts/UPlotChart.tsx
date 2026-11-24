// A uPlot time-series chart (06-web.md sections 2.1 and 9.1), loaded lazily with its own chunk. uPlot draws on a
// canvas and sets sizes through the CSSOM (allowed by the CSP of 03-api.md section 1); its stylesheet is a static file
// of this chunk (section 10.2, no runtime `<style>`). The legend is uPlot's (live values under the cursor); the tooltip
// (an element of uPlot's cursor layer, so its position is the cursor's) follows the cursor with the bucket time and the
// values; axis ticks use the active locale (24-hour times). The chart follows the width of its container and is built
// again only when its data or labels change (the formatters are read from the newest props).
import { useEffect, useLayoutEffect, useRef } from 'react';
import uPlot from 'uplot';
import 'uplot/dist/uPlot.min.css';
import { colorOf, type ChartProps } from './types';

export const HEIGHT = 200;

function tooltipPlugin(current: { readonly current: ChartProps }, tooltip: HTMLDivElement): uPlot.Plugin {
  return {
    hooks: {
      setCursor: (plot) => {
        const props = current.current;
        const index = plot.cursor.idx;
        if (typeof index !== 'number') {
          tooltip.hidden = true;
          return;
        }
        const lines = [props.formatTime(plot.data[0][index]!)];
        props.data.series.forEach((series, position) => {
          const value = series.values[index];
          lines.push(`${props.labels[position]}: ${typeof value === 'number' ? props.format(value) : '–'}`);
        });
        tooltip.textContent = lines.join('\n');
        tooltip.hidden = false;
        tooltip.style.left = `${Math.round(plot.cursor.left as number) + 8}px`;
        tooltip.style.top = `${Math.round(plot.cursor.top as number) + 8}px`;
      },
    },
  };
}

export default function UPlotChart(props: ChartProps) {
  const host = useRef<HTMLDivElement>(null);
  const latest = useRef(props);
  useLayoutEffect(() => {
    latest.current = props;
  });
  const { data } = props;
  const labels = props.labels.join('\n');
  useEffect(() => {
    const element = host.current!;
    const names = labels.split('\n');
    const tooltip = document.createElement('div');
    tooltip.hidden = true;
    tooltip.setAttribute('role', 'tooltip');
    tooltip.className =
      'pointer-events-none absolute z-10 whitespace-pre rounded bg-gray-900 px-2 py-1 text-xs text-white';
    tooltip.dataset.testid = 'monitoring.chart.tooltip';
    const options: uPlot.Options = {
      width: Math.max(element.clientWidth, 200),
      height: HEIGHT,
      series: [
        { value: (_plot, seconds) => latest.current.formatTime(seconds) },
        ...names.map((label, index) => ({
          label,
          stroke: colorOf(index),
          width: 2,
          spanGaps: false,
          value: (_plot: uPlot, value: number | null) => (value === null ? '–' : latest.current.format(value)),
        })),
      ],
      axes: [
        {
          values: (plot, ticks) => {
            const span = plot.scales.x!.max! - plot.scales.x!.min!;
            return ticks.map((tick) => latest.current.formatTick(tick, span));
          },
        },
        { size: 80, values: (_plot, values) => values.map((value) => latest.current.format(value)) },
      ],
      // from zero, with headroom; at least one unit high (an all-zero series still gets an axis)
      scales: { y: { range: (_plot, _min, max) => [0, Math.max(max * 1.1, 1)] } },
      plugins: [tooltipPlugin(latest, tooltip)],
    };
    const aligned = [data.x, ...data.series.map((series) => series.values)] as uPlot.AlignedData;
    const plot = new uPlot(options, aligned, element);
    plot.over.append(tooltip);
    const observer = new ResizeObserver(() =>
      plot.setSize({ width: Math.max(element.clientWidth, 200), height: HEIGHT }),
    );
    observer.observe(element);
    return () => {
      observer.disconnect();
      plot.destroy();
    };
  }, [data, labels]);
  return (
    <div data-testid="monitoring.chart.canvas">
      <div ref={host} aria-hidden="true" />
    </div>
  );
}
