// U-WEB-credentials (KOR-50, ADR-081 section 6): the model access of an organization from `GET /v1/orgs/{org}/quotas` –
// the own subscription with its windows or "not readable" and the last observed limit, the operator's subscriptions,
// DFX access and the summary; malformed rows left out, a malformed answer `unavailable`.
import { describe, expect, it } from 'vitest';
import {
  loadOrgQuotas,
  operatorSubscription,
  parseOrgQuotas,
  subscriptionShare,
} from '../../../src/core/credentials/orgQuotas';
import { credWorld } from './world';

const AT = '2026-11-24T13:05:00.000Z';
const LATER = '2026-11-24T14:05:00.000Z';
const ms = Date.parse;

const subscription = (overrides: Record<string, unknown> = {}) => ({
  label: 'team-a',
  state: 'exhausted',
  readable: true,
  used_percent: 100,
  resets_at: LATER,
  limited_at: AT,
  until: LATER,
  fetched_at: AT,
  ...overrides,
});

export const QUOTAS = {
  own: {
    windows: [
      {
        window: 'five_hour',
        used_percent: 100,
        resets_at: LATER,
        severity: 'critical',
        fetched_at: AT,
        device_id: '0190f000-0000-7000-8000-000000000001',
      },
      { window: 'monthly' },
    ],
    unreadable: null,
    limited: { at: AT, until: LATER, readable: true },
  },
  operator: { granted: true, subscriptions: [subscription(), subscription({ label: '' }), 'x'] },
  platform: { available: true },
  summary: { usable: 1, exhausted: 1, next_release: LATER },
};

const codeOf = (run: () => unknown) => {
  try {
    run();
  } catch (error) {
    return (error as { code: string }).code;
  }
  return 'none';
};

describe('operator subscriptions', () => {
  it('reads a subscription and leaves malformed ones out', () => {
    expect(operatorSubscription(subscription())).toEqual({
      label: 'team-a',
      state: 'exhausted',
      readable: true,
      usedPercent: 100,
      resetsAt: ms(LATER),
      limitedAt: ms(AT),
      until: ms(LATER),
      fetchedAt: ms(AT),
    });
    // not readable: no share, no times (also when the fields are absent)
    const unreadable = operatorSubscription({ label: 'b', state: 'usable', readable: false });
    expect(unreadable).toEqual({
      label: 'b',
      state: 'usable',
      readable: false,
      usedPercent: null,
      resetsAt: null,
      limitedAt: null,
      until: null,
      fetchedAt: null,
    });
    expect(subscriptionShare(unreadable!)).toBeNull();
    expect(subscriptionShare(operatorSubscription(subscription({ used_percent: 140 }))!)).toBe(1);
    expect(subscriptionShare(operatorSubscription(subscription({ used_percent: 40 }))!)).toBe(0.4);
    for (const broken of [
      'x',
      subscription({ label: 3 }),
      subscription({ label: '' }),
      subscription({ state: 1 }),
      subscription({ readable: 'yes' }),
      subscription({ used_percent: 'full' }),
      subscription({ used_percent: -1 }),
      subscription({ used_percent: Infinity }),
      subscription({ until: 'soon' }),
      subscription({ fetched_at: 5 }),
    ]) {
      expect(operatorSubscription(broken)).toBeNull();
    }
  });
});

describe('org quotas', () => {
  it('reads the own subscription, the operator subscriptions, DFX access and the summary', () => {
    const found = parseOrgQuotas(QUOTAS);
    expect(found.own.bars.map((bar) => [bar.window, bar.usedPercent])).toEqual([['five_hour', 100]]);
    expect(found.own).toMatchObject({
      unreadableSince: null,
      limited: { at: ms(AT), until: ms(LATER), readable: true },
    });
    expect(found.operator.granted).toBe(true);
    expect(found.operator.subscriptions.map((row) => row.label)).toEqual(['team-a']);
    expect(found.platformAvailable).toBe(true);
    expect(found.summary).toEqual({ usable: 1, exhausted: 1, nextRelease: ms(LATER) });
  });

  it('says "not readable" with the last observed limit and drops malformed limits', () => {
    const unreadable = {
      ...QUOTAS,
      own: { windows: null, unreadable: { since: AT }, limited: { at: AT, until: null, readable: false } },
      operator: { granted: false, subscriptions: null },
      summary: { usable: 0, exhausted: 0, next_release: null },
    };
    const found = parseOrgQuotas(unreadable);
    expect(found.own).toEqual({
      bars: [],
      unreadableSince: ms(AT),
      limited: { at: ms(AT), until: null, readable: false },
    });
    expect(found.operator).toEqual({ granted: false, subscriptions: [] });
    expect(found.summary.nextRelease).toBeNull();
    for (const limited of [
      'x',
      { at: AT, until: null },
      { at: null, until: null, readable: true },
      { at: 'now', until: null, readable: true },
      { at: AT, until: 'soon', readable: true },
    ]) {
      const own = { windows: [], unreadable: 'x', limited };
      expect(parseOrgQuotas({ ...QUOTAS, own }).own).toMatchObject({ limited: null, unreadableSince: null });
    }
  });

  it('refuses a malformed answer', () => {
    for (const broken of [
      null,
      { ...QUOTAS, own: null },
      { ...QUOTAS, operator: 'x' },
      { ...QUOTAS, platform: null },
      { ...QUOTAS, summary: null },
      { ...QUOTAS, operator: { granted: 1, subscriptions: [] } },
      { ...QUOTAS, platform: { available: null } },
      { ...QUOTAS, summary: { usable: -1, exhausted: 0, next_release: null } },
      { ...QUOTAS, summary: { usable: 1, exhausted: 1.5, next_release: null } },
      { ...QUOTAS, summary: { usable: 1, exhausted: 0, next_release: 'soon' } },
    ]) {
      expect(codeOf(() => parseOrgQuotas(broken))).toBe('unavailable');
    }
  });

  it('loads the answer of the hub', async () => {
    const t = await credWorld();
    t.client.on(`GET /v1/orgs/${t.org}/quotas`, () => QUOTAS);
    expect((await loadOrgQuotas(t.context, t.org)).summary.usable).toBe(1);
    await expect(loadOrgQuotas(t.context, 'other')).rejects.toMatchObject({ code: 'not_found' });
  });
});
