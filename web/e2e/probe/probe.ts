// Probe page (WEB-01, 06-web.md section 10.2): mounts the read-only terminal with the WebGL or canvas renderer and a
// uPlot chart exactly as the screens will, under the hub CSP. The spec checks that no `<style>` element appears and
// no CSP violation is reported. Results are written to data attributes, never to window globals.
import { CanvasAddon } from '@xterm/addon-canvas';
import { FitAddon } from '@xterm/addon-fit';
import { WebglAddon } from '@xterm/addon-webgl';
import { Terminal } from '@xterm/xterm';
import '@xterm/xterm/css/xterm.css';
import uPlot from 'uplot';
import 'uplot/dist/uPlot.min.css';

const body = document.body;
const renderer = new URLSearchParams(location.search).get('renderer') ?? 'webgl';

try {
  const terminal = new Terminal({ disableStdin: true, cursorBlink: false, cols: 160, rows: 48 });
  terminal.loadAddon(renderer === 'canvas' ? new CanvasAddon() : new WebglAddon());
  terminal.loadAddon(new FitAddon());
  terminal.open(document.getElementById('terminal')!);
  terminal.write('\x1b[1;32mread-only\x1b[0m terminal probe\r\n');
  await new Promise<void>((resolve) => terminal.write('', resolve));
  body.dataset.renderer = renderer;
  body.dataset.canvases = String(document.querySelectorAll('#terminal canvas').length);
} catch (error) {
  body.dataset.renderer = `error: ${String(error)}`;
}

new uPlot(
  { width: 320, height: 160, series: [{}, { label: 'cpu', stroke: 'blue' }] },
  [
    [1, 2, 3],
    [10, 20, 15],
  ],
  document.getElementById('chart')!,
);
body.dataset.ready = '1';
