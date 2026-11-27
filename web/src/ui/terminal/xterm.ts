// One read-only xterm.js screen (06-web.md section 5.5, ADR-016, ADR-019): input disabled (`disableStdin`), no
// `onData`/`onKey`/`attachCustomKeyEventHandler` handler is ever registered, the cursor is hidden (transparent cursor,
// no inactive outline, DECTCEM off). The renderer addon is loaded *before* `open()` – WebGL, or the canvas renderer
// where WebGL2 is unavailable – because xterm's default DOM renderer injects `<style>` elements the CSP forbids
// (decision WEB-01 19). When the WebGL context is lost for good (the addon waited for a restore in vain), the canvas
// renderer takes over on the same terminal, keeping its buffer; the WebGL addon is not disposed, because its disposal
// installs the DOM renderer (KOR-05). The geometry is the asciicast header's; `fitFont` scales the font with
// `addon-fit` so that the fixed number of columns fits the container – never resizing the terminal (no reflow); below
// the minimum font size the container scrolls horizontally. The scrollback is 10 000 lines.
//
// The screens look like a local terminal (KONZEPT E-43): the theme of `core/terminal/screen.ts`, a terminal font
// (the usual monospace fonts of macOS, Windows and Linux) at line height 1. For the task tab "Claude Code" the screen is
// read back: `visibleRows` (text of the visible rows), `cellColorAt` (the colour the canvas drew) and `cellSize`; and
// `fitToSpace` fits columns and rows into the page (on a phone the rows into its height).
import { CanvasAddon } from '@xterm/addon-canvas';
import { FitAddon } from '@xterm/addon-fit';
import { WebglAddon } from '@xterm/addon-webgl';
import { Terminal } from '@xterm/xterm';
import '@xterm/xterm/css/xterm.css';
import { cellColor, fitFontSize, THEME } from '../../core/terminal/screen';

export const FONT_SIZE = 13;
export const MIN_FONT = 7;
export const MAX_FONT = 15;
/** The smallest font on a phone, where the rows fill the height and the columns scroll sideways. */
export const PHONE_MIN_FONT = 9;
/** The largest font when the screen fills the page (a large display shows the 160 columns over its whole width). */
export const FILL_MAX_FONT = 22;
/** The terminal fonts of macOS, Windows and Linux, as a local terminal uses them. */
export const FONT_FAMILY =
  '"SF Mono", Menlo, Monaco, "Cascadia Mono", Consolas, "DejaVu Sans Mono", "Liberation Mono", monospace';
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
    theme: { ...THEME, cursor: 'transparent', cursorAccent: 'transparent' },
    fontFamily: FONT_FAMILY,
    lineHeight: 1,
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

/** The text of the visible rows of a screen (its viewport). */
export function visibleRows(screen: Screen): string[] {
  const { terminal } = screen;
  const buffer = terminal.buffer.active;
  return Array.from(
    { length: terminal.rows },
    (_, row) => buffer.getLine(buffer.viewportY + row)?.translateToString(true) ?? '',
  );
}

/** The foreground colour the screen drew at the visible `row` × `column`. */
export function cellColorAt(screen: Screen, row: number, column: number): string {
  const buffer = screen.terminal.buffer.active;
  const cell = buffer.getLine(buffer.viewportY + row)?.getCell(column);
  if (cell === undefined) return THEME.foreground;
  return cellColor(cell.isFgRGB() ? 'rgb' : cell.isFgPalette() ? 'palette' : 'default', cell.getFgColor());
}

/** The size of one cell (px) as rendered, null before the screen is open. */
export function cellSize(screen: Screen): { readonly width: number; readonly height: number } | null {
  const { terminal } = screen;
  const rendered = terminal.element?.querySelector<HTMLElement>('.xterm-screen');
  return rendered
    ? { width: rendered.clientWidth / terminal.cols, height: rendered.clientHeight / terminal.rows }
    : null;
}

/**
 * The font size at which all columns and rows fit `width` × `height`; with `phone` the rows fill the height and the
 * columns scroll sideways. The current size when the screen is not rendered yet.
 */
export function fitToSpace(screen: Screen, width: number, height: number, phone: boolean): number {
  const { terminal } = screen;
  const fontSize = terminal.options.fontSize as number;
  const cell = cellSize(screen);
  if (cell === null) return fontSize;
  return fitFontSize({
    cols: terminal.cols,
    rows: terminal.rows,
    cellWidth: cell.width,
    cellHeight: cell.height,
    fontSize,
    width,
    height,
    minFont: phone ? PHONE_MIN_FONT : MIN_FONT,
    maxFont: FILL_MAX_FONT,
    heightOnly: phone,
  });
}
