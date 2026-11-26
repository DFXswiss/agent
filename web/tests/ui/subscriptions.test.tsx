// U-WEB-ui-subscriptions (KOR-68, rule E-30): the card "My subscriptions" of the home page (hidden while loading, the
// way to add one, the provider rows), the page `/subscriptions` (loading, failure and reload, empty with "Add
// subscription", "detailed" and "sort by used", the provider details with every account, the table with every column
// and "Log in now", the redirect when locked), its refresh, the settings link and the guard. The data come from the
// fake hub routes of KOR-69 and are opened like in the browser (`loadSubscriptions`).
import { act, cleanup, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ed25519KeyPair } from '../../src/core/crypto/keys';
import { buildPlatformEntries } from '../../src/core/log/entries';
import { appendPlatform } from '../../src/core/log/platform';
import { HubError } from '../../src/core/net/signedFetch';
import { setPhase } from '../../src/core/store/slices/session';
import { setPlatform } from '../../src/core/store/slices/orgs';
import { KIND, OPERATOR_ROLE } from '../../src/core/subscriptions/subscriptions';
import { routes } from '../../src/ui/screens/subscriptions/routes';
import { SubscriptionsScreen } from '../../src/ui/screens/subscriptions/SubscriptionsScreen';
import { section, SubscriptionsLink } from '../../src/ui/settings/sections/subscriptions';
import { needsUnlock } from '../../src/ui/shell/guard';
import { SubscriptionsCard } from '../../src/ui/subscriptions/SubscriptionsCard';
import { REFRESH_MS } from '../../src/ui/subscriptions/useSubscriptions';
import { credWorld, type CredWorld, storedRecord, SUBSCRIPTION } from '../core/credentials/world';
import { seed, T0 } from '../core/log/world';
import { mount } from './chat-world';

const NOW = T0 + 10 * 60_000;
const iso = (at: number) => new Date(at).toISOString();
const FRESH = iso(NOW - 5 * 60_000);
const OLD = iso(NOW - 2 * 3600_000);
const DEVICE = '0190f000-0000-7000-8000-0000000000d1';
const LIST = 'GET /v1/me/subscriptions';

const id = (n: number) => `0190f000-0000-7000-8000-0000000005${String(n).padStart(2, '0')}`;

const win = (name: string, used: number, resets: number | null) => ({
  window: name,
  used_percent: used,
  resets_at: resets === null ? null : iso(resets),
  severity: null,
});

const measured = (windows: unknown[], overrides: Record<string, unknown> = {}) => ({
  fetched_at: FRESH,
  device_id: DEVICE,
  unreadable: false,
  windows,
  ...overrides,
});

const item = (n: number, overrides: Record<string, unknown> = {}) => ({
  id: id(n),
  owner_id: 'x',
  primary: false,
  version: 1,
  epoch: 1,
  created_at: FRESH,
  updated_at: FRESH,
  reading: null,
  ...overrides,
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

async function world(): Promise<CredWorld> {
  const t = await credWorld();
  setPhase(t.store, 'unlocked');
  return t;
}

/** A sealed record served at `GET /v1/subscriptions/{id}`. */
async function serveRecord(t: CredWorld, n: number, body: unknown, options: Parameters<typeof storedRecord>[3] = {}) {
  const record = await storedRecord(t, KIND, body, { id: id(n), ...options });
  t.client.on(`GET /v1/subscriptions/${id(n)}`, () => ({ owner_id: t.me.id, primary: false, record }));
}

/** The platform role `operator` with the operator subscriptions of the world's org granted. */
async function operator(t: CredWorld, subscriptions: unknown[]) {
  const root = await ed25519KeyPair(await seed('platform root'));
  const fresh = await appendPlatform(null, t.platform.entries, t.platform.root);
  const entries = await buildPlatformEntries(t.platform.state.headValue(), root.privateKey, [
    { type: 'role_grant', subject: t.me.id, role: OPERATOR_ROLE },
    { type: 'org_operator_subscriptions', org: t.org, granted: true },
  ]);
  setPlatform(t.store, await appendPlatform(fresh, entries, t.platform.root));
  t.client.on(`GET /v1/orgs/${t.org}/quotas`, () => ({
    own: { windows: [], unreadable: null, limited: null },
    operator: { granted: true, subscriptions },
    platform: { available: true },
    summary: { usable: 0, exhausted: 0, next_release: null },
  }));
}

const operatorSub = (label: string, used: number | null, fetched: string | null, state = 'usable') => ({
  label,
  state,
  readable: true,
  used_percent: used,
  resets_at: used === null ? null : iso(NOW + 3 * 3600_000),
  limited_at: null,
  until: null,
  fetched_at: fetched,
});

/** Every kind of row: own, further, not logged in, not readable, not measured, unverifiable, operator. */
async function fullWorld(): Promise<CredWorld> {
  const t = await world();
  t.client.on(LIST, () => ({
    subscriptions: [
      item(1, { reading: measured([win('seven_day', 95, NOW + 30_000)], { fetched_at: OLD }) }),
      item(2, {
        primary: true,
        reading: measured([
          win('five_hour', 20, NOW + 125 * 60_000),
          win('seven_day', 50, NOW + 27 * 3600_000),
          win('weekly:opus', 75, null),
        ]),
      }),
      item(3),
      item(4, { reading: measured([], { unreadable: true, device_id: null }) }),
      item(5, { reading: measured([win('five_hour', 72, NOW - 1000), win('seven_day', 10, NOW + 5 * 60_000)]) }),
      item(6),
      item(7, { updated_at: 'never' }),
    ],
  }));
  await serveRecord(t, 1, { ...SUBSCRIPTION, name: 'Team', note: 'night shifts', use: { pool: true, orgs: [t.org] } });
  t.client.on(`GET /v1/subscriptions/${id(2)}`, async () => ({
    owner_id: t.me.id,
    primary: true,
    record: await storedRecord(t, KIND, SUBSCRIPTION, { id: id(2), author: t.device }),
  }));
  await serveRecord(t, 3, { kind: 'setup_token', email: null, name: 'Spare', use: { pool: false, orgs: [] } });
  await serveRecord(t, 4, { ...SUBSCRIPTION, email: null, name: 'Beta', use: { pool: true, orgs: [] } });
  await serveRecord(t, 5, { ...SUBSCRIPTION, name: 'Gamma', use: { pool: false, orgs: [t.org] } });
  await serveRecord(t, 6, { ...SUBSCRIPTION, name: 'Delta' });
  t.client.on(`GET /v1/subscriptions/${id(7)}`, () => {
    throw new HubError('forbidden', 403);
  });
  await operator(t, [operatorSub('team-a', 40, FRESH), operatorSub('team-b', null, null, 'later')]);
  return t;
}

const cells = (n: number | string) =>
  within(screen.getByTestId('subscriptions.table'))
    .getAllByTestId('subscriptions.row')
    .find((row) => row.dataset.key === (typeof n === 'number' ? id(n) : n))!
    .querySelectorAll('td');

const account = (key: string) =>
  screen.getAllByTestId('subscriptions.account').find((found) => found.dataset.key === key)!;

const toggle = (details: HTMLElement, open: boolean) => {
  (details as HTMLDetailsElement).open = open;
  fireEvent(details, new Event('toggle'));
};

beforeEach(() => {
  vi.useFakeTimers({ now: NOW, toFake: ['Date'] });
});

afterEach(() => {
  vi.useRealTimers();
});

describe('the card of the home page', () => {
  it('is hidden while loading, then shows the way to add a subscription', async () => {
    const t = await world();
    const answer = deferred<unknown>();
    t.client.on(LIST, () => answer.promise);
    const { router } = mount(t, [{ path: '/', element: <SubscriptionsCard /> }], '/');
    expect(screen.queryByTestId('subscriptions.card')).toBeNull();
    answer.resolve({ subscriptions: [] });
    expect(await screen.findByTestId('subscriptions.card.empty')).toHaveTextContent(
      'No subscription yet. Add one and the platform logs it in and measures it for you.Add subscription',
    );
    expect(screen.getByTestId('subscriptions.card')).toHaveTextContent('My subscriptions');
    expect(screen.getByTestId('subscriptions.card.add')).toHaveAttribute('href', '/subscriptions');
    fireEvent.click(screen.getByTestId('subscriptions.card.all'));
    expect(router.state.location.pathname).toBe('/subscriptions');
  });

  it('stays hidden when the hub cannot answer', async () => {
    const t = await world();
    t.client.on(LIST, () => {
      throw new HubError('not_found', 404);
    });
    mount(t, [{ path: '/', element: <SubscriptionsCard /> }], '/');
    await waitFor(() => expect(t.client.of(LIST)).toHaveLength(1));
    await act(async () => undefined);
    expect(screen.queryByTestId('subscriptions.card')).toBeNull();
  });

  it('shows the provider row with the week as the total of all accounts and opens it', async () => {
    const t = await fullWorld();
    mount(t, [{ path: '/', element: <SubscriptionsCard /> }], '/');
    const provider = await screen.findByTestId('subscriptions.provider');
    expect(provider).toHaveAttribute('data-provider', 'claude');
    expect(within(provider).getByTestId('subscriptions.provider.summary')).toHaveTextContent('Claude');
    // seven records and two token files: the line stands for all of them
    expect(within(provider).getByTestId('subscriptions.provider.total')).toHaveTextContent('all 9');
    // Team, own sessions, Gamma and team-a are below 100 % in their binding window; the others are not measured
    expect(within(provider).getByTestId('subscriptions.provider.usable')).toHaveTextContent('usable now: 4 of 9');
    // the week of the three accounts that measured it: (95 + 50 + 10) / 3, the next reset of them
    expect(within(provider).getByTestId('subscriptions.provider.used')).toHaveTextContent('51.7%');
    expect(within(provider).getByTestId('subscriptions.provider.reset')).toHaveTextContent('<1 min');
    expect(within(provider).getByTestId('subscriptions.provider.bar')).toHaveAttribute('data-level', 'ok');
    expect(within(provider).getByTestId('subscriptions.provider.bar')).toHaveAttribute('aria-label', 'Claude – all 9');
    expect(screen.queryByTestId('subscriptions.provider.details')).toBeNull();
    toggle(provider, true);
    const details = await screen.findByTestId('subscriptions.provider.details');
    expect(within(details).getByTestId('subscriptions.window.five_hour')).toHaveTextContent('5hdue46%');
    expect(within(details).getByTestId('subscriptions.window.five_hour.bar')).toHaveAttribute('data-level', 'ok');
    expect(within(details).getByTestId('subscriptions.window.seven_day')).toHaveTextContent('Week<1 min51.7%');
    expect(within(details).getByTestId('subscriptions.window.weekly:opus')).toHaveTextContent('Week opus75%');
    expect(within(details).getByTestId('subscriptions.window.fullest')).toHaveTextContent('fullest window3 h 0 min40%');
    expect(within(details).getByTestId('subscriptions.provider.recorded')).toHaveTextContent('measured 5 min ago');
    toggle(provider, false);
    await waitFor(() => expect(screen.queryByTestId('subscriptions.provider.details')).toBeNull());
  });

  it('says "not measured yet" while no account was read and "no data" when a read has no window', async () => {
    const t = await world();
    t.client.on(LIST, () => ({ subscriptions: [item(1)] }));
    await serveRecord(t, 1, SUBSCRIPTION);
    mount(t, [{ path: '/', element: <SubscriptionsCard /> }], '/');
    expect(await screen.findByTestId('subscriptions.provider.none')).toHaveTextContent('not measured yet');
    cleanup();
    t.client.on(LIST, () => ({ subscriptions: [item(1, { reading: measured([], { unreadable: true }) })] }));
    mount(t, [{ path: '/', element: <SubscriptionsCard /> }], '/');
    expect(await screen.findByTestId('subscriptions.provider.none')).toHaveTextContent('no data');
    toggle(screen.getByTestId('subscriptions.provider'), true);
    expect(await screen.findByTestId('subscriptions.account.none')).toHaveTextContent('not readable');
  });
});

describe('the review of KOR-68', () => {
  it('marks an old headline on the closed line, names the source of every reading and caps the meter value', async () => {
    const t = await world();
    t.client.on(LIST, () => ({
      subscriptions: [
        item(1, { reading: measured([win('seven_day', 130, NOW + 3600_000)], { fetched_at: OLD }) }),
        item(2, { reading: measured([win('seven_day', 90, null)], { device_id: null }) }),
      ],
    }));
    await serveRecord(t, 1, { ...SUBSCRIPTION, name: 'One' });
    await serveRecord(t, 2, { ...SUBSCRIPTION, name: 'Two' });
    await operator(t, [operatorSub('file', 10, FRESH)]);
    mount(t, [{ path: '/', element: <SubscriptionsCard /> }], '/');
    const provider = await screen.findByTestId('subscriptions.provider');
    // the total counts an old reading: the line is marked old; (130 + 90) / 2 = 110 % is capped
    expect(within(provider).getByTestId('subscriptions.provider.old')).toHaveTextContent('old (2 h ago)');
    expect(within(provider).getByTestId('subscriptions.provider.used')).toHaveTextContent('110%');
    expect(within(provider).getByTestId('subscriptions.provider.bar')).toHaveAttribute('aria-valuenow', '100');
    toggle(provider, true);
    const ages = (await screen.findAllByTestId('subscriptions.account.age')).map((found) => found.textContent);
    expect(ages).toEqual([
      'measured 2 h ago · platform device',
      'measured 5 min ago · platform device',
      'measured 5 min ago · token file',
    ]);
    cleanup();
    // fresh readings: no mark
    t.client.on(LIST, () => ({ subscriptions: [item(2, { reading: measured([win('seven_day', 20, null)]) })] }));
    mount(t, [{ path: '/', element: <SubscriptionsCard /> }], '/');
    await screen.findByTestId('subscriptions.provider.used');
    expect(screen.queryByTestId('subscriptions.provider.old')).toBeNull();
  });
});

describe('the binding window of an account', () => {
  it('shows an account whose 5 hours are used up as used up, not by its week', async () => {
    const t = await world();
    t.client.on(LIST, () => ({
      subscriptions: [
        item(1, {
          reading: measured([win('five_hour', 100, NOW + 90 * 60_000), win('seven_day', 13, NOW + 4 * 86400_000)]),
        }),
        item(2, {
          reading: measured([win('five_hour', 30, NOW + 60 * 60_000), win('seven_day', 100, NOW + 2 * 86400_000)]),
        }),
      ],
    }));
    await serveRecord(t, 1, { ...SUBSCRIPTION, name: 'Nora' });
    await serveRecord(t, 2, { ...SUBSCRIPTION, name: 'Otto' });
    mount(t, [{ path: '/', element: <SubscriptionsCard /> }], '/');
    const provider = await screen.findByTestId('subscriptions.provider');
    // neither can be used now; the provider line stays with the mean of the week ((13 + 100) / 2)
    expect(within(provider).getByTestId('subscriptions.provider.usable')).toHaveTextContent('usable now: 0 of 2');
    expect(within(provider).getByTestId('subscriptions.provider.usable')).toHaveAttribute('data-usable', '0');
    expect(within(provider).getByTestId('subscriptions.provider.used')).toHaveTextContent('56.5%');
    toggle(provider, true);
    await screen.findByTestId('subscriptions.provider.details');
    const nora = account(id(1));
    expect(within(nora).getByTestId('subscriptions.account.bar')).toHaveAttribute('data-level', 'low');
    expect(within(nora).getByTestId('subscriptions.account.bar')).toHaveAttribute('data-window', 'five_hour');
    expect(within(nora).getByTestId('subscriptions.account.bar')).toHaveAttribute('aria-label', 'Nora – 5h');
    expect(within(nora).getByTestId('subscriptions.account.used')).toHaveTextContent('100%');
    expect(within(nora).getByTestId('subscriptions.account.window')).toHaveTextContent('5h');
    expect(within(nora).getByTestId('subscriptions.account.reset')).toHaveTextContent('1 h 30 min');
    expect(within(nora).getByTestId('subscriptions.account.other')).toHaveTextContent('Week: 13%');
    const otto = account(id(2));
    expect(within(otto).getByTestId('subscriptions.account.bar')).toHaveAttribute('data-window', 'seven_day');
    expect(within(otto).getByTestId('subscriptions.account.window')).toHaveTextContent('Week');
    expect(within(otto).getByTestId('subscriptions.account.reset')).toHaveTextContent('2 d 0 h');
    expect(within(otto).getByTestId('subscriptions.account.other')).toHaveTextContent('5h: 30%');
    cleanup();
    // in German
    mount(t, [{ path: '/', element: <SubscriptionsCard /> }], '/', 'de');
    const de = await screen.findByTestId('subscriptions.provider');
    expect(within(de).getByTestId('subscriptions.provider.usable')).toHaveTextContent('jetzt nutzbar: 0 von 2');
    toggle(de, true);
    await screen.findByTestId('subscriptions.provider.details');
    expect(within(account(id(1))).getByTestId('subscriptions.account.window')).toHaveTextContent('5 h');
    expect(within(account(id(1))).getByTestId('subscriptions.account.other')).toHaveTextContent('Woche: 13%');
  });
});

describe('the refresh', () => {
  it('reads again every minute and stops when the card is gone', async () => {
    vi.useRealTimers();
    vi.useFakeTimers({ now: NOW, toFake: ['Date', 'setInterval', 'clearInterval'] });
    const t = await world();
    t.client.on(LIST, () => ({ subscriptions: [] }));
    mount(t, [{ path: '/', element: <SubscriptionsCard /> }], '/');
    expect(await screen.findByTestId('subscriptions.card')).toBeVisible();
    act(() => vi.advanceTimersByTime(REFRESH_MS));
    await waitFor(() => expect(t.client.of(LIST)).toHaveLength(2));
    cleanup();
    act(() => vi.advanceTimersByTime(REFRESH_MS));
    expect(t.client.of(LIST)).toHaveLength(2);
  });

  it('drops an answer or a failure that arrives after the card is gone', async () => {
    const t = await world();
    const answers = [deferred<unknown>(), deferred<unknown>()];
    let next = 0;
    t.client.on(LIST, () => answers[next++]!.promise);
    const errors = vi.spyOn(console, 'error');
    mount(t, [{ path: '/', element: <SubscriptionsCard /> }], '/');
    await waitFor(() => expect(t.client.of(LIST)).toHaveLength(1));
    cleanup();
    mount(t, [{ path: '/', element: <SubscriptionsCard /> }], '/');
    await waitFor(() => expect(t.client.of(LIST)).toHaveLength(2));
    cleanup();
    await act(async () => {
      answers[0]!.resolve({ subscriptions: [] });
      answers[1]!.reject(new HubError('unavailable', 503));
      await Promise.resolve();
    });
    expect(screen.queryByTestId('subscriptions.card')).toBeNull();
    expect(errors).not.toHaveBeenCalled();
  });
});

describe('the page', () => {
  it('is reached through its route', async () => {
    const t = await world();
    t.client.on(LIST, () => ({ subscriptions: [] }));
    mount(t, routes as never, '/subscriptions');
    expect(await screen.findByTestId('subscriptions.page')).toHaveTextContent('My subscriptions');
  });

  it('shows the loading state, then the empty state with the hint to add one', async () => {
    const t = await world();
    const answer = deferred<unknown>();
    t.client.on(LIST, () => answer.promise);
    mount(t, [{ path: '/subscriptions', element: <SubscriptionsScreen /> }], '/subscriptions');
    expect(screen.getByTestId('subscriptions.loading')).toHaveTextContent('Loading the subscriptions…');
    expect(screen.queryByTestId('subscriptions.add')).toBeNull();
    answer.resolve({ subscriptions: [] });
    expect(await screen.findByTestId('subscriptions.empty')).toBeVisible();
    expect(screen.queryByTestId('subscriptions.loading')).toBeNull();
    expect(screen.queryByTestId('subscriptions.table')).toBeNull();
    // "Add subscription" opens the login dialog of KOR-69 and closes again
    fireEvent.click(screen.getByTestId('subscriptions.add'));
    expect(screen.getByTestId('subadd')).toBeVisible();
    expect(screen.queryByTestId('subscriptions.add')).toBeNull();
    fireEvent.click(screen.getByTestId('subadd.close'));
    expect(screen.getByTestId('subscriptions.add')).toBeVisible();
  });

  it('reports a failed load and loads again on "Reload"', async () => {
    const t = await world();
    t.client.on(LIST, () => {
      throw new HubError('unavailable', 503);
    });
    mount(t, [{ path: '/subscriptions', element: <SubscriptionsScreen /> }], '/subscriptions');
    const failed = await screen.findByTestId('subscriptions.load-failed');
    expect(failed).toHaveAttribute('data-code', 'unavailable');
    expect(failed).toHaveTextContent('The subscriptions could not be loaded');
    expect(screen.queryByTestId('subscriptions.loading')).toBeNull();
    t.client.on(LIST, () => ({ subscriptions: [] }));
    fireEvent.click(screen.getByTestId('subscriptions.reload'));
    expect(await screen.findByTestId('subscriptions.empty')).toBeVisible();
    expect(screen.queryByTestId('subscriptions.load-failed')).toBeNull();
    expect(t.client.of(LIST)).toHaveLength(2);
  });

  it('sends a locked user to the sign-in and back', async () => {
    const t = await world();
    setPhase(t.store, 'locked');
    t.client.on(LIST, () => ({ subscriptions: [] }));
    const { router } = mount(t, [{ path: '/subscriptions', element: <SubscriptionsScreen /> }], '/subscriptions');
    await waitFor(() => expect(router.state.location.pathname).toBe('/login'));
    expect(router.state.location.search).toBe('?next=%2Fsubscriptions');
    expect(screen.queryByTestId('subscriptions.page')).toBeNull();
  });

  it('shows every provider and account, "detailed" and "sort by used"', async () => {
    const t = await fullWorld();
    mount(t, [{ path: '/subscriptions', element: <SubscriptionsScreen /> }], '/subscriptions');
    const provider = await screen.findByTestId('subscriptions.provider');
    expect(screen.queryByTestId('subscriptions.provider.details')).toBeNull();
    fireEvent.click(screen.getByTestId('subscriptions.detailed'));
    expect(screen.getByTestId('subscriptions.detailed')).toBeChecked();
    expect(screen.getByTestId('subscriptions.provider.details')).toBeVisible();
    // while "detailed" opens every provider, closing one changes nothing
    toggle(provider, false);
    expect(screen.getByTestId('subscriptions.provider.details')).toBeInTheDocument();
    expect(screen.getAllByTestId('subscriptions.account').map((found) => found.dataset.key)).toEqual([
      id(2),
      id(7),
      id(4),
      id(6),
      id(5),
      id(3),
      id(1),
      'operator:team-a',
      'operator:team-b',
    ]);
    const own = account(id(2));
    expect(own).toHaveAttribute('data-status', 'logged_in');
    expect(own).toHaveTextContent('own sessions claude@example.com');
    // the week binds (50 % against 20 % of the 5 hours); the 5 hours stay as the second value
    expect(within(own).getByTestId('subscriptions.account.used')).toHaveTextContent('50%');
    expect(within(own).getByTestId('subscriptions.account.window')).toHaveTextContent('Week');
    expect(within(own).getByTestId('subscriptions.account.reset')).toHaveTextContent('1 d 3 h');
    expect(within(own).getByTestId('subscriptions.account.other')).toHaveTextContent('5h: 20%');
    expect(within(own).getByTestId('subscriptions.account.bar')).toHaveAttribute('data-level', 'ok');
    expect(within(own).getByTestId('subscriptions.account.bar')).toHaveAttribute('data-window', 'seven_day');
    // the 5 hours bind (72 % against 10 % of the week): amber, with their own reset
    const gamma = account(id(5));
    expect(within(gamma).getByTestId('subscriptions.account.used')).toHaveTextContent('72%');
    expect(within(gamma).getByTestId('subscriptions.account.window')).toHaveTextContent('5h');
    expect(within(gamma).getByTestId('subscriptions.account.reset')).toHaveTextContent('due');
    expect(within(gamma).getByTestId('subscriptions.account.bar')).toHaveAttribute('data-level', 'warn');
    expect(within(gamma).getByTestId('subscriptions.account.other')).toHaveTextContent('Week: 10%');
    // only the week: no second value
    expect(within(account(id(1))).queryByTestId('subscriptions.account.other')).toBeNull();
    expect(within(account('operator:team-a')).getByTestId('subscriptions.account.window')).toHaveTextContent(
      'fullest window',
    );
    expect(within(own).getByTestId('subscriptions.account.age')).toHaveAttribute('data-old', 'no');
    expect(within(own).getByTestId('subscriptions.account.age')).toHaveTextContent('measured 5 min ago');
    expect(within(account(id(1))).getByTestId('subscriptions.account.age')).toHaveAttribute('data-old', 'yes');
    expect(within(account(id(1))).getByTestId('subscriptions.account.age')).toHaveTextContent('measured 2 h ago');
    expect(within(account(id(3))).getByTestId('subscriptions.account.none')).toHaveTextContent('not logged in');
    expect(within(account(id(3))).queryByTestId('subscriptions.account.age')).toBeNull();
    expect(account(id(3))).not.toHaveTextContent('@');
    expect(within(account(id(6))).getByTestId('subscriptions.account.none')).toHaveTextContent('not measured yet');
    expect(within(account(id(7))).getByTestId('subscriptions.account.none')).toHaveTextContent(
      'cannot be opened (forbidden)',
    );
    expect(account(id(7))).toHaveTextContent('subscription');
    expect(within(account('operator:team-a')).getByTestId('subscriptions.account.used')).toHaveTextContent('40%');
    expect(within(account('operator:team-b')).getByTestId('subscriptions.account.none')).toHaveTextContent(
      'not measured yet',
    );
    fireEvent.click(screen.getByTestId('subscriptions.sort'));
    expect(screen.getByTestId('subscriptions.sort')).toBeChecked();
    expect(screen.getAllByTestId('subscriptions.provider')).toHaveLength(1);
    // without "detailed" the provider is closed again, the user's own choice opens it
    fireEvent.click(screen.getByTestId('subscriptions.detailed'));
    expect(screen.queryByTestId('subscriptions.provider.details')).toBeNull();
    toggle(provider, true);
    expect(await screen.findByTestId('subscriptions.provider.details')).toBeInTheDocument();
  });

  it('shows the table with every column and the login hint of a subscription that is not logged in', async () => {
    const t = await fullWorld();
    mount(t, [{ path: '/subscriptions', element: <SubscriptionsScreen /> }], '/subscriptions');
    const table = await screen.findByTestId('subscriptions.table');
    expect(
      within(table)
        .getAllByRole('columnheader')
        .map((cell) => cell.textContent),
    ).toEqual([
      'Provider',
      'Account',
      'Name',
      'Status',
      'Note',
      'Use',
      'Listed',
      'Plan',
      'Used',
      'Reset',
      'Device',
      'Checked',
      'Changed',
    ]);
    const text = (n: number | string) => [...cells(n)].map((cell) => cell.textContent);
    expect(text(2)).toEqual([
      'Claude',
      'claude@example.com',
      'own sessions',
      'logged in',
      '—',
      'own sessions',
      'listed',
      '—',
      '5h: 20%Week: 50%Week opus: 75%',
      '5h: 2 h 5 minWeek: 1 d 3 hWeek opus: —',
      'platform device',
      '5 min ago',
      '5 min ago',
    ]);
    expect(text(1)).toEqual([
      'Claude',
      'claude@example.com',
      'Team',
      'logged in',
      'night shifts',
      'pool, 1 org(s)',
      'listed',
      '—',
      'Week: 95%',
      'Week: <1 min',
      'platform device',
      '2 h ago',
      '5 min ago',
    ]);
    expect(text(4).slice(2, 12)).toEqual([
      'Beta',
      'not readable',
      '—',
      'pool',
      'listed',
      '—',
      '—',
      '—',
      'platform device',
      '5 min ago',
    ]);
    expect(text(4)[1]).toBe('—');
    expect(text(5)[5]).toBe('1 org(s)');
    expect(text(5)[9]).toBe('5h: dueWeek: 5 min');
    expect(text(7)).toEqual([
      'Claude',
      '—',
      'subscription',
      'cannot be opened (forbidden)',
      '—',
      '—',
      'listed',
      '—',
      '—',
      '—',
      '—',
      '—',
      '—',
    ]);
    expect(text('operator:team-a').slice(2, 13)).toEqual([
      'team-a',
      'measured',
      '—',
      'pool',
      'token file',
      'usable',
      'fullest window: 40%',
      'fullest window: 3 h 0 min',
      'token file',
      '5 min ago',
      '—',
    ]);
    expect(text('operator:team-b')[7]).toBe('later');
    // "Log in now" only for a subscription without token, with the agent tool's command
    expect(within(table).getAllByTestId('subscriptions.row.login')).toHaveLength(1);
    const spare = cells(3);
    expect(spare[5]).toHaveTextContent('measure only');
    const login = within(spare[3]!).getByTestId('subscriptions.row.login');
    expect(spare[3]).toHaveTextContent('not logged inLog in now');
    // "Log in now" opens the login of KOR-69 for this record; without a capable device it ends with a message
    fireEvent.click(login);
    expect(await within(spare[3]!).findByTestId('sublogin.flow')).toBeVisible();
    expect(within(spare[3]!).queryByTestId('subscriptions.row.login')).toBeNull();
    fireEvent.click(within(spare[3]!).getByTestId('sublogin.close'));
    expect(within(spare[3]!).getByTestId('subscriptions.row.login')).toBeVisible();
  });

  it('shows the page in German', async () => {
    const t = await world();
    t.client.on(LIST, () => ({ subscriptions: [] }));
    mount(t, [{ path: '/subscriptions', element: <SubscriptionsScreen /> }], '/subscriptions', 'de');
    expect(await screen.findByTestId('subscriptions.empty')).toBeVisible();
    expect(screen.getByTestId('subscriptions.page')).not.toHaveTextContent('subscriptions.');
  });
});

describe('the settings link and the guard', () => {
  it('registers the section and leads to the page', async () => {
    expect(section).toMatchObject({ id: 'subscriptions', titleKey: 'subscriptions.title' });
    const t = await world();
    const { router } = mount(t, [{ path: '/settings', element: <SubscriptionsLink /> }], '/settings');
    const link = screen.getByTestId('settings.subscriptions.link');
    expect(link).toHaveTextContent('Open “My subscriptions”');
    fireEvent.click(link);
    expect(router.state.location.pathname).toBe('/subscriptions');
  });

  it('needs an unlocked identity for the page', () => {
    expect(needsUnlock('/subscriptions')).toBe(true);
    expect(needsUnlock('/subscriptions/x')).toBe(true);
    expect(needsUnlock('/subscriptionsx')).toBe(false);
  });
});
