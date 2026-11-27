// U-WEB-ui-subscriptions (KOR-75, ADR-102): the overview "Who used how much" of the page `/subscriptions` – the people
// of each own subscription with tokens, sessions, share of the tokens and the estimated share of the quota (always
// marked as estimated), the total, another person's subscription with the own row only, the 5-hour window, the week
// and a chosen span (an invalid span is refused before any request), loading, failure and reload, the empty state, and
// the usage through DFX access per person with the link to "Credit and usage". The data come from the fake hub routes.
import { act, cleanup, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { HubError } from '../../src/core/net/signedFetch';
import { setPhase, setActiveOrg } from '../../src/core/store/slices/session';
import { KIND } from '../../src/core/subscriptions/subscriptions';
import { dayOf } from '../../src/core/subscriptions/usage';
import { SubscriptionsScreen } from '../../src/ui/screens/subscriptions/SubscriptionsScreen';
import { REFRESH_MS } from '../../src/ui/subscriptions/useSubscriptions';
import { BILLING } from '../core/billing/world';
import { credWorld, type CredWorld, storedRecord, SUBSCRIPTION } from '../core/credentials/world';
import { T0 } from '../core/log/world';
import { mount } from './chat-world';

const NOW = T0 + 10 * 60_000;
const iso = (at: number) => new Date(at).toISOString();
const USAGE = 'GET /v1/me/subscriptions/usage';
const SUB = '0190f000-0000-7000-8000-000000000501';
const OTHER = '0190f000-0000-7000-8000-000000000502';
const ADA = '0190f000-0000-7000-8000-0000000000a2';
const OWNER = '0190f000-0000-7000-8000-0000000000a3';

const person = (user: string, tokens: number, share: number, estimated: number | null) => ({
  user_id: user,
  requests: 3,
  input_tokens: tokens,
  output_tokens: 0,
  cache_read_tokens: 0,
  cache_write_tokens: 0,
  tokens,
  sessions: 2,
  token_share: share,
  estimated_percent: estimated,
});

function entry(t: CredWorld, overrides: Record<string, unknown> = {}) {
  return {
    subscription_id: SUB,
    owner_id: t.me.id,
    primary: false,
    own: true,
    since: iso(NOW - 3 * 3600_000),
    until: iso(NOW),
    measured: { used_percent: 40, resets_at: iso(NOW + 2 * 3600_000), fetched_at: iso(NOW - 5 * 60_000) },
    total: { requests: 6, input_tokens: 16_000, output_tokens: 0, cache_read_tokens: 0, cache_write_tokens: 0,
      tokens: 16_000, sessions: 3 }, // prettier-ignore
    people: [person(t.me.id, 12_000, 0.75, 30), person(ADA, 4_000, 0.25, 10)],
    ...overrides,
  };
}

async function world(): Promise<CredWorld> {
  const t = await credWorld();
  setPhase(t.store, 'unlocked');
  t.client.on('GET /v1/me/subscriptions', () => ({
    subscriptions: [
      {
        id: SUB,
        owner_id: t.me.id,
        primary: false,
        version: 1,
        epoch: 1,
        created_at: iso(NOW),
        updated_at: iso(NOW),
        reading: null,
      },
    ],
  }));
  const record = await storedRecord(t, KIND, { ...SUBSCRIPTION, name: 'Team' }, { id: SUB });
  t.client.on(`GET /v1/subscriptions/${SUB}`, () => ({ owner_id: t.me.id, primary: false, record }));
  return t;
}

const blocks = () => screen.getAllByTestId('subusage.subscription');

beforeEach(() => {
  vi.useFakeTimers({ now: NOW, toFake: ['Date'] });
});

afterEach(() => {
  vi.useRealTimers();
});

describe('who used how much', () => {
  it('shows every person of an own subscription with the estimated share and the total', async () => {
    const t = await world();
    const foreign = entry(t, {
      subscription_id: OTHER,
      owner_id: OWNER,
      own: false,
      primary: true,
      measured: null,
      total: null,
      people: [person(t.me.id, 950, 0.5, null)],
    });
    t.client.on(USAGE, () => ({ period: 'five_hour', subscriptions: [entry(t), foreign] }));
    mount(t, [{ path: '/subscriptions', element: <SubscriptionsScreen /> }], '/subscriptions');
    await waitFor(() => expect(blocks()).toHaveLength(2));
    expect(t.client.of(USAGE)[0]!.target).toBe('/v1/me/subscriptions/usage?period=five_hour');
    const [own, theirs] = blocks();
    expect(own).toHaveAttribute('data-own', 'yes');
    expect(within(own!).getByRole('heading', { level: 3 })).toHaveTextContent('Team');
    expect(within(own!).getByTestId('subusage.measured')).toHaveTextContent('Measured: 40% used (5 min ago)');
    const rows = within(own!).getAllByTestId('subusage.person');
    expect(rows.map((row) => row.dataset.user)).toEqual([t.me.id, ADA]);
    expect(rows[0]).toHaveTextContent('you');
    expect(within(rows[0]!).getByTestId('subusage.tokens')).toHaveTextContent('12K');
    expect(within(rows[0]!).getByTestId('subusage.estimated')).toHaveTextContent('≈ 30%');
    expect(rows[1]).toHaveTextContent('25%');
    expect(within(own!).getByTestId('subusage.total')).toHaveTextContent('Total16K');
    expect(within(own!).getByTestId('subusage.total')).toHaveTextContent('≈ 40%');
    expect(within(own!).getByRole('columnheader', { name: 'Share of the quota (estimated)' })).toBeVisible();
    expect(screen.getByTestId('subusage.estimate-note')).toHaveTextContent('an estimate');
    // another person's subscription: the own row only, no total, no estimate without a measurement
    expect(theirs).toHaveAttribute('data-own', 'no');
    expect(within(theirs!).getByRole('heading', { level: 3 })).toHaveTextContent('Subscription of');
    expect(theirs).toHaveTextContent('you see your own use only');
    expect(within(theirs!).getByTestId('subusage.measured')).toHaveTextContent('No measurement of this window');
    expect(within(theirs!).queryByTestId('subusage.total')).toBeNull();
    expect(within(theirs!).getByTestId('subusage.estimated')).toHaveTextContent('—');
  });

  it('switches to the week and to a chosen span, and refuses an invalid span', async () => {
    const t = await world();
    t.client.on(USAGE, (_body, target) => {
      const period = new URL(String(target), 'https://hub.example').searchParams.get('period');
      return { period, subscriptions: [entry(t, { people: [], measured: null })] };
    });
    mount(t, [{ path: '/subscriptions', element: <SubscriptionsScreen /> }], '/subscriptions');
    expect(await screen.findByTestId('subusage.none')).toHaveTextContent('No use in this period.');
    fireEvent.click(screen.getByTestId('subusage.period.week'));
    await waitFor(() => expect(t.client.of(USAGE).at(-1)!.target).toContain('period=week'));
    fireEvent.click(screen.getByTestId('subusage.period.range'));
    await waitFor(() => expect(t.client.of(USAGE).at(-1)!.target).toContain('period=range&since='));
    expect(screen.getByTestId('subusage.estimate-note')).toHaveTextContent('measures no quota for a chosen period');
    const calls = t.client.of(USAGE).length;
    fireEvent.change(screen.getByTestId('subusage.from'), { target: { value: '2025-01-01' } });
    expect(await screen.findByTestId('subusage.range-invalid')).toHaveTextContent('at most 92 days');
    expect(screen.queryByTestId('subusage.subscription')).toBeNull();
    expect(t.client.of(USAGE)).toHaveLength(calls);
    fireEvent.change(screen.getByTestId('subusage.from'), { target: { value: '2026-11-20' } });
    fireEvent.change(screen.getByTestId('subusage.to'), { target: { value: '2026-11-21' } });
    await waitFor(() => expect(screen.queryByTestId('subusage.range-invalid')).toBeNull());
    await waitFor(() => expect(t.client.of(USAGE).length).toBeGreaterThan(calls));
  });

  it('says when nobody used anything, reports a failure, reloads and refreshes', async () => {
    vi.useFakeTimers({ now: NOW, shouldAdvanceTime: true });
    const t = await world();
    let answer: () => unknown = () => {
      throw new HubError('not_found', 404);
    };
    t.client.on(USAGE, () => answer());
    mount(t, [{ path: '/subscriptions', element: <SubscriptionsScreen /> }], '/subscriptions');
    expect(await screen.findByTestId('subusage.load-failed')).toHaveAttribute('data-code', 'not_found');
    answer = () => ({ period: 'five_hour', subscriptions: [] });
    fireEvent.click(screen.getByTestId('subusage.reload'));
    expect(await screen.findByTestId('subusage.empty')).toHaveTextContent('Nobody used a subscription');
    const calls = t.client.of(USAGE).length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(REFRESH_MS);
    });
    expect(t.client.of(USAGE).length).toBeGreaterThan(calls);
    answer = () => ({ broken: true });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(REFRESH_MS);
    });
    expect(await screen.findByTestId('subusage.load-failed')).toHaveAttribute('data-code', 'unavailable');
  });

  it('shows the loading state and an unnamed own subscription', async () => {
    const t = await world();
    let release: (value: unknown) => void = () => undefined;
    t.client.on(USAGE, () => new Promise((resolve) => (release = resolve)));
    mount(t, [{ path: '/subscriptions', element: <SubscriptionsScreen /> }], '/subscriptions');
    expect(await screen.findByTestId('subusage.loading')).toHaveTextContent('Loading usage');
    const alone = entry(t, { subscription_id: OTHER, people: [person(t.me.id, 10, 1, 40)] });
    await act(async () => release({ period: 'five_hour', subscriptions: [alone] }));
    expect(within(blocks()[0]!).getByRole('heading', { level: 3 })).toHaveTextContent(/^subscription$/);
    // one person: no total line
    expect(screen.queryByTestId('subusage.total')).toBeNull();
  });

  it('lists the usage through DFX access per person and links to credit and usage', async () => {
    const t = await world();
    t.client.on(USAGE, () => ({ period: 'five_hour', subscriptions: [] }));
    const people = [{ user_id: t.me.id, tokens_micro_usd: 1_500_000, runtime_micro_usd: 0, tokens: 10, runtime_ms: 0 }];
    const route = `GET /v1/orgs/${t.org}/billing`;
    // a refused billing read leaves the link only
    let billing: () => unknown = () => {
      throw new HubError('forbidden', 403);
    };
    t.client.on(route, () => billing());
    const { router } = mount(t, [{ path: '/subscriptions', element: <SubscriptionsScreen /> }], '/subscriptions');
    expect(await screen.findByTestId('subusage.dfx')).toHaveTextContent('No organization chosen.');
    act(() => setActiveOrg(t.store, t.org));
    await waitFor(() => expect(t.client.of(route)).toHaveLength(1));
    await act(async () => undefined);
    expect(screen.queryByTestId('subusage.dfx.people')).toBeNull();
    billing = () => ({ ...BILLING, people });
    act(() => setActiveOrg(t.store, null));
    act(() => setActiveOrg(t.store, t.org));
    expect(await screen.findByTestId('subusage.dfx.people')).toHaveTextContent('$1.50');
    fireEvent.click(screen.getByTestId('subusage.dfx.link'));
    expect(router.state.location.pathname).toBe('/settings');
    expect(router.state.location.hash).toBe('#billing');
  });

  it('drops answers that arrive after the page was left', async () => {
    const t = await world();
    setActiveOrg(t.store, t.org);
    let usage: (value: unknown) => void = () => undefined;
    let refuse: (error: unknown) => void = () => undefined;
    let billing: (value: unknown) => void = () => undefined;
    t.client.on(USAGE, () => new Promise((resolve) => (usage = resolve)));
    t.client.on(`GET /v1/orgs/${t.org}/billing`, () => new Promise((resolve) => (billing = resolve)));
    mount(t, [{ path: '/subscriptions', element: <SubscriptionsScreen /> }], '/subscriptions');
    expect(await screen.findByTestId('subusage.loading')).toBeVisible();
    fireEvent.click(screen.getByTestId('subusage.period.week'));
    await waitFor(() => expect(t.client.of(USAGE)).toHaveLength(2));
    const first = usage;
    t.client.on(USAGE, () => new Promise((_resolve, reject) => (refuse = reject)));
    fireEvent.click(screen.getByTestId('subusage.period.five_hour'));
    await waitFor(() => expect(t.client.of(USAGE)).toHaveLength(3));
    cleanup();
    await act(async () => {
      first({ period: 'week', subscriptions: [] });
      refuse(new HubError('not_found', 404));
      billing({ ...BILLING, people: [] });
    });
    expect(screen.queryByTestId('subusage.section')).toBeNull();
  });

  it('marks the total of a chosen span without an estimate', async () => {
    const t = await world();
    t.client.on(USAGE, () => ({ period: 'range', subscriptions: [entry(t, { measured: null })] }));
    mount(t, [{ path: '/subscriptions', element: <SubscriptionsScreen /> }], '/subscriptions');
    const total = await screen.findByTestId('subusage.total');
    expect(total).toHaveTextContent('Total16K');
    expect(within(total).getAllByRole('cell').at(-1)).toHaveTextContent('—');
    // only the end of the span changes: a valid span is read
    fireEvent.click(screen.getByTestId('subusage.period.range'));
    const calls = t.client.of(USAGE).length;
    fireEvent.change(screen.getByTestId('subusage.to'), { target: { value: dayOf(NOW + 2 * 86_400_000) } });
    await waitFor(() => expect(t.client.of(USAGE).length).toBeGreaterThan(calls));
    expect(t.client.of(USAGE).at(-1)!.target).toContain('period=range');
  });
});
