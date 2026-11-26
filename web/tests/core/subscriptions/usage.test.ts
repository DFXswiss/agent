// U-WEB-subscriptions (KOR-75, ADR-100): "Who used how much" – the answer of `GET /v1/me/subscriptions/usage` checked
// subscription by subscription and person by person (a malformed one left out, nothing twice), the request target of
// each period, the span of chosen days and the short token numbers.
import { describe, expect, it } from 'vitest';
import { HubError } from '../../../src/core/net/signedFetch';
import {
  dayOf,
  dayStart,
  formatTokens,
  loadUsage,
  MAX_DAYS,
  parseUsage,
  rangeOf,
  usageTarget,
} from '../../../src/core/subscriptions/usage';
import type { RequestsContext } from '../../../src/core/requests/client';

const SUB = '0190f000-0000-7000-8000-000000000501';
const OTHER = '0190f000-0000-7000-8000-000000000502';
const ME = '0190f000-0000-7000-8000-0000000000a1';
const ADA = '0190f000-0000-7000-8000-0000000000a2';

const person = (user: string, overrides: Record<string, unknown> = {}) => ({
  user_id: user,
  requests: 2,
  input_tokens: 100,
  output_tokens: 20,
  cache_read_tokens: 1000,
  cache_write_tokens: 30,
  tokens: 1150,
  sessions: 1,
  token_share: 0.5,
  estimated_percent: 20,
  ...overrides,
});

const { user_id: _user, token_share: _share, estimated_percent: _estimated, ...TOTAL } = person(ME);

const entry = (overrides: Record<string, unknown> = {}) => ({
  subscription_id: SUB,
  owner_id: ME,
  primary: false,
  own: true,
  since: '2026-11-26T10:00:00.000Z',
  until: '2026-11-26T13:00:00.000Z',
  measured: { used_percent: 40, resets_at: '2026-11-26T15:00:00.000Z', fetched_at: '2026-11-26T12:55:00.000Z' },
  total: TOTAL,
  people: [person(ME), person(ADA, { estimated_percent: null })],
  ...overrides,
});

describe('parseUsage', () => {
  it('reads every subscription and person', () => {
    const found = parseUsage({ period: 'five_hour', subscriptions: [entry()] });
    expect(found.period).toBe('five_hour');
    const [first] = found.subscriptions;
    expect(first).toMatchObject({
      subscriptionId: SUB,
      ownerId: ME,
      primary: false,
      own: true,
      since: Date.parse('2026-11-26T10:00:00.000Z'),
      measured: { usedPercent: 40, resetsAt: Date.parse('2026-11-26T15:00:00.000Z') },
      total: { requests: 2, inputTokens: 100, outputTokens: 20, cacheReadTokens: 1000, cacheWriteTokens: 30 },
    });
    expect(first!.people.map((p) => [p.userId, p.tokenShare, p.estimatedPercent, p.tokens, p.sessions])).toEqual([
      [ME, 0.5, 20, 1150, 1],
      [ADA, 0.5, null, 1150, 1],
    ]);
  });

  it('leaves out what is malformed and never shows anything twice', () => {
    const found = parseUsage({
      period: 'week',
      subscriptions: [
        entry({
          people: [
            person(ME),
            person(ME),
            person(ADA, { token_share: 2 }),
            person(ADA, { estimated_percent: 'a' }),
            person(ADA, { tokens: -1 }),
            { user_id: 1 },
            'x',
          ],
          measured: { used_percent: 'x' },
          total: { requests: 1 },
        }),
        entry(),
        entry({ subscription_id: OTHER, own: false, total: TOTAL, measured: null, primary: true }),
        entry({ subscription_id: 3 }),
        entry({ since: 'never' }),
        entry({ own: 'yes' }),
        entry({ people: null }),
        null,
      ],
    });
    expect(found.subscriptions.map((s) => s.subscriptionId)).toEqual([SUB, OTHER]);
    const [first, second] = found.subscriptions;
    expect(first!.people.map((p) => p.userId)).toEqual([ME]);
    expect(first!.measured).toBeNull();
    expect(first!.total).toBeNull();
    // another owner's subscription never carries a total
    expect([second!.own, second!.total, second!.primary]).toEqual([false, null, true]);
  });

  it('refuses a malformed answer', () => {
    for (const body of [null, {}, { period: 'month', subscriptions: [] }, { period: 'week', subscriptions: {} }]) {
      expect(() => parseUsage(body)).toThrow(HubError);
    }
  });
});

describe('periods and days', () => {
  it('builds the request target of each period', () => {
    expect(usageTarget('five_hour')).toBe('/v1/me/subscriptions/usage?period=five_hour');
    expect(usageTarget('week')).toBe('/v1/me/subscriptions/usage?period=week');
    const since = Date.parse('2026-11-01T00:00:00.000Z');
    const until = Date.parse('2026-11-08T00:00:00.000Z');
    expect(usageTarget('range', { since, until })).toBe(
      '/v1/me/subscriptions/usage?period=range&since=2026-11-01T00%3A00%3A00.000Z&until=2026-11-08T00%3A00%3A00.000Z',
    );
    expect(usageTarget('range')).toBe('/v1/me/subscriptions/usage?period=range');
  });

  it('turns chosen days into a span of local midnights', () => {
    const since = dayStart('2026-11-01')!;
    expect(new Date(since).getHours()).toBe(0);
    expect(dayOf(since)).toBe('2026-11-01');
    const span = rangeOf('2026-11-01', '2026-11-07')!;
    expect(span.since).toBe(since);
    expect(dayOf(span.until)).toBe('2026-11-08');
    expect(rangeOf('2026-11-01', '2026-11-01')).not.toBeNull(); // one day
    expect(rangeOf('2026-11-07', '2026-11-01')).toBeNull(); // backwards
    expect(rangeOf('2026-01-01', '2026-11-01')).toBeNull(); // longer than MAX_DAYS
    expect(rangeOf('x', '2026-11-01')).toBeNull();
    expect(rangeOf('2026-11-01', '')).toBeNull();
    expect(dayStart('2026-02-30')).toBeNull();
    expect(MAX_DAYS).toBe(92);
  });

  it('writes token counts short', () => {
    expect(formatTokens(950, 'en')).toBe('950');
    expect(formatTokens(12_345, 'en')).toBe('12.3K');
    expect(formatTokens(4_100_000, 'en')).toBe('4.1M');
  });

  it('loads through the client', async () => {
    const calls: string[] = [];
    const context = {
      client: {
        request: async (_method: string, target: string) => {
          calls.push(target);
          return { period: 'week', subscriptions: [] };
        },
      },
    } as unknown as RequestsContext;
    expect(await loadUsage(context, 'week')).toEqual({ period: 'week', subscriptions: [] });
    expect(calls).toEqual(['/v1/me/subscriptions/usage?period=week']);
  });
});
