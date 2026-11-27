// What the task tab "Claude Code" reads from a terminal screen (KONZEPT E-43, ADR-104): the input box of Claude Code's
// TUI, the colours of the screen's cells and the font size at which the recorded geometry fills the page.
//
// - **Input box:** the rule of the guest (`input_box` in `src/agent_guest/delivery.py`, Claude Code 2.1.x): the last row
//   that starts (after spaces) with the prompt marker ❯ below a row holding a horizontal rule (8 × ─); the box reaches
//   from the nearest rule above the marker to the next rule below it. The web input is laid over exactly these rows,
//   so the page shows one input – the TUI's own box never shows through. Without both rules there is no box.
// - **Colours:** the theme of the terminal (`THEME`, also the page's background) and the xterm 256-colour palette
//   (6 × 6 × 6 cube, 24 greys), so a cell copied from the screen (the rules, the marker) has the colour the canvas drew.
// - **Font size:** the recorded geometry is never reflowed (ADR-016); the font is scaled so that all columns and rows fit
//   the space (`fitFontSize`), or – on a phone – so that the rows fill its height (the columns then scroll sideways).

export const PROMPT_MARK = '❯';
export const RULE = '─'.repeat(8);

export interface InputBox {
  /** Row of the rule above the input (0: the first visible row). */
  readonly top: number;
  /** Row of the prompt marker. */
  readonly marker: number;
  /** Row of the rule below the input. */
  readonly bottom: number;
  /** Column of the prompt marker. */
  readonly column: number;
}

/** The input box of Claude Code's TUI among the visible `rows` (text per row), null when none is visible. */
export function findInputBox(rows: readonly string[]): InputBox | null {
  for (let marker = rows.length - 1; marker >= 0; marker -= 1) {
    const row = rows[marker]!;
    const text = row.trimStart();
    if (!text.startsWith(PROMPT_MARK)) continue;
    let top = marker - 1;
    while (top >= 0 && !rows[top]!.includes(RULE)) top -= 1;
    let bottom = marker + 1;
    while (bottom < rows.length && !rows[bottom]!.includes(RULE)) bottom += 1;
    if (top < 0 || bottom === rows.length) return null;
    return { top, marker, bottom, column: row.length - text.length };
  }
  return null;
}

/** The colours of the terminal: background (also the page's), foreground and the 16 ANSI colours. */
export const THEME = {
  background: '#1e1e1e',
  foreground: '#cccccc',
  black: '#000000',
  red: '#cd3131',
  green: '#0dbc79',
  yellow: '#e5e510',
  blue: '#2472c8',
  magenta: '#bc3fbc',
  cyan: '#11a8cd',
  white: '#e5e5e5',
  brightBlack: '#666666',
  brightRed: '#f14c4c',
  brightGreen: '#23d18b',
  brightYellow: '#f5f543',
  brightBlue: '#3b8eea',
  brightMagenta: '#d670d6',
  brightCyan: '#29b8db',
  brightWhite: '#e5e5e5',
} as const;

const ANSI = [
  THEME.black,
  THEME.red,
  THEME.green,
  THEME.yellow,
  THEME.blue,
  THEME.magenta,
  THEME.cyan,
  THEME.white,
  THEME.brightBlack,
  THEME.brightRed,
  THEME.brightGreen,
  THEME.brightYellow,
  THEME.brightBlue,
  THEME.brightMagenta,
  THEME.brightCyan,
  THEME.brightWhite,
];

const CUBE = [0, 0x5f, 0x87, 0xaf, 0xd7, 0xff];

const hex = (r: number, g: number, b: number): string =>
  `#${[r, g, b].map((part) => part.toString(16).padStart(2, '0')).join('')}`;

/** The colour of palette entry `index` (0–255) as `#rrggbb`. */
export function paletteColor(index: number): string {
  if (index < 16) return ANSI[index]!;
  if (index < 232) {
    const cube = index - 16;
    return hex(CUBE[Math.floor(cube / 36)]!, CUBE[Math.floor(cube / 6) % 6]!, CUBE[cube % 6]!);
  }
  const grey = 8 + (index - 232) * 10;
  return hex(grey, grey, grey);
}

/** How a cell names its foreground colour (xterm's `isFgDefault` / `isFgPalette` / `isFgRGB`). */
export type ColorMode = 'default' | 'palette' | 'rgb';

/** The foreground colour of a cell as `#rrggbb`: the theme's foreground, a palette entry or a 24-bit colour. */
export function cellColor(mode: ColorMode, value: number): string {
  if (mode === 'palette') return paletteColor(value);
  if (mode === 'rgb') return hex((value >> 16) & 0xff, (value >> 8) & 0xff, value & 0xff);
  return THEME.foreground;
}

export interface FitInput {
  readonly cols: number;
  readonly rows: number;
  /** Cell size (px) at `fontSize`. */
  readonly cellWidth: number;
  readonly cellHeight: number;
  readonly fontSize: number;
  /** The space for the screen (px). */
  readonly width: number;
  readonly height: number;
  readonly minFont: number;
  readonly maxFont: number;
  /** Fit the rows to the height only (a phone: the columns scroll sideways). */
  readonly heightOnly: boolean;
}

/** The font size (half pixels, within the bounds) at which the screen fits the space; `fontSize` when unmeasurable. */
export function fitFontSize(input: FitInput): number {
  const { cols, rows, cellWidth, cellHeight, fontSize, width, height } = input;
  const byHeight = height / (rows * cellHeight);
  const scale = input.heightOnly ? byHeight : Math.min(width / (cols * cellWidth), byHeight);
  if (!Number.isFinite(scale) || scale <= 0) return fontSize;
  const size = Math.floor(fontSize * scale * 2) / 2;
  return Math.min(input.maxFont, Math.max(input.minFont, size));
}
