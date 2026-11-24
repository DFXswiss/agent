// One read-only xterm.js screen (06-web.md section 5.5, ADR-016, ADR-019): input disabled (`disableStdin`), no
// `onData`/`onKey`/`attachCustomKeyEventHandler` handler is ever registered, the cursor is hidden (transparent cursor,
// no inactive outline, DECTCEM off). The renderer addon is loaded *before* `open()` – WebGL, or the canvas renderer
// where WebGL2 is unavailable – because xterm's default DOM renderer injects `<style>` elements the CSP forbids
// (decision WEB-01 19). When the WebGL context is lost for good (the addon waited for a restore in vain), the canvas
// renderer takes over on the same terminal, keeping its buffer; the WebGL addon is not disposed, because its disposal
// installs the DOM renderer (KOR-05). The geometry is the asciicast header's; `fitFont` scales the font with
// `addon-fit` so that the fixed number of columns fits the container – never resizing the terminal (no reflow); below
// the minimum font size the container scrolls horizontally. The scrollback is 10 000 lines.
import { CanvasAddon } from '@xterm/addon-canvas';
import { FitAddon } from '@xterm/addon-fit';
import { WebglAddon } from '@xterm/addon-webgl';
import { Terminal } from '@xterm/xterm';
import '@xterm/xterm/css/xterm.css';

export const FONT_SIZE = 13;
export const MIN_FONT = 7;
export const MAX_FONT = 15;
/** Lines a screen keeps above its rows (ADR-086 section 1; older output is replayed from memory or the hub). */
export const SCROLLBACK = 10_000;
const HIDE_CURSOR = '\x1b[?25l';

export interface Screen {
  readonly terminal: Terminal;
  readonly fit: FitAddon;
}

/** Whether the browser offers WebGL2 (the WebGL renderer needs it). */
export function hasWebgl2(): boolean {
  return document.createElement('canvas').getContext('webgl2') !== null;
}

/** The WebGL renderer of `terminal`, handing over to the canvas renderer when its context is lost for good. */
function webglRenderer(terminal: Terminal): WebglAddon {
  const addon = new WebglAddon();
  addon.onContextLoss(() => terminal.loadAddon(new CanvasAddon()));
  return addon;
}

/** A read-only terminal of `cols` × `rows` opened in `host`. */
export function openScreen(host: HTMLElement, cols: number, rows: number, webgl: boolean = hasWebgl2()): Screen {
  const terminal = new Terminal({
    cols,
    rows,
    disableStdin: true,
    cursorBlink: false,
    cursorInactiveStyle: 'none',
    theme: { cursor: 'transparent', cursorAccent: 'transparent' },
    fontSize: FONT_SIZE,
    scrollback: SCROLLBACK,
  });
  terminal.loadAddon(webgl ? webglRenderer(terminal) : new CanvasAddon());
  const fit = new FitAddon();
  terminal.loadAddon(fit);
  terminal.open(host);
  terminal.write(HIDE_CURSOR);
  return { terminal, fit };
}

/** Clears a screen for a replay (output, scrollback, modes), the cursor stays hidden. */
export function clearScreen(screen: Screen): void {
  screen.terminal.reset();
  screen.terminal.write(HIDE_CURSOR);
}

/** The font size at which the terminal's columns fit the host's width (within `MIN_FONT`…`MAX_FONT`). */
export function fitFont(screen: Screen): number {
  const { terminal, fit } = screen;
  const current = terminal.options.fontSize ?? FONT_SIZE;
  const proposed = fit.proposeDimensions();
  if (proposed === undefined || !Number.isFinite(proposed.cols) || proposed.cols <= 0) return current;
  const size = Math.floor(((current * proposed.cols) / terminal.cols) * 2) / 2;
  return Math.min(MAX_FONT, Math.max(MIN_FONT, size));
}
