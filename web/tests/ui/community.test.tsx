// U-WEB-ui-community (14-community.md section 6): the community screen in jsdom with the real store slice and the
// real `Community` client of the store, whose network actions are spied: redirect while locked, start and live
// channel, the states (loading, absent, error with retry, waiting for the key and after two minutes, failed key,
// empty, muted), messages grouped by day with name, mark, moderator badge, time, links and the removed, unverifiable
// and pending forms, "Load older messages", the read position while visible, the composer (first-post notice with the
// editable name, Enter and Shift+Enter, counter, offline, rate limit and catalog errors), the message menu (delete,
// remove, mute) and the muted users of a moderator; the navigation badge and the settings section "Account".
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { createMemoryRouter, MemoryRouter, RouterProvider } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { Prefs } from '../../src/core/account/prefs';
import { community } from '../../src/core/community/engine';
import { senderMark } from '../../src/core/community/mark';
import { createI18n } from '../../src/core/i18n';
import { HubError } from '../../src/core/net/signedFetch';
import { createStore, type Store } from '../../src/core/store';
import { type CommunityMessage, type CommunityState, patchCommunity } from '../../src/core/store/slices/community';
import { setOnline } from '../../src/core/store/slices/connection';
import { setSession } from '../../src/core/store/slices/me';
import { setActiveOrg, setPhase } from '../../src/core/store/slices/session';
import { I18nProvider } from '../../src/ui/i18n';
import { badgeText, type NavEntry } from '../../src/ui/nav';
import { BottomNav } from '../../src/ui/responsive/BottomNav';
import { CommunityScreen, LONG_WAIT_MS } from '../../src/ui/screens/community/CommunityScreen';
import { actionsOf } from '../../src/ui/screens/community/MessageActions';
import { lastName } from '../../src/ui/screens/community/MutedUsers';
import { communityHidden, communityUnread, nav } from '../../src/ui/screens/community/nav';
import { dayLabel } from '../../src/ui/screens/community/parts';
import { routes } from '../../src/ui/screens/community/routes';
import { AccountSection, section } from '../../src/ui/settings/sections/account';
import { AppEnvProvider } from '../../src/ui/shell/context';
import { Nav } from '../../src/ui/shell/Nav';
import { MemoryStorage } from './harness';

const ME = '0190f000-0000-7000-8000-0000000000e1';
const BOB = '0190f000-0000-7000-8000-0000000000b0';
const MOD = '0190f000-0000-7000-8000-0000000000d0';
const NOON = new Date();
NOON.setHours(12, 0, 0, 0);
const DAY = 86_400_000;

afterEach(() => vi.useRealTimers());

function message(seq: number, patch: Partial<CommunityMessage> = {}): CommunityMessage {
  return {
    seq,
    id: `m${seq}`,
    author: BOB,
    at: NOON.getTime() + seq * 60_000,
    removed: false,
    status: 'verified',
    name: 'Bob',
    text: `hello ${seq}`,
    moderator: false,
    ...patch,
  };
}

function world(options: { phase?: 'unlocked' | 'locked'; landing?: string | null } = {}) {
  const store = createStore();
  setSession(store, ME, { keyId: 'k', expiresAt: Date.now() + 3_600_000 });
  setPhase(store, options.phase ?? 'unlocked');
  const storage = new MemoryStorage();
  const prefs = new Prefs(storage);
  const client = { request: vi.fn(async () => ({})) };
  const app = {
    store,
    client,
    prefs,
    landingOrg: vi.fn(() => (options.landing === undefined ? 'o1' : options.landing)),
    activate: vi.fn(),
  };
  const engine = community({ store, client, prefs });
  const spies = {
    start: vi.spyOn(engine, 'start').mockResolvedValue(),
    markRead: vi.spyOn(engine, 'markRead').mockResolvedValue(),
    post: vi.spyOn(engine, 'post').mockResolvedValue(),
    remove: vi.spyOn(engine, 'remove').mockResolvedValue(),
    loadOlder: vi.spyOn(engine, 'loadOlder').mockResolvedValue(),
    mutes: vi.spyOn(engine, 'mutes').mockResolvedValue([]),
    setMuted: vi.spyOn(engine, 'setMuted').mockResolvedValue(),
  };
  return { store, app, engine, prefs, storage, spies };
}

type World = ReturnType<typeof world>;

function ready(store: Store, patch: Partial<CommunityState> = {}) {
  patchCommunity(store, { phase: 'ready', key: 'ready', me: ME, loaded: true, posted: true, ...patch });
}

function mount(w: World, locale: 'en' | 'de' = 'en') {
  const router = createMemoryRouter(
    [
      { path: routes[0]!.path!, element: <CommunityScreen /> },
      { path: '/login', element: <p data-testid="login">login</p> },
      { path: '*', element: <p data-testid="elsewhere">elsewhere</p> },
    ],
    { initialEntries: ['/community'] },
  );
  render(
    <AppEnvProvider value={{ app: w.app as never, credentials: {} as never, origin: 'https://hub.example' }}>
      <I18nProvider value={createI18n(locale)}>
        <RouterProvider router={router} />
      </I18nProvider>
    </AppEnvProvider>,
  );
  return router;
}

describe('community screen states', () => {
  it('loads the screen lazily from its route', async () => {
    const loaded = await routes[0]!.lazy!();
    expect(loaded).toMatchObject({ Component: CommunityScreen });
    expect(routes[0]!.path).toBe('/community');
  });

  it('sends a locked browser to the sign-in and back', async () => {
    const w = world({ phase: 'locked' });
    const router = mount(w);
    await screen.findByTestId('login');
    expect(router.state.location.search).toBe(`?next=${encodeURIComponent('/community')}`);
    expect(w.spies.start).not.toHaveBeenCalled();
  });

  it('starts the client, opens the landing org channel and shows loading', () => {
    const w = world();
    mount(w);
    expect(screen.getByTestId('community.loading')).toHaveTextContent('Loading the community');
    expect(w.spies.start).toHaveBeenCalled();
    expect(w.app.activate).toHaveBeenCalledWith('o1');
  });

  it('keeps an active org, and does without a landing org', () => {
    const active = world();
    setActiveOrg(active.store, 'o7');
    mount(active);
    expect(active.app.activate).not.toHaveBeenCalled();
    const lonely = world({ landing: null });
    mount(lonely);
    expect(lonely.app.activate).not.toHaveBeenCalled();
  });

  it('says when the hub has no community', () => {
    const w = world();
    patchCommunity(w.store, { phase: 'absent' });
    mount(w);
    expect(screen.getByTestId('community.absent')).toHaveTextContent('not available');
  });

  it('shows a start error from the catalog and tries again', () => {
    const w = world();
    patchCommunity(w.store, { phase: 'error', code: 'unavailable' });
    mount(w, 'de');
    const failure = screen.getByTestId('community.error');
    expect(failure).toHaveAttribute('data-code', 'unavailable');
    w.spies.start.mockClear();
    fireEvent.click(within(failure).getByTestId('community.retry'));
    expect(w.spies.start).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId('community.retry')).toHaveTextContent('Erneut versuchen');
  });

  it('waits for the key, and says after two minutes that another user must be online', () => {
    const w = world();
    ready(w.store, { key: 'waiting', waitingSince: Date.now() });
    mount(w);
    expect(screen.getByTestId('community.waiting')).toHaveAttribute('data-long', 'false');
    expect(screen.getByTestId('community.waiting')).toHaveTextContent('Community is being unlocked…');
    expect(screen.queryByTestId('community.composer')).toBeNull();
    act(() => patchCommunity(w.store, { waitingSince: Date.now() - LONG_WAIT_MS - 1000 }));
    expect(screen.getByTestId('community.waiting')).toHaveAttribute('data-long', 'true');
    expect(screen.getByTestId('community.waiting')).toHaveTextContent('as soon as another user is online');
    act(() => patchCommunity(w.store, { key: 'none', waitingSince: null }));
    expect(screen.getByTestId('community.waiting')).toHaveAttribute('data-long', 'false');
  });

  it('shows a failed key, the empty state and the muted state', () => {
    const w = world();
    ready(w.store, { key: 'failed', keyCode: 'decrypt_failed', muted: true });
    mount(w);
    expect(screen.getByTestId('community.key-failed')).toHaveTextContent('could not be unlocked');
    expect(screen.getByTestId('community.empty')).toHaveTextContent('No messages yet – write the first one.');
    expect(screen.getByTestId('community.muted')).toHaveTextContent('You have been muted in the community');
    expect(screen.queryByTestId('community.composer')).toBeNull();
    act(() => patchCommunity(w.store, { key: 'ready', muted: false }));
    expect(screen.getByTestId('community.composer')).toBeInTheDocument();
    // before the newest page arrived, "empty" is not claimed
    act(() => patchCommunity(w.store, { loaded: false }));
    expect(screen.queryByTestId('community.empty')).toBeNull();
  });
});

describe('community messages', () => {
  it('groups by day and shows name, mark, badge, time, links and the special forms', async () => {
    const w = world();
    ready(w.store, {
      messages: [
        message(1, { at: NOON.getTime() - 5 * DAY, name: 'Old' }),
        message(2, { at: NOON.getTime() - DAY, author: MOD, name: 'Mia', moderator: true }),
        message(3, { text: 'see https://dfx.swiss/x. and <b>bold</b>\nnext line' }),
        message(4, { removed: true, name: null, text: null }),
        message(5, { status: 'unverifiable', name: null, text: null }),
        message(6, { status: 'pending', name: null, text: null, at: 0 }),
        message(7, { author: ME, name: 'Me' }),
        message(8, { name: 'Ada', agent: true }),
      ],
    });
    mount(w);
    const days = screen.getAllByTestId('community.day');
    expect(days.map((day) => day.textContent)).toEqual([
      new Intl.DateTimeFormat('en', { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' }).format(
        NOON.getTime() - 5 * DAY,
      ),
      'Yesterday',
      'Today',
      // the message without a time is a day of its own
      expect.any(String),
      'Today',
    ]);
    const items = screen.getAllByTestId('community.message');
    expect(items.map((item) => item.dataset.status)).toEqual([
      'verified',
      'verified',
      'verified',
      'removed',
      'unverifiable',
      'pending',
      'verified',
      'verified',
    ]);
    // a message of an own-identity agent (ADR-082)
    expect(within(items[7]!).getByTestId('community.message.name')).toHaveTextContent('Agent of Ada');
    const mia = items[1]!;
    expect(within(mia).getByTestId('community.message.name')).toHaveTextContent('Mia');
    expect(within(mia).getByTestId('community.message.moderator')).toHaveTextContent('Moderator');
    const mark = await senderMark(MOD);
    await waitFor(() => expect(within(mia).getByTestId('community.message.mark')).toHaveTextContent(mark));
    expect(mark).toMatch(/^#[A-Z2-7]{4}$/);
    const time = within(items[2]!).getByTestId('community.message.time');
    expect(time).toHaveTextContent('12:03');
    expect(time).toHaveAttribute('dateTime', new Date(NOON.getTime() + 3 * 60_000).toISOString());
    expect(time.getAttribute('title')).toMatch(/12:03:00/);
    const text = within(items[2]!).getByTestId('community.message.text');
    expect(text.textContent).toBe('see https://dfx.swiss/x (opens in a new tab). and <b>bold</b>\nnext line');
    expect(text.querySelector('b')).toBeNull();
    const link = within(text).getByTestId('community.message.link');
    expect(link).toHaveAttribute('href', 'https://dfx.swiss/x');
    expect(link).toHaveAttribute('target', '_blank');
    expect(link).toHaveAttribute('rel', 'noopener noreferrer nofollow');
    expect(within(items[3]!).getByTestId('community.message.removed')).toHaveTextContent('Message removed');
    expect(within(items[4]!).getByTestId('community.message.unverifiable')).toHaveTextContent(
      'Message could not be verified',
    );
    expect(within(items[5]!).getByTestId('community.message.pending')).toBeInTheDocument();
    expect(within(items[5]!).queryByTestId('community.message.time')).toBeNull();
    expect(within(items[4]!).queryByTestId('community.message.name')).toBeNull();
    expect(items[6]).toHaveAttribute('data-own', 'true');
    expect(items[0]).toHaveAttribute('data-own', 'false');
    // the mark of a known user comes from the cache at once
    act(() => patchCommunity(w.store, { messages: [message(8, { author: MOD })] }));
    expect(screen.getByTestId('community.message.mark')).toHaveTextContent(mark);
  });

  it('labels days in German', () => {
    const t = createI18n('de').t;
    expect(dayLabel(NOON.getTime(), NOON.getTime(), 'de', t)).toBe('Heute');
    expect(dayLabel(NOON.getTime() - DAY, NOON.getTime(), 'de', t)).toBe('Gestern');
  });

  it('loads older messages and reports a failure', async () => {
    const w = world();
    ready(w.store, { older: true, messages: [message(5)] });
    mount(w);
    let finish: () => void = () => undefined;
    w.spies.loadOlder.mockImplementationOnce(() => new Promise<void>((resolve) => (finish = resolve)));
    fireEvent.click(screen.getByTestId('community.older'));
    expect(screen.getByTestId('community.older')).toHaveTextContent('Loading older messages');
    expect(screen.getByTestId('community.older')).toBeDisabled();
    await act(async () => finish());
    expect(screen.getByTestId('community.older')).toHaveTextContent('Load older messages');
    expect(screen.getByTestId('community.older.error')).not.toBeVisible();
    w.spies.loadOlder.mockRejectedValueOnce(new HubError('unavailable', 503));
    fireEvent.click(screen.getByTestId('community.older'));
    await waitFor(() => expect(screen.getByTestId('community.older.error')).toHaveTextContent('Service unavailable'));
    expect(screen.getByTestId('community.older.error')).toBeVisible();
    act(() => patchCommunity(w.store, { older: false }));
    expect(screen.queryByTestId('community.older')).toBeNull();
  });

  it('moves the read position to the newest message while the page is visible', async () => {
    const w = world();
    ready(w.store, { lastRead: 2, messages: [message(1), message(2)] });
    mount(w);
    expect(w.spies.markRead).not.toHaveBeenCalled();
    act(() => patchCommunity(w.store, { messages: [message(1), message(2), message(3)] }));
    await waitFor(() => expect(w.spies.markRead).toHaveBeenCalledWith(3));
    // a failure is quiet; a hidden page sets nothing
    w.spies.markRead.mockClear();
    w.spies.markRead.mockRejectedValueOnce(new HubError('unavailable', 503));
    act(() => patchCommunity(w.store, { messages: [message(1), message(2), message(3), message(4)] }));
    await waitFor(() => expect(w.spies.markRead).toHaveBeenCalledWith(4));
    w.spies.markRead.mockClear();
    const hidden = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden');
    act(() => {
      document.dispatchEvent(new Event('visibilitychange'));
      patchCommunity(w.store, { messages: [message(1), message(5)] });
    });
    expect(w.spies.markRead).not.toHaveBeenCalled();
    hidden.mockReturnValue('visible');
    act(() => document.dispatchEvent(new Event('visibilitychange')));
    await waitFor(() => expect(w.spies.markRead).toHaveBeenCalledWith(5));
  });

  it('follows new messages at the bottom of the page, and own ones always', () => {
    const scroll = vi.fn();
    Element.prototype.scrollIntoView = scroll;
    try {
      const w = world();
      ready(w.store, { messages: [message(1)] });
      mount(w);
      expect(scroll).toHaveBeenCalledTimes(1);
      // scrolled up: a message of another user does not move the page, an own one does
      vi.spyOn(document.documentElement, 'scrollHeight', 'get').mockReturnValue(5000);
      act(() => window.dispatchEvent(new Event('scroll')));
      act(() => patchCommunity(w.store, { messages: [message(1), message(2)] }));
      expect(scroll).toHaveBeenCalledTimes(1);
      act(() => patchCommunity(w.store, { messages: [message(1), message(2), message(3, { author: ME })] }));
      expect(scroll).toHaveBeenCalledTimes(2);
      // a re-render without a new message does not scroll
      act(() => patchCommunity(w.store, { lastRead: 3 }));
      expect(scroll).toHaveBeenCalledTimes(2);
    } finally {
      delete (Element.prototype as { scrollIntoView?: unknown }).scrollIntoView;
    }
  });
});

describe('community composer', () => {
  it('shows the notice and the editable name before the first post, then sends with Enter', async () => {
    const w = world();
    w.prefs.setCommunityName(ME, 'Ada');
    ready(w.store, { posted: false, messages: [message(1)] });
    mount(w);
    expect(screen.getByTestId('community.notice.text')).toHaveTextContent(
      'Your name is visible to all users of agent.dfx.swiss.',
    );
    expect(screen.getByTestId('community.notice.text')).toHaveTextContent('do not share confidential information');
    expect(screen.getByTestId('community.notice.security')).toHaveAttribute('href', '/security');
    const name = screen.getByTestId('community.name');
    expect(name).toHaveValue('Ada');
    fireEvent.change(name, { target: { value: '  Ada L.  ' } });
    const field = screen.getByTestId('community.composer.text');
    const send = screen.getByTestId('community.composer.send');
    expect(send).toBeDisabled();
    fireEvent.change(field, { target: { value: '  first line\r\nsecond​  ' } });
    // Shift+Enter breaks the line, an Enter while composing (IME) does nothing
    fireEvent.keyDown(field, { key: 'Enter', shiftKey: true });
    fireEvent.keyDown(field, { key: 'Enter', isComposing: true });
    fireEvent.keyDown(field, { key: 'a' });
    expect(w.spies.post).not.toHaveBeenCalled();
    fireEvent.keyDown(field, { key: 'Enter' });
    await waitFor(() => expect(w.spies.post).toHaveBeenCalledWith('Ada L.', 'first line\nsecond'));
    await waitFor(() => expect(field).toHaveValue(''));
  });

  it('keeps what was typed while a post was on its way', async () => {
    const w = world();
    ready(w.store);
    w.prefs.setCommunityName(ME, 'Ada');
    mount(w);
    let finish: () => void = () => undefined;
    w.spies.post.mockImplementationOnce(() => new Promise<void>((resolve) => (finish = resolve)));
    const field = screen.getByTestId('community.composer.text');
    fireEvent.change(field, { target: { value: 'one' } });
    fireEvent.keyDown(field, { key: 'Enter' });
    expect(screen.getByTestId('community.composer.send')).toBeDisabled();
    fireEvent.change(field, { target: { value: 'two' } });
    // Enter while the post runs sends nothing more
    fireEvent.keyDown(field, { key: 'Enter' });
    await act(async () => finish());
    expect(field).toHaveValue('two');
    expect(w.spies.post).toHaveBeenCalledTimes(1);
  });

  it('asks for a name, and offers to change it after the first post', () => {
    const w = world();
    ready(w.store, { posted: false });
    mount(w);
    expect(screen.getByTestId('community.name')).toHaveValue('');
    expect(screen.getByTestId('community.name.missing')).toBeInTheDocument();
    fireEvent.change(screen.getByTestId('community.composer.text'), { target: { value: 'hi' } });
    expect(screen.getByTestId('community.composer.send')).toBeDisabled();
    fireEvent.keyDown(screen.getByTestId('community.composer.text'), { key: 'Enter' });
    expect(w.spies.post).not.toHaveBeenCalled();
    // after a post the name comes from the newest own message and can be changed
    act(() => patchCommunity(w.store, { posted: true, messages: [message(1, { author: ME, name: 'Ada' })] }));
    expect(screen.getByTestId('community.name.as')).toHaveTextContent('You write as Ada.');
    expect(screen.queryByTestId('community.notice')).toBeNull();
    fireEvent.click(screen.getByTestId('community.name.change'));
    expect(screen.getByTestId('community.name')).toHaveValue('Ada');
  });

  it('counts the characters near the limit and refuses too long a text', () => {
    const w = world();
    ready(w.store, { textChars: 100 });
    w.prefs.setCommunityName(ME, 'Ada');
    mount(w);
    const field = screen.getByTestId('community.composer.text');
    fireEvent.change(field, { target: { value: 'x'.repeat(80) } });
    expect(screen.queryByTestId('community.composer.counter')).toBeNull();
    fireEvent.change(field, { target: { value: 'x'.repeat(95) } });
    expect(screen.getByTestId('community.composer.counter')).toHaveTextContent('5 characters left');
    expect(screen.getByTestId('community.composer.send')).toBeEnabled();
    fireEvent.change(field, { target: { value: '😀'.repeat(102) } });
    expect(screen.getByTestId('community.composer.counter')).toHaveTextContent('2 characters too many');
    expect(screen.getByTestId('community.composer.send')).toBeDisabled();
  });

  it('says Offline without a connection', () => {
    const w = world();
    ready(w.store);
    w.prefs.setCommunityName(ME, 'Ada');
    setOnline(w.store, false);
    mount(w, 'de');
    expect(screen.getByTestId('community.composer.send')).toHaveTextContent('Offline');
    expect(screen.getByTestId('community.composer.send')).toBeDisabled();
  });

  it('shows the wait time of the rate limit and catalog errors, keeping the text', async () => {
    const w = world();
    ready(w.store);
    w.prefs.setCommunityName(ME, 'Ada');
    mount(w);
    const field = screen.getByTestId('community.composer.text');
    fireEvent.change(field, { target: { value: 'fast' } });
    w.spies.post.mockRejectedValueOnce(new HubError('rate_limited', 429, null, 7));
    fireEvent.click(screen.getByTestId('community.composer.send'));
    expect(await screen.findByTestId('community.composer.error')).toHaveTextContent(
      'You are writing very fast. Please wait 7 seconds, then send again.',
    );
    expect(field).toHaveValue('fast');
    w.spies.post.mockRejectedValueOnce(new HubError('rate_limited', 429));
    fireEvent.submit(screen.getByTestId('community.composer'));
    await waitFor(() =>
      expect(screen.getByTestId('community.composer.error')).toHaveTextContent('Please wait a moment'),
    );
    w.spies.post.mockRejectedValueOnce(new HubError('community_muted', 403));
    fireEvent.click(screen.getByTestId('community.composer.send'));
    await waitFor(() =>
      expect(screen.getByTestId('community.composer.error')).toHaveAttribute('data-code', 'community_muted'),
    );
    expect(screen.getByTestId('community.composer.error')).toHaveTextContent('Muted in the community');
    w.spies.post.mockRejectedValueOnce(new Error('network'));
    fireEvent.click(screen.getByTestId('community.composer.send'));
    await waitFor(() =>
      expect(screen.getByTestId('community.composer.error')).toHaveAttribute('data-code', 'unavailable'),
    );
  });
});

describe('community message menu', () => {
  it('deletes an own message after confirmation and reports a failure', async () => {
    const w = world();
    ready(w.store, { messages: [message(1, { author: ME, name: 'Ada' }), message(2)] });
    mount(w);
    const [own, other] = screen.getAllByTestId('community.message');
    expect(within(other!).queryByTestId('community.message.menu')).toBeNull();
    const menu = within(own!).getByTestId('community.message.menu');
    expect(menu).toHaveAttribute('aria-label', 'Actions for the message of Ada');
    fireEvent.click(menu);
    const sheet = screen.getByTestId('community.actions');
    expect(within(sheet).queryByTestId('community.actions.remove')).toBeNull();
    fireEvent.click(within(sheet).getByTestId('community.actions.delete'));
    expect(within(sheet).getByTestId('community.actions.question')).toHaveTextContent(
      'Delete this message for everyone?',
    );
    w.spies.remove.mockRejectedValueOnce(new HubError('forbidden', 403));
    fireEvent.click(within(sheet).getByTestId('community.actions.confirm'));
    expect(await within(sheet).findByTestId('community.actions.error')).toHaveTextContent('No access');
    fireEvent.click(within(sheet).getByTestId('community.actions.confirm'));
    await waitFor(() => expect(screen.queryByTestId('community.actions')).toBeNull());
    expect(w.spies.remove).toHaveBeenLastCalledWith('m1');
    // cancelling closes without action
    fireEvent.click(within(own!).getByTestId('community.message.menu'));
    fireEvent.click(screen.getByTestId('community.actions.cancel'));
    expect(screen.queryByTestId('community.actions')).toBeNull();
    expect(w.spies.remove).toHaveBeenCalledTimes(2);
  });

  it('lets a moderator remove and mute, but not mute another moderator', async () => {
    const w = world();
    ready(w.store, {
      moderator: true,
      messages: [message(1), message(2, { author: MOD, name: 'Mia', moderator: true }), message(3, { removed: true })],
    });
    mount(w);
    const [bob, mia, removed] = screen.getAllByTestId('community.message');
    expect(within(removed!).queryByTestId('community.message.menu')).toBeNull();
    fireEvent.click(within(mia!).getByTestId('community.message.menu'));
    expect(screen.queryByTestId('community.actions.mute')).toBeNull();
    fireEvent.click(screen.getByTestId('community.actions.remove'));
    expect(screen.getByTestId('community.actions.question')).toHaveTextContent('Remove this message for everyone?');
    fireEvent.click(screen.getByTestId('community.actions.confirm'));
    await waitFor(() => expect(w.spies.remove).toHaveBeenCalledWith('m2'));
    fireEvent.click(within(bob!).getByTestId('community.message.menu'));
    fireEvent.click(screen.getByTestId('community.actions.mute'));
    expect(screen.getByTestId('community.actions.question')).toHaveTextContent('Mute Bob?');
    fireEvent.click(screen.getByTestId('community.actions.confirm'));
    await waitFor(() => expect(w.spies.setMuted).toHaveBeenCalledWith(BOB, true));
    expect(await screen.findByTestId('community.status')).toHaveTextContent('Bob is muted.');
    expect(screen.queryByTestId('community.actions')).toBeNull();
  });

  it('names a message without a name "Unknown"', () => {
    const w = world();
    ready(w.store, { moderator: true, messages: [message(1, { status: 'unverifiable', name: null, text: null })] });
    mount(w);
    expect(screen.getByTestId('community.message.menu')).toHaveAttribute(
      'aria-label',
      'Actions for the message of Unknown',
    );
  });

  it('knows which actions apply', () => {
    expect(actionsOf(message(1), true, true)).toEqual(['delete']);
    expect(actionsOf(message(1), false, false)).toEqual([]);
    expect(actionsOf(message(1, { removed: true }), true, true)).toEqual([]);
  });
});

describe('muted users', () => {
  it('lists the muted users with the last known name and mark, and unmutes', async () => {
    const w = world();
    const mutedAt = NOON.getTime() - DAY;
    ready(w.store, { moderator: true, messages: [message(1, { name: 'Old Bob' }), message(2, { name: 'Bob' })] });
    w.spies.mutes.mockResolvedValueOnce([
      { user: BOB, mutedAt },
      { user: MOD, mutedAt: 0 },
    ]);
    mount(w);
    fireEvent.click(screen.getByTestId('community.mutes.open'));
    expect(screen.getByTestId('community.mutes.loading')).toBeInTheDocument();
    const items = await screen.findAllByTestId('community.mutes.item');
    expect(within(items[0]!).getByTestId('community.mutes.name')).toHaveTextContent('Bob');
    expect(within(items[0]!).getByTestId('community.mutes.time')).toHaveTextContent('muted since');
    expect(within(items[1]!).queryByTestId('community.mutes.name')).toBeNull();
    expect(within(items[1]!).queryByTestId('community.mutes.time')).toBeNull();
    await waitFor(() =>
      expect(within(items[1]!).getByTestId('community.mutes.mark').textContent).toMatch(/^#[A-Z2-7]{4}$/),
    );
    // a failed unmute, then one that works and reads the list again
    w.spies.setMuted.mockRejectedValueOnce(new HubError('unavailable', 503));
    fireEvent.click(within(items[0]!).getByTestId('community.mutes.unmute'));
    expect(await screen.findByTestId('community.mutes.error')).toHaveTextContent('Service unavailable');
    w.spies.mutes.mockResolvedValueOnce([]);
    fireEvent.click(within(screen.getAllByTestId('community.mutes.item')[0]!).getByTestId('community.mutes.unmute'));
    expect(await screen.findByTestId('community.mutes.empty')).toHaveTextContent('Nobody is muted.');
    expect(w.spies.setMuted).toHaveBeenLastCalledWith(BOB, false);
    expect(screen.queryByTestId('community.mutes.error')).toBeNull();
    fireEvent.click(screen.getByTestId('community.mutes.close'));
    expect(screen.queryByTestId('community.mutes')).toBeNull();
    // Escape closes the sheet too
    fireEvent.click(screen.getByTestId('community.mutes.open'));
    fireEvent.keyDown(screen.getByTestId('community.mutes'), { key: 'Escape' });
    expect(screen.queryByTestId('community.mutes')).toBeNull();
  });

  it('reports a list that cannot be read', async () => {
    const w = world();
    ready(w.store, { moderator: true });
    w.spies.mutes.mockRejectedValueOnce(new HubError('forbidden', 403));
    mount(w);
    fireEvent.click(screen.getByTestId('community.mutes.open'));
    expect(await screen.findByTestId('community.mutes.error')).toHaveTextContent('No access');
    expect(screen.queryByTestId('community.mutes.loading')).toBeNull();
  });

  it('finds the last known name', () => {
    expect(lastName([message(1, { name: 'A' }), message(2, { name: null })], BOB)).toBe('A');
    expect(lastName([message(1)], ME)).toBeNull();
  });
});

describe('community navigation', () => {
  function shell(w: World, entries: NavEntry[]) {
    return render(
      <AppEnvProvider value={{ app: w.app as never, credentials: {} as never, origin: 'https://hub.example' }}>
        <I18nProvider value={createI18n('en')}>
          <MemoryRouter>
            <Nav entries={entries} />
            <BottomNav entries={entries} />
          </MemoryRouter>
        </I18nProvider>
      </AppEnvProvider>,
    );
  }

  it('shows the entry with the unread badge, on the phone in More and as a dot', () => {
    const w = world();
    setActiveOrg(w.store, 'o1');
    const entries = [{ id: 'home', order: 10, to: '/o/:org', labelKey: 'home.nav' }, ...nav];
    shell(w, entries);
    expect(screen.getByTestId('shell.nav.community')).toHaveAttribute('href', '/community');
    expect(screen.queryByTestId('shell.nav.community.badge')).toBeNull();
    expect(screen.queryByTestId('shell.bottom.more.dot')).toBeNull();
    act(() => ready(w.store, { lastRead: 0, baseHead: 3, unreadBase: 3 }));
    expect(screen.getByTestId('shell.nav.community.badge')).toHaveTextContent('3');
    expect(screen.getByTestId('shell.bottom.more.dot')).toBeInTheDocument();
    fireEvent.click(screen.getByTestId('shell.bottom.more'));
    expect(screen.getByTestId('shell.more.community.badge')).toHaveTextContent('3');
    expect(screen.queryByTestId('shell.more.settings.badge')).toBeNull();
    act(() => patchCommunity(w.store, { unreadBase: 150, baseHead: 150 }));
    expect(screen.getByTestId('shell.nav.community.badge')).toHaveTextContent('99+');
    act(() => patchCommunity(w.store, { phase: 'absent' }));
    expect(screen.queryByTestId('shell.nav.community')).toBeNull();
    expect(screen.queryByTestId('shell.more.community')).toBeNull();
    expect(screen.queryByTestId('shell.bottom.more.dot')).toBeNull();
  });

  it('hides the entry while signed out', () => {
    const w = world({ phase: 'locked' });
    shell(w, nav);
    expect(screen.queryByTestId('shell.nav')).toBeNull();
    expect(communityHidden(w.store.getState())).toBe(true);
    expect(communityUnread(w.store.getState())).toBe(0);
    expect(badgeText(0)).toBeNull();
    expect(badgeText(100)).toBe('99+');
  });
});

describe('settings section "Account"', () => {
  function account(w: World) {
    return render(
      <AppEnvProvider value={{ app: w.app as never, credentials: {} as never, origin: 'https://hub.example' }}>
        <I18nProvider value={createI18n('en')}>
          <AccountSection />
        </I18nProvider>
      </AppEnvProvider>,
    );
  }

  it('shows the user ID and copies it', async () => {
    expect(section).toMatchObject({ id: 'account', titleKey: 'account.title' });
    const w = world();
    const writeText = vi.fn(async () => undefined);
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
    account(w);
    expect(screen.getByTestId('settings.account.user-id')).toHaveTextContent(ME);
    fireEvent.click(screen.getByTestId('settings.account.copy'));
    expect(await screen.findByTestId('settings.account.copied')).toHaveTextContent('Copied.');
    expect(writeText).toHaveBeenCalledWith(ME);
    writeText.mockRejectedValueOnce(new Error('denied'));
    fireEvent.click(screen.getByTestId('settings.account.copy'));
    expect(await screen.findByTestId('settings.account.failed')).toHaveTextContent('select the ID');
  });

  it('shows nothing without a signed-in user', () => {
    const w = world();
    w.store.update('me', (me) => ({ ...me, userId: null }));
    const { container } = account(w);
    expect(container.innerHTML).toBe('');
  });
});
