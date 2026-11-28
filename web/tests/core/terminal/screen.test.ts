// The screen reading of the task tab "Claude Code" (KONZEPT E-43, ADR-104): the input box of Claude Code 2.1.x (the
// guest's rule), the colours of cells and the font size that fits the recorded geometry.
import { describe, expect, it } from 'vitest';
import {
  cellColor,
  findInputBox,
  fitFontSize,
  paletteColor,
  PROMPT_MARK,
  RULE,
  THEME,
} from '../../../src/core/terminal/screen';

const rule = '─'.repeat(160);

/** The bottom of a Claude Code 2.1.330 screen (as captured from the real TUI in a 160 × 48 pane). */
const SCREEN = [
  ' ▐▛███▛█   Claude Code v2.1.330',
  '',
  '❯ Sessions that expire during a request are still accepted. Fix the expiry check in session.py.',
  '',
  '● Fixed.',
  '',
  `${' '.repeat(142)}● high · /effort`,
  rule,
  '❯ ',
  rule,
  "  ⏵⏵ don't ask on (shift+tab to cycle) · ← for agents",
  '',
];

describe('input box', () => {
  it('finds the box of the idle TUI below the conversation, not the echoed prompt', () => {
    expect(findInputBox(SCREEN)).toEqual({ top: 7, marker: 8, bottom: 9, column: 0 });
    expect(PROMPT_MARK).toBe('❯');
    expect(RULE).toBe('────────');
  });

  it('spans an input of several lines and an indented marker', () => {
    const rows = ['x', rule, '  ❯ first line', '    second line', rule, 'status'];
    expect(findInputBox(rows)).toEqual({ top: 1, marker: 2, bottom: 4, column: 2 });
  });

  it('is no box without a rule above or below the marker, and none without a marker', () => {
    expect(findInputBox(['❯ text', rule])).toBeNull();
    expect(findInputBox([rule, '❯ text', 'status'])).toBeNull();
    expect(findInputBox([rule, 'no marker', rule])).toBeNull();
    expect(findInputBox([])).toBeNull();
  });
});

describe('colours', () => {
  it('maps the 16 theme colours, the cube and the greys of the 256-colour palette', () => {
    expect(paletteColor(0)).toBe(THEME.black);
    expect(paletteColor(8)).toBe(THEME.brightBlack);
    expect(paletteColor(15)).toBe(THEME.brightWhite);
    expect(paletteColor(16)).toBe('#000000');
    expect(paletteColor(174)).toBe('#d78787');
    expect(paletteColor(231)).toBe('#ffffff');
    expect(paletteColor(232)).toBe('#080808');
    expect(paletteColor(246)).toBe('#949494');
    expect(paletteColor(255)).toBe('#eeeeee');
  });

  it('takes the foreground, a palette entry or a 24-bit colour of a cell', () => {
    expect(cellColor('default', 0)).toBe(THEME.foreground);
    expect(cellColor('palette', 1)).toBe(THEME.red);
    expect(cellColor('rgb', 0xd77757)).toBe('#d77757');
  });
});

describe('font size', () => {
  const base = {
    cols: 160,
    rows: 48,
    cellWidth: 8,
    cellHeight: 16,
    fontSize: 13,
    minFont: 7,
    maxFont: 15,
    heightOnly: false,
  };

  it('fits columns and rows into the space, the narrower dimension deciding', () => {
    // width allows 1.0, height allows 1.0: unchanged
    expect(fitFontSize({ ...base, width: 1280, height: 768 })).toBe(13);
    // the height is short: the rows decide
    expect(fitFontSize({ ...base, width: 1280, height: 600 })).toBe(10);
    // a wide and tall space: bounded above
    expect(fitFontSize({ ...base, width: 4000, height: 4000 })).toBe(15);
    // a tiny space: bounded below
    expect(fitFontSize({ ...base, width: 100, height: 100 })).toBe(7);
  });

  it('fits only the height on a phone', () => {
    expect(fitFontSize({ ...base, width: 412, height: 870, heightOnly: true })).toBe(14.5);
  });

  it('keeps the font when the space or the cells cannot be measured', () => {
    expect(fitFontSize({ ...base, cellWidth: 0, cellHeight: 0, width: 1280, height: 768 })).toBe(13);
    expect(fitFontSize({ ...base, width: 0, height: 0 })).toBe(13);
    expect(fitFontSize({ ...base, width: Number.NaN, height: 768 })).toBe(13);
  });
});
