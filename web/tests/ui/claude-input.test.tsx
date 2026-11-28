// The one input of the task tab "Claude Code" (KONZEPT E-43, ADR-104) against a scripted composer: it lies exactly over
// the TUI's input box (rule, marker, rule in the colours copied from the screen, the cell size of the screen), docks
// below the screen without a box, grows upwards with the text, sends with Enter, starts lines with Shift+Enter or
// Alt+Enter, leaves Enter to an IME, interrupts with Esc anywhere and shows failures and offline as TUI lines.
import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { createI18n } from '../../src/core/i18n';
import { PROMPT_MARK, THEME } from '../../src/core/terminal/screen';
import { I18nProvider } from '../../src/ui/i18n';
import type { Composer } from '../../src/ui/task-tabs/chat/ChatComposer';
import { ClaudeInput, RULE_COLOR, textRows } from '../../src/ui/task-tabs/terminal/ClaudeInput';
import type { TerminalLayout } from '../../src/ui/terminal/TerminalView';

function composer(overrides: Partial<Composer> = {}): Composer {
  return {
    text: '',
    setText: vi.fn(),
    online: true,
    disabled: false,
    failure: null,
    working: false,
    alive: true,
    send: vi.fn(async () => undefined),
    control: vi.fn(async () => undefined),
    ...overrides,
  };
}

const RULE = '─'.repeat(160);

const LAYOUT: TerminalLayout = {
  cols: 160,
  rows: 48,
  cellWidth: 8,
  cellHeight: 16,
  fontSize: 13,
  width: 1280,
  scrollLeft: 0,
  box: { top: 44, marker: 45, bottom: 46, column: 0, rule: RULE, ruleColor: '#808080', markColor: '#aabbcc' },
};

function show(props: Parameters<typeof ClaudeInput>[0]) {
  return render(
    <I18nProvider value={createI18n('en')}>
      <ClaudeInput {...props} />
    </I18nProvider>,
  );
}

describe('the Claude Code input', () => {
  it("lies over the TUI box: anchored at its lower rule, the rules and the marker in the screen's colours", () => {
    show({ composer: composer(), placement: 'box', layout: LAYOUT });
    const input = screen.getByTestId('terminal.input');
    expect(input).toHaveAttribute('data-placement', 'box');
    expect(input).toHaveAttribute('data-rows', '1');
    // its lower edge is the lower edge of the TUI's lower rule (row 46): it grows upwards like the TUI's box
    expect(input).toHaveStyle({ position: 'absolute', top: '752px', transform: 'translateY(-100%)', left: '0px' });
    expect(input).toHaveStyle({ right: '0px' });
    expect(input).toHaveStyle({ fontSize: '13px', lineHeight: '16px', background: THEME.background });
    expect(screen.getByTestId('terminal.input.rule')).toHaveTextContent(RULE);
    expect(screen.getByTestId('terminal.input.rule')).toHaveStyle({ color: '#808080' });
    expect(screen.getByTestId('terminal.input.marker')).toHaveTextContent(PROMPT_MARK);
    expect(screen.getByTestId('terminal.input.marker')).toHaveStyle({ color: '#aabbcc' });
    // the text starts two cells after the marker, on the marker's row; the field is a touch target
    const field = screen.getByTestId('chat.composer.prompt');
    expect(field).toHaveStyle({ paddingTop: '16px', paddingLeft: '16px' });
    expect(field.parentElement!.parentElement).toHaveStyle({ height: '48px' });
    expect(field.parentElement).toHaveStyle({ width: '1280px' });
    expect(document.activeElement).toBe(field);
    expect(document.querySelectorAll('textarea')).toHaveLength(1);
  });

  it('keeps the text in view on a phone and grows with the text, also for long lines', () => {
    const phone = { ...LAYOUT, width: 412, scrollLeft: 300, box: { ...LAYOUT.box!, column: 2, top: 43 } };
    show({ composer: composer({ text: 'one\ntwo\nthree' }), placement: 'box', layout: phone });
    expect(screen.getByTestId('terminal.input')).toHaveAttribute('data-rows', '3');
    const field = screen.getByTestId('chat.composer.prompt');
    // the text follows the sideways scroll of the screen
    expect(field.parentElement).toHaveStyle({ width: '412px', left: '300px' });
    expect(field).toHaveStyle({ paddingLeft: '32px' });
    // a box of two input rows (the TUI wrapped) keeps two rows for a short text
    expect(textRows('x'.repeat(157), 156)).toBe(2);
    expect(textRows('', 10)).toBe(1);
  });

  it("docks below the screen without a box, with a plain rule and the theme's marker", () => {
    show({ composer: composer(), placement: 'docked', layout: null });
    const input = screen.getByTestId('terminal.input');
    expect(input).toHaveAttribute('data-placement', 'docked');
    expect(input).toHaveStyle({ width: '100%', fontSize: '14px', lineHeight: '17px' });
    expect(screen.getByTestId('terminal.input.rule')).toHaveTextContent(RULE);
    expect(screen.getByTestId('terminal.input.rule')).toHaveStyle({ color: RULE_COLOR });
    expect(screen.getByTestId('terminal.input.marker')).toHaveStyle({ color: THEME.foreground });
    expect(screen.getByTestId('chat.composer.prompt').parentElement).toHaveStyle({ width: '100%' });
  });

  it('docks with the measured cells of a screen that shows no box', () => {
    show({ composer: composer(), placement: 'docked', layout: { ...LAYOUT, box: null } });
    expect(screen.getByTestId('terminal.input')).toHaveStyle({ fontSize: '13px', lineHeight: '16px' });
  });

  it('sends with Enter, starts lines with Shift+Enter or Alt+Enter and leaves Enter to an IME', () => {
    const state = composer();
    show({ composer: state, placement: 'box', layout: LAYOUT });
    const field = screen.getByTestId('chat.composer.prompt');
    fireEvent.change(field, { target: { value: 'Fix it' } });
    expect(state.setText).toHaveBeenCalledWith('Fix it');
    fireEvent.keyDown(field, { key: 'Enter', shiftKey: true });
    fireEvent.keyDown(field, { key: 'Enter', altKey: true });
    fireEvent.keyDown(field, { key: 'Enter', isComposing: true });
    fireEvent.keyDown(field, { key: 'a' });
    expect(state.send).not.toHaveBeenCalled();
    fireEvent.keyDown(field, { key: 'Enter' });
    expect(state.send).toHaveBeenCalledTimes(1);
  });

  it('interrupts a working session with Esc wherever the focus is, and nothing else', () => {
    const idle = composer();
    const { unmount } = show({ composer: idle, placement: 'box', layout: LAYOUT });
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(idle.control).not.toHaveBeenCalled();
    unmount();
    const working = composer({ working: true });
    show({ composer: working, placement: 'box', layout: LAYOUT });
    fireEvent.keyDown(window, { key: 'x' });
    fireEvent.keyDown(document.body, { key: 'Escape' });
    expect(working.control).toHaveBeenCalledWith('interrupt');
  });

  it('shows a failed send and being offline as lines of the TUI above the box', () => {
    show({
      composer: composer({ online: false, failure: { key: 'task.chat.send.failed', code: 'rate_limited' } }),
      placement: 'box',
      layout: LAYOUT,
    });
    const failure = screen.getByTestId('terminal.input.failure');
    expect(failure).toHaveAttribute('role', 'alert');
    expect(failure).toHaveAttribute('data-code', 'rate_limited');
    expect(failure).toHaveTextContent(`⎿ Not sent: ${createI18n('en').error('rate_limited').title}`);
    expect(failure).toHaveStyle({ color: THEME.brightRed });
    const offline = screen.getByTestId('terminal.input.offline');
    expect(offline).not.toHaveAttribute('role');
    expect(offline).not.toHaveAttribute('data-code');
    expect(offline).toHaveTextContent('Offline – the input is kept');
  });
});
