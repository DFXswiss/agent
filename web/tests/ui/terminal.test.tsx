// U-WEB-ui-terminal: the session detail (loading, error, tabs, children, session chat) and the read-only terminal view
// in jsdom with xterm.js replaced by a recording double (jsdom has no canvas): renderer choice, no input handler,
// hidden cursor, font fitting, geometry from the chunks, Follow writes in order, History replay into the hidden screen
// and playback, notices (loading, error, empty, unverifiable) and the cancelled paste. The real xterm runs in
// `e2e/devices/terminal.spec.ts`.
import { act, fireEvent, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { HubError } from '../../src/core/net/signedFetch';
import { putSession } from '../../src/core/store/slices/tasks';
import { putEvents } from '../../src/core/store/slices/events';
import { applyPage, terminalFailed } from '../../src/core/store/slices/terminal';
import { SessionScreen } from '../../src/ui/screens/session/SessionScreen';
import TerminalView, { offsetLabel } from '../../src/ui/terminal/TerminalView';
import { clearScreen, fitFont, hasWebgl2, MAX_FONT, MIN_FONT, openScreen } from '../../src/ui/terminal/xterm';
import { sessionRow, taskRow, type TaskWorld, taskWorld } from '../core/tasks/world';
import { MAIN, mount, TASK, verified } from './chat-world';

const xterm = vi.hoisted(() => {
  const terminals: FakeTerminal[] = [];
  const fit = { proposed: { cols: 80, rows: 48 } as { cols: number; rows: number } | undefined };
  class FakeTerminal {
    readonly options: Record<string, unknown>;
    readonly writes: string[] = [];
    readonly addons: string[] = [];
    host: HTMLElement | null = null;
    cols: number;
    rows: number;
    disposed = false;
    resets = 0;
    constructor(options: Record<string, unknown>) {
      this.options = { ...options };
      this.cols = options.cols as number;
      this.rows = options.rows as number;
      terminals.push(this);
    }
    loadAddon(addon: { name: string }) {
      this.addons.push(addon.name);
    }
    open(host: HTMLElement) {
      this.host = host;
    }
    write(data: string, done?: () => void) {
      this.writes.push(data);
      if (done) setTimeout(done, 0);
    }
    reset() {
      this.resets += 1;
      this.writes.length = 0;
    }
    resize(cols: number, rows: number) {
      this.cols = cols;
      this.rows = rows;
    }
    dispose() {
      this.disposed = true;
    }
  }
  return { terminals, fit, FakeTerminal };
});

vi.mock('@xterm/xterm', () => ({ Terminal: xterm.FakeTerminal }));
const lost = vi.hoisted(() => ({ listeners: [] as (() => void)[] }));
vi.mock('@xterm/addon-webgl', () => ({
  WebglAddon: class {
    name = 'webgl';
    onContextLoss(listener: () => void) {
      lost.listeners.push(listener);
      return { dispose: () => undefined };
    }
  },
}));
vi.mock('@xterm/addon-canvas', () => ({
  CanvasAddon: class {
    name = 'canvas';
  },
}));
vi.mock('@xterm/addon-fit', () => ({
  FitAddon: class {
    name = 'fit';
    proposeDimensions() {
      return xterm.fit.proposed;
    }
  },
}));

const HIDE = '\x1b[?25l';
let webgl = true;

beforeEach(() => {
  xterm.terminals.length = 0;
  xterm.fit.proposed = { cols: 80, rows: 48 };
  webgl = true;
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(() => (webgl ? ({} as never) : null));
});
afterEach(() => vi.useRealTimers());

const chunk = (seq: number, frames: [number, string][], size?: [number, number]) => ({
  seq,
  status: 'verified' as const,
  id: `id-${seq}`,
  body: {
    offset_ms_start: frames[0]![0],
    frames: frames.map(([t, data]) => [t, 'o', data]),
    cols: size?.[0] ?? 160,
    rows: size?.[1] ?? 48,
  },
});

/** The terminal view of the main session with the watch's pull answered from `history`. */
async function view(t: TaskWorld, history: ReturnType<typeof chunk>[] = []) {
  t.client.on(`GET /v1/scopes/${t.wg}/events`, () => ({
    events: history.map((item) => ({ scope_seq: item.seq, received_at: null, event: {} })),
    next_after: null,
    head: history.at(-1)?.seq ?? 0,
  }));
  const mounted = mount(t, [{ path: '/', element: <TerminalView org={t.org} wg={t.wg} session={MAIN} /> }], '/');
  mounted.terminal.read.mockImplementation(async (_org, _scope, items) =>
    items.map((item) => {
      const found = history.find((entry) => entry.seq === item.scope_seq)!;
      return {
        ...verified(t, 'session.terminal', found.body, { session: MAIN, seq: found.seq }),
      };
    }),
  );
  return mounted;
}

const screens = () => xterm.terminals.slice(-2);
const text = (index: number) =>
  screens()
    [index]!.writes.filter((item) => item !== HIDE)
    .join('');

describe('xterm screen', () => {
  it('opens read-only with the renderer loaded before open, hidden cursor and no input handler', () => {
    const host = document.createElement('div');
    const webglScreen = openScreen(host, 100, 30);
    const fake = webglScreen.terminal as unknown as InstanceType<typeof xterm.FakeTerminal>;
    expect(fake.options).toMatchObject({
      cols: 100,
      rows: 30,
      disableStdin: true,
      cursorBlink: false,
      cursorInactiveStyle: 'none',
      theme: { cursor: 'transparent', cursorAccent: 'transparent' },
    });
    expect(fake.addons).toEqual(['webgl', 'fit']);
    expect(fake.host).toBe(host);
    expect(fake.writes).toEqual([HIDE]);
    expect('onData' in fake).toBe(false);
    webgl = false;
    expect(hasWebgl2()).toBe(false);
    const canvas = openScreen(host, 10, 10).terminal as unknown as InstanceType<typeof xterm.FakeTerminal>;
    expect(canvas.addons).toEqual(['canvas', 'fit']);
    clearScreen(webglScreen);
    expect(fake.resets).toBe(1);
    expect(fake.writes).toEqual([HIDE]);
  });

  it('switches to the canvas renderer when the WebGL context is lost for good (KOR-05, A-24)', () => {
    lost.listeners.length = 0;
    const screen = openScreen(document.createElement('div'), 80, 24);
    const fake = screen.terminal as unknown as InstanceType<typeof xterm.FakeTerminal>;
    expect(fake.addons).toEqual(['webgl', 'fit']);
    expect(lost.listeners).toHaveLength(1);
    // the WebGL addon stays loaded: disposing it would install xterm's DOM renderer, whose <style> the CSP forbids
    lost.listeners[0]!();
    expect(fake.addons).toEqual(['webgl', 'fit', 'canvas']);
    expect(fake.disposed).toBe(false);
  });

  it('fits the font to the proposed columns within the bounds', () => {
    const screen = openScreen(document.createElement('div'), 160, 48);
    xterm.fit.proposed = { cols: 80, rows: 48 };
    expect(fitFont(screen)).toBe(MIN_FONT);
    xterm.fit.proposed = { cols: 200, rows: 48 };
    expect(fitFont(screen)).toBe(MAX_FONT);
    xterm.fit.proposed = { cols: 170, rows: 48 };
    expect(fitFont(screen)).toBe(13.5);
    xterm.fit.proposed = undefined;
    expect(fitFont(screen)).toBe(13);
    xterm.fit.proposed = { cols: Number.NaN, rows: 1 };
    expect(fitFont(screen)).toBe(13);
    xterm.fit.proposed = { cols: 0, rows: 1 };
    expect(fitFont(screen)).toBe(13);
    delete (screen.terminal.options as Record<string, unknown>).fontSize;
    xterm.fit.proposed = { cols: 160, rows: 48 };
    expect(fitFont(screen)).toBe(13);
  });

  it('labels offsets as minutes and seconds', () => {
    expect(offsetLabel(0)).toBe('0:00.0');
    expect(offsetLabel(65_432)).toBe('1:05.4');
  });
});

describe('terminal view', () => {
  it('writes history and live chunks in order in Follow mode and follows the recorded geometry', async () => {
    const t = await taskWorld();
    const mounted = await view(t, [chunk(2, [[0, 'ab']]), chunk(4, [[100, 'cd']])]);
    expect(screen.getByTestId('terminal.label')).toHaveTextContent('Read-only view – prompts are entered in the chat');
    expect(mounted.terminal.watch).toHaveBeenCalledWith(t.org, MAIN);
    await waitFor(() => expect(screen.getByTestId('terminal')).toHaveAttribute('data-chunks', '2'));
    expect(text(0)).toBe('abcd');
    expect(screen.getByTestId('terminal.screen.a')).toHaveAttribute('data-chars', '4');
    // a live chunk with a new geometry: both screens follow it and the font is fitted again
    xterm.fit.proposed = { cols: 160, rows: 24 };
    act(() =>
      putEvents(t.store, t.wg, [
        verified(t, 'session.output', {}, { session: MAIN, seq: 5 }),
        verified(t, 'session.terminal', chunk(6, [[200, 'ef']], [80, 24]).body, { session: MAIN, seq: 6 }),
      ]),
    );
    expect(text(0)).toBe('abcdef');
    expect(screens().map((item) => [item.cols, item.rows])).toEqual([
      [80, 24],
      [80, 24],
    ]);
    expect(screens().map((item) => item.options.fontSize)).toEqual([14, 14]);
    // the window resize refits the font of both screens
    xterm.fit.proposed = { cols: 40, rows: 24 };
    act(() => window.dispatchEvent(new Event('resize')));
    expect(screens().map((item) => item.options.fontSize)).toEqual([MIN_FONT, MIN_FONT]);
    // paste is cancelled
    const paste = new Event('paste', { bubbles: true, cancelable: true });
    screen.getByTestId('terminal.viewport').dispatchEvent(paste);
    expect(paste.defaultPrevented).toBe(true);
  });

  it('replays up to an offset into the hidden screen, plays back and returns to Follow', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'performance'] });
    const t = await taskWorld();
    await view(t, [
      chunk(1, [
        [0, 'a'],
        [500, 'b'],
      ]),
      chunk(2, [[1000, 'c']]),
    ]);
    await vi.waitFor(() => expect(text(0)).toBe('abc'));
    fireEvent.click(screen.getByTestId('terminal.mode.follow'));
    fireEvent.click(screen.getByTestId('terminal.mode.history'));
    expect(screen.getByTestId('terminal')).toHaveAttribute('data-mode', 'history');
    fireEvent.click(screen.getByTestId('terminal.mode.history'));
    expect(screen.getByTestId('terminal.position')).toHaveTextContent('0:01.0 of 0:01.0');
    expect(screen.getByTestId('terminal.slider')).toHaveValue('1000');
    fireEvent.change(screen.getByTestId('terminal.slider'), { target: { value: '400' } });
    await act(async () => vi.advanceTimersByTimeAsync(1));
    expect(text(1)).toBe('a');
    expect(screen.getByTestId('terminal.screen.b')).not.toHaveClass('invisible');
    expect(screen.getByTestId('terminal.screen.a')).toHaveClass('invisible');
    expect(screen.getByTestId('terminal.position')).toHaveTextContent('0:00.4 of 0:01.0');
    fireEvent.click(screen.getByTestId('terminal.speed.4'));
    expect(screen.getByTestId('terminal.speed.4')).toHaveAttribute('aria-pressed', 'true');
    fireEvent.click(screen.getByTestId('terminal.play'));
    expect(screen.getByTestId('terminal.play')).toHaveAttribute('data-playing', 'true');
    expect(screen.getByTestId('terminal.play')).toHaveTextContent('Pause');
    await act(async () => vi.advanceTimersByTimeAsync(50));
    fireEvent.click(screen.getByTestId('terminal.play'));
    expect(screen.getByTestId('terminal.play')).toHaveAttribute('data-playing', 'false');
    expect(text(1)).toBe('ab');
    // a live chunk during History is not written; Follow replays everything into the hidden screen
    act(() =>
      putEvents(t.store, t.wg, [
        verified(t, 'session.terminal', chunk(3, [[1500, 'd']]).body, { session: MAIN, seq: 3 }),
      ]),
    );
    expect(text(1)).toBe('ab');
    fireEvent.click(screen.getByTestId('terminal.mode.follow'));
    await act(async () => vi.advanceTimersByTimeAsync(1));
    expect(screen.getByTestId('terminal')).toHaveAttribute('data-mode', 'follow');
    expect(text(0)).toBe('abcd');
    expect(screen.getByTestId('terminal.screen.a')).toHaveAttribute('data-chars', '4');
    act(() =>
      putEvents(t.store, t.wg, [
        verified(t, 'session.terminal', chunk(4, [[1600, 'e']]).body, { session: MAIN, seq: 4 }),
      ]),
    );
    expect(text(0)).toBe('abcde');
  });

  it('serialises fast slider moves: only the newest waiting replay runs, the screens flip once', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'performance'] });
    const t = await taskWorld();
    await view(t, [
      chunk(1, [
        [0, 'a'],
        [500, 'b'],
      ]),
      chunk(2, [[1000, 'c']]),
    ]);
    await vi.waitFor(() => expect(text(0)).toBe('abc'));
    fireEvent.click(screen.getByTestId('terminal.mode.history'));
    const slider = screen.getByTestId('terminal.slider');
    fireEvent.change(slider, { target: { value: '100' } });
    fireEvent.change(slider, { target: { value: '600' } });
    fireEvent.change(slider, { target: { value: '700' } });
    await act(async () => vi.advanceTimersByTimeAsync(5));
    expect(text(1)).toBe('ab');
    expect(screens()[1]!.resets).toBe(2);
    expect(screen.getByTestId('terminal.screen.b')).not.toHaveClass('invisible');
    expect(screen.getByTestId('terminal.position')).toHaveTextContent('0:00.7 of 0:01.0');
  });

  it('shows loading, empty, failed and unverifiable states and no history controls without chunks', async () => {
    const t = await taskWorld();
    let release: () => void = () => undefined;
    t.client.on(
      `GET /v1/scopes/${t.wg}/events`,
      () => new Promise((resolve) => (release = () => resolve({ events: [], next_after: null, head: 0 }))),
    );
    const mounted = mount(t, [{ path: '/', element: <TerminalView org={t.org} wg={t.wg} session={MAIN} /> }], '/');
    expect(screen.getByTestId('terminal.history-loading')).toBeInTheDocument();
    act(() => release());
    expect(await screen.findByTestId('terminal.empty')).toHaveTextContent('No terminal output yet.');
    fireEvent.click(screen.getByTestId('terminal.mode.history'));
    expect(screen.queryByTestId('terminal.history')).toBeNull();
    act(() => terminalFailed(t.store, MAIN, 'rate_limited'));
    expect(screen.getByTestId('terminal.error')).toHaveAttribute('data-code', 'rate_limited');
    act(() =>
      t.store.update('terminal', (state) => ({
        sessions: {
          ...state.sessions,
          [MAIN]: applyPage(state.sessions[MAIN]!, [{ seq: 9, status: 'unverifiable', code: 'signature_invalid' }], 9),
        },
      })),
    );
    expect(screen.getByTestId('terminal.unverifiable')).toHaveTextContent(
      '1 unverifiable terminal chunks were left out (signature_invalid).',
    );
    expect(mounted.terminal.read).toHaveBeenCalledWith(t.org, t.wg, []);
  });

  it('works without a terminal entry (the watch has not opened one yet)', async () => {
    const t = await taskWorld();
    const mounted = mount(t, [{ path: '/', element: <TerminalView org={t.org} wg="other" session="none" /> }], '/');
    act(() => t.store.update('terminal', () => ({ sessions: {} })));
    expect(screen.getByTestId('terminal')).toHaveAttribute('data-chunks', '0');
    expect(screen.queryByTestId('terminal.history-loading')).toBeNull();
    expect(mounted.terminal.watch).toHaveBeenCalled();
  });
});

describe('session screen', () => {
  async function sessionPage(t: TaskWorld, path: string) {
    return mount(t, [{ path: '/o/:org/s/:session', element: <SessionScreen /> }], path);
  }

  it('loads the session, shows the subagent title, the session chat and the children', async () => {
    const t = await taskWorld();
    const child = sessionRow(t, {
      session_id: 'child-1',
      parent_session_id: MAIN,
      depth: 1,
      model_id: 'm',
      created_at: '2026-11-21T12:00:02.000Z',
    });
    t.client.on('GET /v1/sessions/child-1', () => ({ session: child, transitions: [], children: [] }));
    t.client.on(`GET /v1/tasks/${TASK}`, () => ({
      task: taskRow(t),
      sessions: [
        { session: sessionRow(t), transitions: [] },
        { session: child, transitions: [] },
      ],
    }));
    t.client.on(`GET /v1/scopes/${t.wg}/events`, () => {
      throw new HubError('unavailable', 503);
    });
    const { router } = await sessionPage(t, `/o/${t.org}/s/child-1`);
    expect(screen.getByTestId('session.loading')).toHaveTextContent('Loading the session…');
    expect(await screen.findByTestId('session.title')).toHaveTextContent('Subagent 1');
    // the terminal view is a lazy chunk: its label appears after the title, not in the same render
    expect(await screen.findByTestId('terminal.label')).toBeInTheDocument();
    fireEvent.click(screen.getByTestId('session.tabs.chat'));
    expect(await screen.findByTestId('chat.empty')).toHaveTextContent('This session has no output yet.');
    act(() =>
      putEvents(t.store, t.wg, [
        verified(t, 'session.output', { text: 'mine' }, { session: 'child-1' }),
        verified(t, 'session.output', { text: 'not mine' }, { session: MAIN }),
      ]),
    );
    expect(screen.getAllByTestId('chat.output')).toHaveLength(1);
    fireEvent.click(screen.getByTestId('session.tabs.children'));
    expect(screen.getByTestId('session.children.empty')).toBeInTheDocument();
    fireEvent.click(screen.getByTestId('session.back'));
    expect(router.state.location.pathname).toBe(`/o/${t.org}/t/${TASK}`);
  });

  it('lists the children of a main session and shows a load failure', async () => {
    const t = await taskWorld();
    t.client.on(`GET /v1/sessions/${MAIN}`, () => ({ session: sessionRow(t), children: ['c'] }));
    t.client.on(`GET /v1/tasks/${TASK}`, () => ({ task: taskRow(t), sessions: [] }));
    const { router } = await sessionPage(t, `/o/${t.org}/s/${MAIN}?tab=children`);
    expect(await screen.findByTestId('session.title')).toHaveTextContent('Session');
    act(() =>
      putSession(t.store, sessionRow(t, { session_id: 'c', parent_session_id: MAIN, depth: 1 }) as never, [], 0),
    );
    fireEvent.click(screen.getByTestId('session.child').querySelector('a')!);
    expect(router.state.location.pathname).toBe(`/o/${t.org}/s/c`);
    const other = await taskWorld();
    await sessionPage(other, `/o/${other.org}/s/missing`);
    expect(await screen.findByTestId('session.error')).toHaveTextContent('Not available');
  });
});
