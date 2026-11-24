// U-WEB-billing (KOR-31): the organization's credit and consumption, the charges of a task, prices of the price list
// in force, and the formats of amounts and runtimes.
import { describe, expect, it } from 'vitest';
import {
  formatRuntime,
  formatUsd,
  loadOrgBilling,
  loadTaskChargesPage,
  orgBilling,
  people,
  priceRows,
  sessionCharges,
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
    expect(found?.creditsTruncated).toBe(false);
    expect(orgBilling({ ...BILLING, credits_truncated: true })?.creditsTruncated).toBe(true);
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
    expect([{ session_id: 's1', ...totals }, { ...totals }, { session_id: 's2' }].map(sessionCharges)).toEqual([
      { sessionId: 's1', tokensMicroUsd: 445_200, runtimeMicroUsd: 3000, tokens: 1_100_000, runtimeMs: 30_000 },
      null,
      null,
    ]);
  });
});

describe('loaders', () => {
  it('loads org billing and task charges, refusing malformed answers', async () => {
    const t = await credWorld();
    t.client.on('GET /v1/orgs/o%2F1/billing', () => BILLING);
    expect((await loadOrgBilling(t.context, 'o/1')).version).toBe(1);
    t.client.on('GET /v1/orgs/o%2F1/billing', () => ({}));
    await expect(loadOrgBilling(t.context, 'o/1')).rejects.toBeInstanceOf(HubError);
    t.client.on('GET /v1/tasks/t1/charges', () => null);
    await expect(loadTaskChargesPage(t.context, 't1', null)).rejects.toBeInstanceOf(HubError);
    t.client.on('GET /v1/tasks/t1/charges', () => ({ sessions: [] }));
    await expect(loadTaskChargesPage(t.context, 't1', null)).rejects.toBeInstanceOf(HubError);
  });

  it('reads the charges of a task page by page, the total from the newest page', async () => {
    const t = await credWorld();
    t.client.on('GET /v1/tasks/t%2F1/charges', (_body, target) =>
      String(target).includes('before=')
        ? { sessions: [{ session_id: 's1', ...totals }], next_before: null }
        : { sessions: [{ session_id: 's2', ...totals }, 'x'], total_micro_usd: 9, next_before: 'c1', people: PEOPLE },
    );
    const newest = await loadTaskChargesPage(t.context, 't/1', null);
    expect(newest).toMatchObject({
      rows: [{ sessionId: 's2' }],
      nextBefore: 'c1',
      truncated: false,
      totalMicroUsd: 9,
    });
    // the consumption per person of the whole task comes with the newest page (ADR-083)
    expect(newest.people?.map((row) => row.userId)).toEqual(['u-1', null]);
    expect(await loadTaskChargesPage(t.context, 't/1', 'c1')).toMatchObject({
      rows: [{ sessionId: 's1' }],
      nextBefore: null,
      totalMicroUsd: null,
    });
    expect(t.client.of('GET /v1/tasks/t%2F1/charges').map((call) => call.target)).toEqual([
      '/v1/tasks/t%2F1/charges?limit=100',
      '/v1/tasks/t%2F1/charges?before=c1&limit=100',
    ]);
  });

  it('reads the whole list once on a hub without paging', async () => {
    const t = await credWorld();
    t.client.on('GET /v1/tasks/t1/charges', (_body, target) => {
      if (String(target).includes('?')) throw new HubError('request_invalid', 400);
      return { sessions: [{ session_id: 's1', ...totals }], total_micro_usd: 5, truncated: true };
    });
    expect(await loadTaskChargesPage(t.context, 't1', null)).toMatchObject({
      rows: [{ sessionId: 's1' }],
      nextBefore: null,
      truncated: true,
      totalMicroUsd: 5,
    });
    expect(await loadTaskChargesPage(t.context, 't1', 'c')).toEqual({
      rows: [],
      nextBefore: null,
      truncated: false,
      totalMicroUsd: null,
      people: null,
    });
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
