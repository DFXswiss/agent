// U-WEB-ui-browser (KOR-44, ADR-085): the chat card of `browser.state` in every state (countdown, "Delayed", reason,
// "Watch live" into the Browser tab), the operation cards of `browser.open` / `browser.close`, and the Browser tab of
// the session detail: the live picture ("Live", its age, only the newest open browser session), the newest recorded
// picture, the bandwidth choice, the windowed record newest first (typed text as stored, errors, notices, unreadable
// entries, pictures only for rows in the window), refusal, failures, the work VM note, and a record of 5 000 actions
// with bounded rows and decoded pictures.
import { act, cleanup, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { Prefs } from '../../src/core/account/prefs';
import type { BrowserContext } from '../../src/core/browser/watch';
import type { BrowserListener } from '../../src/core/browser/watchlist';
import { CryptoError } from '../../src/core/crypto/errors';
import { HubError } from '../../src/core/net/signedFetch';
import { putEvents } from '../../src/core/store/slices/events';
import { BrowserTab, RECORD_ROWS } from '../../src/ui/browser/BrowserTab';
import { chatItem } from '../../src/ui/chat-items/browser.state';
import { KNOWN_TYPES } from '../../src/ui/chat-items/operation/Card';
import { SessionScreen } from '../../src/ui/screens/session/SessionScreen';
import {
  action,
  BROWSER,
  BROWSER2,
  read,
  RecordHub,
  SESSION,
  state,
  TASK as BROWSER_TASK,
  WG,
} from '../core/browser/world';
import { sessionRow, taskRow, type TaskWorld, taskWorld } from '../core/tasks/world';
import { MAIN, mount, TASK, verified } from './chat-world';

const START = Date.parse('2026-11-25T12:00:00.000Z');

// object URLs of jsdom do not exist: count the ones made and not yet revoked
const urls = { made: 0, alive: new Set<string>() };
const original = { create: URL.createObjectURL, revoke: URL.revokeObjectURL };

beforeEach(() => {
  urls.made = 0;
  urls.alive.clear();
  URL.createObjectURL = vi.fn(() => {
    const url = `blob:test/${(urls.made += 1)}`;
    urls.alive.add(url);
    return url;
  });
  URL.revokeObjectURL = vi.fn((url: string) => {
    urls.alive.delete(url);
  });
});

afterEach(() => {
  URL.createObjectURL = original.create;
  URL.revokeObjectURL = original.revoke;
  vi.useRealTimers();
});

describe('chat item browser.state', () => {
  function card(t: TaskWorld, body: unknown, sessionId: string | null = MAIN, at = START) {
    const { Component } = chatItem;
    const item = { key: 'k', kind: 'browser.state', at, seq: 1, sessionId, author: null, body };
    return mount(t, [{ path: '/o/:org/t/:task', element: <Component item={item} /> }], `/o/${t.org}/t/${TASK}`);
  }

  it('counts down while starting and says "Delayed" after the 10 s target', async () => {
    const t = await taskWorld();
    vi.useFakeTimers({ now: START + 4000, toFake: ['setInterval', 'clearInterval', 'Date'] });
    card(
      t,
      state({ state: 'starting', implicit: true, operation_id: null, workvm_id: null, url: null, ready_ms: null }),
    );
    expect(screen.getByTestId('chat.browser.state')).toHaveAttribute('data-state', 'starting');
    expect(screen.getByTestId('chat.browser.state.title')).toHaveTextContent('Browser starting');
    expect(screen.getByTestId('chat.browser.state.implicit')).toHaveTextContent('Opened by a browser tool');
    const text = screen.getByTestId('chat.browser.countdown.text');
    expect(text).toHaveTextContent('Starting for 4.0 s, target under 10 s.');
    act(() => vi.advanceTimersByTime(7000));
    expect(text).toHaveAttribute('data-overdue', 'true');
    expect(text).toHaveTextContent('Delayed: starting for 11.0 s.');
    expect(screen.queryByTestId('chat.browser.state.url')).toBeNull();
    expect(screen.queryByTestId('chat.browser.state.ready')).toBeNull();
  });

  it('shows an open browser with page, actions, VM ready time and "Watch live" into the Browser tab', async () => {
    const t = await taskWorld();
    const { router } = card(t, state({ actions: 3 }));
    expect(screen.getByTestId('chat.browser.state')).toHaveAttribute('data-browser', BROWSER);
    expect(screen.getByTestId('chat.browser.state.title')).toHaveTextContent('Browser open');
    expect(screen.queryByTestId('chat.browser.state.implicit')).toBeNull();
    expect(screen.queryByTestId('chat.browser.countdown')).toBeNull();
    expect(screen.getByTestId('chat.browser.state.url')).toHaveTextContent('Page: https://example.com/');
    expect(screen.getByTestId('chat.browser.state.actions')).toHaveTextContent('Actions: 3');
    expect(screen.getByTestId('chat.browser.state.ready')).toHaveTextContent('VM ready after 4.0 s');
    expect(screen.getByTestId('chat.browser.state.opened')).toHaveTextContent('Opened');
    fireEvent.click(screen.getByTestId('browser.watch'));
    expect(router.state.location.pathname).toBe(`/o/${t.org}/s/${MAIN}`);
    expect(router.state.location.search).toBe('?tab=browser');
  });

  it.each([
    [{ state: 'restarted', reason: 'vm_lost' }, 'Browser started again (the VM failed)', true],
    [{ state: 'restarted' }, 'Browser started again', true],
    [{ state: 'closed', reason: 'browser_idle' }, 'Browser closed (idle)', false],
    [{ state: 'closed', url: '', opened_at: null }, 'Browser closed', false],
  ])('shows %o', async (overrides, title, watch) => {
    const t = await taskWorld();
    card(t, state(overrides));
    expect(screen.getByTestId('chat.browser.state.title')).toHaveTextContent(title);
    expect(screen.queryByTestId('browser.watch') !== null).toBe(watch);
  });

  it('has no "Watch live" without a session and a neutral row for a body that does not follow the contract', async () => {
    const t = await taskWorld();
    card(t, state(), null);
    expect(screen.queryByTestId('browser.watch')).toBeNull();
    const other = await taskWorld();
    card(other, { ...state(), extra: 1 });
    expect(screen.getByTestId('chat.workvm.invalid')).toBeInTheDocument();
  });
});

describe('operation cards', () => {
  it('know browser.open and browser.close', () => {
    expect(KNOWN_TYPES.has('browser.open')).toBe(true);
    expect(KNOWN_TYPES.has('browser.close')).toBe(true);
  });
});

interface Tab {
  readonly t: TaskWorld;
  readonly hub: RecordHub;
  readonly listeners: BrowserListener[];
  readonly handle: { setKbps: ReturnType<typeof vi.fn>; release: ReturnType<typeof vi.fn> };
  readonly watch: ReturnType<typeof vi.fn>;
  readonly opener: ReturnType<typeof vi.fn>;
  readonly prefs: Prefs;
  readonly unmount: () => void;
}

/** The Browser tab of the browser world's session with `entries` in the record and `states` in the store. */
async function tab(
  entries: ReturnType<typeof read>[],
  states: Record<string, unknown>[] = [state()],
  storage: Record<string, string> = {},
): Promise<Tab> {
  const t = await taskWorld();
  const hub = new RecordHub(entries);
  const listeners: BrowserListener[] = [];
  const handle = { setKbps: vi.fn(), release: vi.fn() };
  const watch = vi.fn((_org: string, _session: string, listener: BrowserListener) => {
    listeners.push(listener);
    return handle;
  });
  const opener = vi.fn(() => async (frame: unknown) => {
    const { browser, seq, ts } = frame as { browser: string; seq: number; ts: string };
    return { browser, seq, ts: Date.parse(ts), jpeg: new Uint8Array([seq]) };
  });
  const context: BrowserContext = { ...hub.context(), store: t.store, watch, opener };
  const items = new Map(Object.entries(storage));
  const prefs = new Prefs({
    getItem: (key) => items.get(key) ?? null,
    setItem: (key, value) => items.set(key, value),
    removeItem: (key) => items.delete(key),
  });
  putEvents(
    t.store,
    WG,
    states.map((body, index) => read(1_000_000 + index, { kind: 'browser.state', body })),
  );
  const element = <BrowserTab org={t.org} wg={WG} task={BROWSER_TASK} session={SESSION} />;
  mount(t, [{ path: '/', element }], '/', 'en', context, prefs);
  await waitFor(() => expect(hub.requests.length).toBeGreaterThan(0));
  await waitFor(() => expect(screen.queryByTestId('browser.record.loading')).toBeNull(), { timeout: 30_000 });
  return {
    t,
    hub,
    listeners,
    handle,
    watch,
    opener,
    prefs,
    unmount: () => cleanup(),
  };
}

const rows = () => screen.queryAllByTestId('browser.record.row');
const live = (seq: number, ts = new Date().toISOString(), browser = BROWSER) => ({
  session_id: SESSION,
  frame: { browser, seq, ts },
});

describe('browser tab', () => {
  it('shows the record newest first with every field and the newest recorded picture', async () => {
    const t = await tab([
      read(1, { body: action(1, { tool: 'browser_navigate', action: 'navigate', target: null }) }),
      read(2, { body: action(2, { tool: 'browser_type', action: 'type', text: '••••', target: '#password' }) }),
      read(3, { body: action(3, { ok: false, error: 'No such element', notice: 'Dialog accepted', title: '' }) }),
      read(4, { body: action(4, { image: null, image_omitted: 'too_large' }) }),
      read(5, { body: action(5, { image: null }) }),
      read(6, { status: 'unverifiable' }),
      read(7, { body: action(7, { ok: false, error: null, image: null }) }),
    ]);
    expect(screen.getByTestId('browser.watch-only')).toHaveTextContent('Watch only – no input reaches the page.');
    expect(screen.getByTestId('browser.note.workvm')).toHaveTextContent(
      'The browser of the work VM (workvm_browse and scripts there) is neither recorded nor live.',
    );
    expect(screen.queryByTestId('browser.none')).toBeNull();
    expect(rows().map((row) => row.getAttribute('data-seq'))).toEqual(['7', '6', '5', '4', '3', '2', '1']);
    expect(rows()[1]).toHaveTextContent('This entry could not be read (signature_invalid).');
    const [seven, , five, four, three, two, one] = rows();
    expect(within(seven!).getByTestId('browser.record.result')).toHaveTextContent(/^Failed$/);
    expect(within(five!).getByTestId('browser.record.no-picture')).toHaveTextContent('No picture');
    expect(within(four!).getByTestId('browser.record.no-picture')).toHaveTextContent('No picture: too large');
    expect(within(three!).getByTestId('browser.record.result')).toHaveTextContent('Failed: No such element');
    expect(within(three!).getByTestId('browser.record.notice')).toHaveTextContent('Dialog accepted');
    expect(within(three!).queryByTestId('browser.record.title')).toBeNull();
    expect(two).toHaveAttribute('data-tool', 'browser_type');
    expect(within(two!).getByTestId('browser.record.action')).toHaveTextContent('browser_type · type');
    expect(within(two!).getByTestId('browser.record.text')).toHaveTextContent('••••');
    expect(within(two!).getByTestId('browser.record.target')).toHaveTextContent('#password');
    expect(within(one!).getByTestId('browser.record.result')).toHaveTextContent('Done');
    expect(within(one!).queryByTestId('browser.record.target')).toBeNull();
    expect(within(one!).getByTestId('browser.record.url')).toHaveTextContent('https://example.com/1');
    expect(within(one!).getByTestId('browser.record.title')).toHaveTextContent('Page 1');
    await waitFor(() => expect(within(one!).getByTestId('browser.record.picture')).toHaveAttribute('src'));
    // the newest recorded picture is the main picture, labelled with its time
    const picture = await screen.findByTestId('browser.live');
    expect(picture).toHaveAttribute('data-source', 'record');
    expect(picture).toHaveAttribute('data-seq', '3');
    expect(picture).toHaveAttribute('data-browser', BROWSER);
    expect(picture).toHaveAttribute('alt', 'The page after the action click');
    expect(screen.getByTestId('browser.live.badge')).toHaveTextContent('Recorded picture of');
    expect(screen.getByTestId('browser.record.range')).toHaveTextContent('Entries 1–7 of 7, newest first');
    expect(screen.getByTestId('browser.record.range')).not.toHaveAttribute('aria-live');
    expect(screen.queryByTestId('browser.record.older')).toBeNull();
    expect(t.watch).toHaveBeenCalledWith(t.t.org, SESSION, expect.anything(), 1000);
  });

  it('shows live frames of the newest open browser session with "Live" and the age after 30 s', async () => {
    const t = await tab([read(1)]);
    const [listener] = t.listeners;
    await act(async () => listener!.live(live(4)));
    const picture = await screen.findByTestId('browser.live');
    await waitFor(() => expect(picture).toHaveAttribute('data-source', 'live'));
    expect(picture).toHaveAttribute('data-seq', '4');
    expect(picture).toHaveAttribute('alt', "Live picture of the session's browser");
    expect(screen.getByTestId('browser.live.badge')).toHaveTextContent('Live');
    expect(screen.queryByTestId('browser.live.age')).toBeNull();
    // an older frame and a frame of another browser session are not shown
    await act(async () => listener!.live(live(3)));
    await act(async () => listener!.live(live(9, undefined, BROWSER2)));
    expect(screen.getByTestId('browser.live')).toHaveAttribute('data-seq', '4');
    await act(async () => listener!.live(live(5, new Date(Date.now() - 45_000).toISOString())));
    await waitFor(() => expect(screen.getByTestId('browser.live.age')).toHaveTextContent(/4[45] s old/), {
      timeout: 3000,
    });
    // a live entry arrives as a frame
    await act(async () => listener!.action({ scope_id: WG, scope_seq: 2, event: { header: {}, entry: read(2) } }));
    await waitFor(() => expect(rows()).toHaveLength(2));
    // the browser session closes: the newest recorded picture again
    act(() => putEvents(t.t.store, WG, [read(2_000_000, { kind: 'browser.state', body: state({ state: 'closed' }) })]));
    await waitFor(() => expect(screen.getByTestId('browser.live')).toHaveAttribute('data-source', 'record'));
    expect(screen.getByTestId('browser.live')).toHaveAttribute('data-seq', '2');
  });

  it('keeps the bandwidth choice in this browser and hands it to the watch', async () => {
    const t = await tab([], [state()], { 'agent-platform.browser-bandwidth': 'economical' });
    expect(t.watch).toHaveBeenCalledWith(t.t.org, SESSION, expect.anything(), 250);
    const select = screen.getByTestId('browser.kbps');
    expect(select).toHaveValue('economical');
    expect(
      within(select)
        .getAllByRole('option')
        .map((option) => option.textContent),
    ).toEqual(['Economical (250 kbit/s)', 'Standard (1000 kbit/s)', 'Sharp (3000 kbit/s)']);
    expect(screen.getByLabelText('Bandwidth of the live picture')).toBe(select);
    fireEvent.change(select, { target: { value: 'sharp' } });
    expect(select).toHaveValue('sharp');
    expect(t.prefs.browserBandwidth()).toBe('sharp');
    expect(t.handle.setKbps).toHaveBeenCalledWith(3000);
    expect(t.watch).toHaveBeenCalledTimes(1);
  });

  it('says when nothing is there yet, when the watch is refused and when a picture does not open', async () => {
    const t = await tab([], []);
    expect(screen.getByTestId('browser.none')).toHaveTextContent('This session has not opened a browser yet.');
    expect(screen.getByTestId('browser.record.empty')).toHaveTextContent('No actions recorded yet.');
    expect(screen.getByTestId('browser.picture.none')).toHaveTextContent('No picture yet.');
    expect(screen.queryByTestId('browser.live.badge')).toBeNull();
    expect(screen.queryByTestId('browser.refused')).toBeNull();
    act(() => t.listeners[0]!.refused());
    expect(screen.getByTestId('browser.refused')).toHaveTextContent('The browser of this session cannot be watched.');
    expect(screen.queryByTestId('browser.live.failed')).toBeNull();
  });

  it('shows the code of a live frame that does not open', async () => {
    const t = await taskWorld();
    const listeners: BrowserListener[] = [];
    const context: BrowserContext = {
      ...new RecordHub([]).context(),
      store: t.store,
      watch: (_org, _session, listener) => {
        listeners.push(listener);
        return { setKbps: () => undefined, release: () => undefined };
      },
      opener: () => () => Promise.reject(new CryptoError('decrypt_failed')),
    };
    putEvents(t.store, WG, [read(10, { kind: 'browser.state', body: state() })]);
    mount(
      t,
      [{ path: '/', element: <BrowserTab org={t.org} wg={WG} task={BROWSER_TASK} session={SESSION} /> }],
      '/',
      'en',
      context,
    );
    await act(async () => listeners[0]!.live(live(1)));
    expect(await screen.findByTestId('browser.live.failed')).toHaveTextContent(
      'A live picture could not be opened: Cannot be opened',
    );
  });

  it('reports a failed record and shows what was read while loading', async () => {
    const t = await taskWorld();
    const hub = new RecordHub([read(1)]);
    const context: BrowserContext = {
      ...hub.context(),
      store: t.store,
      watch: () => ({ setKbps: () => undefined, release: () => undefined }),
      opener: () => () => Promise.reject(new Error('none')),
    };
    let release!: () => void;
    hub.request.mockImplementationOnce(
      () =>
        new Promise((_resolve, reject) => {
          release = () => reject(new HubError('rate_limited', 429));
        }),
    );
    mount(
      t,
      [{ path: '/', element: <BrowserTab org={t.org} wg={WG} task={BROWSER_TASK} session={SESSION} /> }],
      '/',
      'en',
      context,
    );
    expect(await screen.findByTestId('browser.record.loading')).toHaveTextContent(
      'Loading the record… (0 entries so far)',
    );
    act(() => release());
    expect(await screen.findByTestId('browser.record.failed')).toHaveTextContent(
      'The record could not be loaded: Too many attempts. It is tried again.',
    );
    expect(screen.queryByTestId('browser.record.loading')).toBeNull();
  });

  it('pages the window, announces the position only after the user moved it and releases on unmount', async () => {
    const t = await tab(Array.from({ length: 45 }, (_, index) => read(index + 1)));
    expect(rows()).toHaveLength(RECORD_ROWS);
    expect(rows()[0]).toHaveAttribute('data-seq', '45');
    // only the newest page was read: older ones are loaded as the window reaches them
    expect(t.hub.requests).toHaveLength(1);
    expect(screen.getByTestId('browser.record.range')).toHaveTextContent(
      'Entries 1–20, newest first; older ones are loaded as you page on',
    );
    expect(screen.getByTestId('browser.record.newest')).toBeDisabled();
    expect(screen.getByTestId('browser.record.newer')).toBeDisabled();
    fireEvent.click(screen.getByTestId('browser.record.older'));
    await waitFor(() => expect(rows()[0]).toHaveAttribute('data-seq', '25'));
    expect(screen.getByTestId('browser.record.range')).toHaveTextContent('Entries 21–40, newest first;');
    expect(screen.getByTestId('browser.record.range')).toHaveAttribute('aria-live', 'polite');
    fireEvent.click(screen.getByTestId('browser.record.older'));
    await waitFor(() => expect(rows()).toHaveLength(5));
    expect(screen.getByTestId('browser.record.range')).toHaveTextContent('Entries 41–45 of 45, newest first');
    expect(screen.getByTestId('browser.record.older')).toBeDisabled();
    expect(t.hub.requests).toHaveLength(3);
    fireEvent.click(screen.getByTestId('browser.record.newer'));
    expect(rows()[0]).toHaveAttribute('data-seq', '25');
    fireEvent.click(screen.getByTestId('browser.record.newest'));
    expect(rows()[0]).toHaveAttribute('data-seq', '45');
    await waitFor(() => expect(screen.getAllByTestId('browser.record.picture')).toHaveLength(RECORD_ROWS));
    t.unmount();
    expect(t.handle.release).toHaveBeenCalled();
    expect(urls.alive.size).toBe(0);
  });
});

describe('record volume', () => {
  it('opens a record of 5 000 actions with one request and keeps a window of rows and pictures', async () => {
    const t = await tab(Array.from({ length: 5000 }, (_, index) => read(index + 1)));
    expect(rows()).toHaveLength(RECORD_ROWS);
    expect(rows()[0]).toHaveAttribute('data-seq', '5000');
    // opening costs one page, as for a record of 20 actions
    expect(t.hub.requests).toHaveLength(1);
    expect(t.hub.read.mock.calls.flatMap(([, , items]) => items)).toHaveLength(RECORD_ROWS);
    await waitFor(() => expect(screen.getAllByTestId('browser.record.picture')).toHaveLength(RECORD_ROWS));
    // the window's pictures and the main picture are decoded, nothing else
    expect(urls.alive.size).toBeLessThanOrEqual(RECORD_ROWS + 1);
    for (let page = 0; page < 5; page += 1) fireEvent.click(screen.getByTestId('browser.record.older'));
    await waitFor(() => expect(rows()[0]).toHaveAttribute('data-seq', '4900'));
    await waitFor(() => expect(screen.getAllByTestId('browser.record.picture')).toHaveLength(RECORD_ROWS));
    expect(urls.alive.size).toBeLessThanOrEqual(RECORD_ROWS + 1);
    // paging back 100 entries read only the pages up to the window, not the record
    const pages = t.hub.requests.filter((target) => target.includes(`limit=${RECORD_ROWS}&`));
    expect(pages.length).toBe(6);
    expect(document.querySelectorAll('li').length).toBeLessThanOrEqual(RECORD_ROWS);
  }, 60_000);
});

describe('session screen', () => {
  it('has a Browser tab that shows the platform browser of the session', async () => {
    const t = await taskWorld();
    t.client.on(`GET /v1/sessions/${MAIN}`, () => ({ session: sessionRow(t), transitions: [], children: [] }));
    t.client.on(`GET /v1/tasks/${TASK}`, () => ({
      task: taskRow(t),
      sessions: [{ session: sessionRow(t), transitions: [] }],
    }));
    t.client.on(`GET /v1/scopes/${t.wg}/events`, () => ({ events: [], next_after: null, head: 0 }));
    mount(t, [{ path: '/o/:org/s/:session', element: <SessionScreen /> }], `/o/${t.org}/s/${MAIN}?tab=browser`);
    const link = await screen.findByTestId('session.tabs.browser');
    expect(link).toHaveTextContent('Browser');
    expect(link).toHaveAttribute('aria-current', 'page');
    expect(await screen.findByTestId('browser.tab')).toBeInTheDocument();
    expect(screen.queryByTestId('terminal.label')).toBeNull();
    act(() => putEvents(t.store, t.wg, [verified(t, 'browser.state', state({ state: 'closed' }), { session: MAIN })]));
    expect(screen.queryByTestId('browser.none')).toBeNull();
  });
});
