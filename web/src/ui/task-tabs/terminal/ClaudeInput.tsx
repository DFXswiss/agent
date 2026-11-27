// The one input of the task tab "Claude Code" (KONZEPT E-43, ADR-104): it looks like the input box of Claude Code's TUI
// and takes its place, so the page shows a single input.
//
// - Placement `box`: drawn over the rows of the TUI's box (`DrawnBox`) in the screen's coordinates – the rule above in
//   the rule's colour, the prompt marker ❯ in the marker's colour, the text in the terminal font at the cell size, the
//   rule below. It is anchored at the lower rule and grows upwards with the text, as the TUI's box does; the TUI's own
//   status line below it (mode and hints) stays the TUI's output. On a phone the text part follows the sideways scroll
//   of the screen (`scrollLeft`), so it stays in view.
// - Placement `docked`: no box on the screen (the start, an agent that ended) – the same box below the screen.
//
// Enter sends the text as a signed prompt (`useComposer`, the same path as the chat; nothing reaches the terminal as a
// keystroke, ADR-016); Shift+Enter or Alt+Enter start a new line, Enter while an IME composes belongs to the IME. Esc
// interrupts the main session's turn (`interrupt`) wherever the focus is. A failed send or control and being offline
// are lines above the box in the TUI's own style (`⎿`). The text area covers the box from rule to rule and at least
// 44 px (a touch target, 06-web.md section 12.1); it is transparent, the rows below show through.
import { type CSSProperties, type KeyboardEvent, useEffect, useId, useRef } from 'react';
import { paletteColor, PROMPT_MARK, THEME } from '../../../core/terminal/screen';
import { useI18n } from '../../i18n';
import type { TerminalLayout } from '../../terminal/TerminalView';
import { FONT_FAMILY } from '../../terminal/xterm';
import type { Composer } from '../chat/ChatComposer';

/** Metrics of the docked box before a screen was measured. */
const DOCKED = { cols: 160, cellWidth: 8.4, cellHeight: 17, fontSize: 14 };
/** Dim grey (palette 246) of a rule where none can be copied and of quiet lines; readable on the theme (WCAG AA). */
export const RULE_COLOR = paletteColor(246);
const MIN_TARGET = 44;

export interface ClaudeInputProps {
  readonly composer: Composer;
  readonly placement: 'box' | 'docked';
  readonly layout: TerminalLayout | null;
}

/** Rows the text takes at `perLine` characters a row (each line at least one row). */
export function textRows(text: string, perLine: number): number {
  return text.split('\n').reduce((rows, line) => rows + Math.max(1, Math.ceil(line.length / perLine)), 0);
}

export function ClaudeInput({ composer, placement, layout }: ClaudeInputProps) {
  const { t, error } = useI18n();
  const label = useId();
  const field = useRef<HTMLTextAreaElement>(null);
  const latest = useRef(composer);
  useEffect(() => {
    latest.current = composer;
  });
  // Esc interrupts the turn, as in Claude Code
  useEffect(() => {
    const onKey = (event: globalThis.KeyboardEvent) => {
      if (event.key === 'Escape' && latest.current.working) void latest.current.control('interrupt');
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);
  useEffect(() => field.current!.focus(), []);

  const metrics = layout ?? DOCKED;
  const box = placement === 'box' ? layout!.box! : null;
  const { cols, cellWidth, cellHeight, fontSize } = metrics;
  const column = box?.column ?? 0;
  const rule = box?.rule ?? '─'.repeat(cols);
  const ruleColor = box?.ruleColor ?? RULE_COLOR;
  const markColor = box?.markColor ?? THEME.foreground;
  const rows = Math.max(
    box === null ? 1 : box.bottom - box.top - 1,
    textRows(composer.text, Math.max(1, cols - column - 2)),
  );
  const font: CSSProperties = { fontFamily: FONT_FAMILY, fontSize, lineHeight: `${cellHeight}px` };
  const anchor: CSSProperties =
    box === null
      ? { width: '100%', overflow: 'hidden' }
      : {
          // as wide as the scrolled area (the screen and xterm's scroll bar), so the text can stay in view to its end
          position: 'absolute',
          left: 0,
          right: 0,
          top: (box.bottom + 1) * cellHeight,
          transform: 'translateY(-100%)',
        };
  const visible = box === null ? '100%' : Math.min(cols * cellWidth, layout!.width);
  const height = Math.max((rows + 2) * cellHeight, MIN_TARGET);
  const notices = [
    ...(composer.failure === null
      ? []
      : [
          {
            key: 'failure',
            color: THEME.brightRed,
            text: t(composer.failure.key, { reason: error(composer.failure.code).title }),
          },
        ]),
    ...(composer.online ? [] : [{ key: 'offline', color: RULE_COLOR, text: t('task.terminal.offline') }]),
  ];

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key !== 'Enter' || event.shiftKey || event.altKey || event.nativeEvent.isComposing) return;
    event.preventDefault();
    void composer.send();
  };

  return (
    <div
      className="z-10 whitespace-pre"
      style={{ ...anchor, ...font, background: THEME.background, color: THEME.foreground }}
      data-testid="terminal.input"
      data-placement={placement}
      data-rows={rows}
      data-box={box === null ? undefined : `${box.top}-${box.bottom}`}
    >
      {notices.map((notice) => (
        <div
          key={notice.key}
          role={notice.key === 'failure' ? 'alert' : undefined}
          style={{ color: notice.color }}
          data-testid={`terminal.input.${notice.key}`}
          data-code={notice.key === 'failure' ? composer.failure!.code : undefined}
        >
          {`  ⎿  ${notice.text}`}
        </div>
      ))}
      <div className="relative">
        <div aria-hidden="true" style={{ color: ruleColor }} data-testid="terminal.input.rule">
          {rule}
        </div>
        <div aria-hidden="true" style={{ height: rows * cellHeight }}>
          {' '.repeat(column)}
          <span style={{ color: markColor }} data-testid="terminal.input.marker">
            {PROMPT_MARK}
          </span>
        </div>
        <div aria-hidden="true" style={{ color: ruleColor }}>
          {rule}
        </div>
        <div className="absolute left-0 top-0 w-full" style={{ height }}>
          <div
            className="absolute top-0 h-full"
            style={{ left: box === null ? 0 : layout!.scrollLeft, width: visible }}
          >
            <label htmlFor={label} className="sr-only">
              {t('task.chat.prompt.label')}
            </label>
            <textarea
              id={label}
              ref={field}
              rows={rows}
              className="block h-full w-full resize-none overflow-hidden border-0 bg-transparent p-0 outline-none"
              style={{
                ...font,
                paddingTop: cellHeight,
                paddingLeft: (column + 2) * cellWidth,
                color: THEME.foreground,
                caretColor: THEME.foreground,
              }}
              spellCheck={false}
              autoCapitalize="off"
              autoComplete="off"
              value={composer.text}
              onChange={(event) => composer.setText(event.target.value)}
              onKeyDown={onKeyDown}
              data-testid="chat.composer.prompt"
            />
          </div>
        </div>
      </div>
    </div>
  );
}
