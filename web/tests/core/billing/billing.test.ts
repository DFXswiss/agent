// U-WEB-billing (KOR-31): the organization's credit and consumption, the charges of a task, prices of the price list
// in force, and the formats of amounts and runtimes.
import { describe, expect, it } from 'vitest';
import {
  formatRuntime,
  formatUsd,
  loadOrgBilling,
  loadTaskCharges,
  orgBilling,
  people,
  priceRows,
  taskCharges,
} from '../../../src/core/billing/billing';
import { HubError } from '../../../src/core/net/signedFetch';
import { credWorld } from '../credentials/world';
import { BILLING, PEOPLE, totals } from './world';

describe('org billing', () => {
  it('parses the answer and drops malformed rows', () => {
    const found = orgBilling(BILLING);
    expect(found).toMatchObject({ balanceMicroUsd: 4_551_800, exhausted: false, active: true, version: 1 });
    expect(found?.tokenPrices).toEqual([{ label: 'deepseek/flash', input: 0.6, output: 2.4 }]);
    expect(found?.hourly).toEqual([
      { size: 'session', usd: 0.36 },
      { size: 'large', usd: 0.72 },
    ]);
    expect(found?.credits.map((entry) => [entry.kind, entry.reason])).toEqual([
      ['grant', 'hi'],
      ['start', null],
    ]);
    expect(found?.days).toEqual([
      { day: '2026-11-21', tokensMicroUsd: 445_200, runtimeMicroUsd: 3000, tokens: 1_100_000, runtimeMs: 30_000 },
    ]);
    expect(found?.workgroups.map((row) => row.wgId)).toEqual(['wg-1']);
    expect(found?.people).toBeNull();
  });

  it('reads the consumption per person when the hub sends it (ADR-083)', () => {
    const found = orgBilling({ ...BILLING, people: PEOPLE });
    expect(found?.people).toEqual([
      { userId: 'u-1', tokensMicroUsd: 445_200, runtimeMicroUsd: 3000, tokens: 1_100_000, runtimeMs: 30_000 },
      { userId: null, tokensMicroUsd: 445_200, runtimeMicroUsd: 3000, tokens: 1_100_000, runtimeMs: 30_000 },
    ]);
    expect(people(undefined)).toBeNull();
    expect(people([])).toEqual([]);
  });

  it('accepts an answer without price list and lists, and refuses malformed answers', () => {
    const bare = { balance_micro_usd: 0, exhausted: false, billing_active: false, price_list: null };
    expect(orgBilling(bare)).toMatchObject({ version: null, tokenPrices: [], hourly: [], credits: [], days: [] });
    for (const wrong of [
      null,
      {},
      { balance_micro_usd: 'x' },
      { ...bare, exhausted: 1 },
      { ...bare, billing_active: 1 },
    ]) {
      expect(orgBilling(wrong)).toBeNull();
    }
  });

  it('reads prices only from a well-formed document', () => {
    expect(priceRows(null)).toEqual({ tokenPrices: [], hourly: [] });
    expect(priceRows({ tokens: {}, runtime: 1 })).toEqual({ tokenPrices: [], hourly: [] });
    expect(priceRows({ tokens: { models: { m: { input: 1, output: 2 } } }, runtime: {} })).toEqual({
      tokenPrices: [],
      hourly: [],
    });
    expect(priceRows({ tokens: { factors: { platform: 1 }, models: 3 }, runtime: { per_hour: { s: 'x' } } })).toEqual({
      tokenPrices: [],
      hourly: [],
    });
  });
});

describe('task charges', () => {
  it('parses sessions and refuses malformed answers', () => {
    expect(
      taskCharges({
        sessions: [{ session_id: 's1', ...totals }, { ...totals }, { session_id: 's2' }],
        total_micro_usd: 1,
      }),
    ).toEqual({
      totalMicroUsd: 1,
      sessions: [
        { sessionId: 's1', tokensMicroUsd: 445_200, runtimeMicroUsd: 3000, tokens: 1_100_000, runtimeMs: 30_000 },
      ],
      people: null,
    });
    expect(taskCharges({ sessions: 'x', total_micro_usd: 0 })).toEqual({
      totalMicroUsd: 0,
      sessions: [],
      people: null,
    });
    expect(taskCharges({ sessions: [], total_micro_usd: 0, people: PEOPLE })?.people?.map((row) => row.userId)).toEqual(
      ['u-1', null],
    );
    expect(taskCharges({ sessions: [] })).toBeNull();
    expect(taskCharges(null)).toBeNull();
  });
});

describe('loaders', () => {
  it('loads org billing and task charges, refusing malformed answers', async () => {
    const t = await credWorld();
    t.client.on('GET /v1/orgs/o%2F1/billing', () => BILLING);
    expect((await loadOrgBilling(t.context, 'o/1')).version).toBe(1);
    t.client.on('GET /v1/orgs/o%2F1/billing', () => ({}));
    await expect(loadOrgBilling(t.context, 'o/1')).rejects.toBeInstanceOf(HubError);
    t.client.on('GET /v1/tasks/t1/charges', () => ({ sessions: [], total_micro_usd: 0 }));
    expect(await loadTaskCharges(t.context, 't1')).toEqual({ sessions: [], totalMicroUsd: 0, people: null });
    t.client.on('GET /v1/tasks/t1/charges', () => null);
    await expect(loadTaskCharges(t.context, 't1')).rejects.toBeInstanceOf(HubError);
  });
});

describe('formats', () => {
  it('shows dollars with cents, small amounts with four decimals', () => {
    expect(formatUsd(4_551_800, 'en')).toBe('$4.55');
    expect(formatUsd(3000, 'en')).toBe('$0.0030');
    expect(formatUsd(0, 'en')).toBe('$0.00');
    expect(formatUsd(-1_500_000, 'en')).toBe('-$1.50');
    expect(formatUsd(1_234_560_000, 'de')).toMatch(/^1\.234,56\s\$$/);
  });

  it('shows runtimes in hours, minutes and seconds', () => {
    expect(formatRuntime(0)).toBe('0 s');
    expect(formatRuntime(30_000)).toBe('30 s');
    expect(formatRuntime(3_600_000)).toBe('1 h');
    expect(formatRuntime(3_723_000)).toBe('1 h 2 min 3 s');
  });
});
