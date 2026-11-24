// U-WEB-credentials (WEB-10): quota bars from `GET /v1/me/quotas` (newest read per window, ordered 5 h, week, per
// model) and the per-task usage totals of the usage strip (tokens, cost with basis, context share of the main session).
import { describe, expect, it } from 'vitest';
import { barShare, loadQuotas, quotaBar, quotaBars } from '../../../src/core/credentials/quotas';
import { applyFrame, frameHandlers } from '../../../src/core/net/frames';
import { HubError } from '../../../src/core/net/signedFetch';
import { createStore } from '../../../src/core/store';
import {
  isSessionUsage,
  putTaskUsage,
  putUsage,
  type SessionUsage,
  taskUsage,
} from '../../../src/core/store/slices/usage';
import { credWorld } from './world';

const row = (overrides: Record<string, unknown> = {}) => ({
  window: 'five_hour',
  used_percent: 42.5,
  resets_at: '2026-11-21T15:00:00.000Z',
  severity: null,
  fetched_at: '2026-11-21T12:00:00.000Z',
  device_id: '0190f000-0000-7000-8000-000000000001',
  ...overrides,
});

describe('quota bars', () => {
  it('parses rows and drops malformed ones', () => {
    expect(quotaBar(row())).toEqual({
      window: 'five_hour',
      model: null,
      usedPercent: 42.5,
      resetsAt: Date.parse('2026-11-21T15:00:00.000Z'),
      severity: null,
      fetchedAt: Date.parse('2026-11-21T12:00:00.000Z'),
    });
    expect(quotaBar(row({ window: 'weekly:claude-opus-4-1', resets_at: null, severity: 'warning' }))).toMatchObject({
      model: 'claude-opus-4-1',
      resetsAt: null,
      severity: 'warning',
    });
    for (const broken of [
      'x',
      row({ window: 'daily' }),
      row({ window: 3 }),
      row({ used_percent: -1 }),
      row({ used_percent: 'a' }),
      row({ used_percent: Infinity }),
      row({ fetched_at: 'x' }),
      row({ fetched_at: 3 }),
      row({ resets_at: 'soon' }),
    ]) {
      expect(quotaBar(broken)).toBeNull();
    }
  });

  it('keeps the newest read per window, ordered', () => {
    const bars = quotaBars([
      row({ window: 'weekly:b-model' }),
      row({ window: 'seven_day', used_percent: 10 }),
      row({ window: 'weekly:a-model' }),
      row({ used_percent: 99, fetched_at: '2026-11-21T11:00:00.000Z' }),
      row({ used_percent: 50, fetched_at: '2026-11-21T12:30:00.000Z' }),
      row({ used_percent: 1, fetched_at: '2026-11-21T12:10:00.000Z' }),
      row({ window: 'weekly:a-model', used_percent: 3 }),
      'junk',
    ]);
    expect(bars.map((bar) => [bar.window, bar.usedPercent])).toEqual([
      ['five_hour', 50],
      ['seven_day', 10],
      ['weekly:a-model', 42.5],
      ['weekly:b-model', 42.5],
    ]);
    expect(barShare(bars[0]!)).toBe(0.5);
    expect(barShare({ ...bars[0]!, usedPercent: 140 })).toBe(1);
  });

  it('loads into the slice, refuses malformed answers, writes nothing after a sign-out', async () => {
    const t = await credWorld();
    t.client.on('GET /v1/me/quotas', () => ({ quotas: [row()] }));
    expect(await loadQuotas(t.context)).toHaveLength(1);
    expect(t.store.getState().quotas.bars).toHaveLength(1);
    expect(t.store.getState().quotas.unreadableSince).toBeNull();
    // KOR-40: the subscription refused the read (a setup token): since when, a malformed marker counts as none
    t.client.on('GET /v1/me/quotas', () => ({ quotas: [], unreadable: { since: '2026-11-21T12:00:00.000Z' } }));
    await loadQuotas(t.context);
    expect(t.store.getState().quotas.unreadableSince).toBe(Date.parse('2026-11-21T12:00:00.000Z'));
    t.client.on('GET /v1/me/quotas', () => ({ quotas: [], unreadable: { since: 'never' } }));
    await loadQuotas(t.context);
    expect(t.store.getState().quotas.unreadableSince).toBeNull();
    t.client.on('GET /v1/me/quotas', () => ({ quotas: 'x' }));
    await expect(loadQuotas(t.context)).rejects.toBeInstanceOf(HubError);
    t.client.on('GET /v1/me/quotas', () => null);
    await expect(loadQuotas(t.context)).rejects.toBeInstanceOf(HubError);
    t.client.on('GET /v1/me/quotas', () => {
      t.store.reset();
      return { quotas: [] };
    });
    await loadQuotas(t.context);
    expect(t.store.getState().quotas.bars).toBeNull();
  });
});

const usage = (overrides: Partial<Record<keyof SessionUsage, unknown>> = {}): SessionUsage =>
  ({
    requests: 3,
    input_tokens: 1000,
    output_tokens: 200,
    cache_read_tokens: 50,
    cache_write_tokens: 25,
    thinking_tokens: 0,
    cost_micro_usd: 1300,
    cost_basis: 'provider',
    context_used_tokens: 30_000,
    context_window_tokens: 120_000,
    last_provider: 'DeepInfra',
    updated_at: '2026-11-21T12:00:00.000Z',
    ...overrides,
  }) as SessionUsage;

describe('usage', () => {
  it('checks counters', () => {
    expect(isSessionUsage(usage())).toBe(true);
    expect(isSessionUsage(usage({ last_provider: null }))).toBe(true);
    for (const broken of [
      usage({ input_tokens: -1 }),
      usage({ cost_basis: 'guess' }),
      usage({ last_provider: 3 }),
      usage({ updated_at: 'x' }),
      usage({ updated_at: 3 }),
      'x',
    ]) {
      expect(isSessionUsage(broken)).toBe(false);
    }
  });

  it('sums the task, names every basis and takes the context of the main session', () => {
    const store = createStore();
    expect(taskUsage(store.getState().usage, 't1', 's1')).toBeNull();
    putUsage(store, 't1', 's1', usage());
    putUsage(store, 't1', 's2', usage({ cost_basis: 'list_price', cost_micro_usd: 2_500_000, input_tokens: 5 }));
    putUsage(store, 't2', 's3', usage({ input_tokens: 999_999 }));
    expect(taskUsage(store.getState().usage, 't1', 's1')).toEqual({
      sessions: 2,
      input: 1005,
      output: 400,
      cache: 150,
      costUsd: 2.5013,
      bases: ['provider', 'list_price'],
      context: 0.25,
    });
    expect(taskUsage(store.getState().usage, 't1', null)!.context).toBeNull();
    expect(taskUsage(store.getState().usage, 't1', 'unknown')!.context).toBeNull();
    putUsage(store, 't1', 's1', usage({ context_window_tokens: 0, updated_at: '2026-11-21T12:00:01.000Z' }));
    expect(taskUsage(store.getState().usage, 't1', 's1')!.context).toBeNull();
    // an older counter does not replace a newer one
    putUsage(store, 't1', 's1', usage({ input_tokens: 1, updated_at: '2026-11-21T11:00:00.000Z' }));
    expect(store.getState().usage.bySession.s1!.usage.input_tokens).toBe(1000);
  });

  it('applies session frames with a counter', () => {
    const store = createStore();
    const handlers = frameHandlers();
    const session = { session_id: 's1', task_id: 't1' };
    applyFrame(store, 'session', { session, transition: null, usage: usage() }, 0, handlers);
    expect(store.getState().usage.bySession.s1!.task).toBe('t1');
    applyFrame(store, 'session', { session: { session_id: 's2' }, transition: null, usage: usage() }, 0, handlers);
    applyFrame(store, 'session', { session, transition: null }, 0, handlers);
    applyFrame(store, 'session', 'x', 0, handlers);
    applyFrame(store, 'session', { session: 'x', usage: usage() }, 0, handlers);
    expect(Object.keys(store.getState().usage.bySession)).toEqual(['s1']);
  });

  it('takes the counters of a task answer', () => {
    const store = createStore();
    putTaskUsage(store, 't1', [
      { session: { session_id: 's1', task_id: 't1' }, usage: usage(), transitions: [] },
      { session: { session_id: 's2', task_id: 't1' }, usage: null, transitions: [] },
      { session: { session_id: 's3', task_id: 'other' }, usage: usage(), transitions: [] },
      { session: 'x' },
      'junk',
    ]);
    expect(Object.keys(store.getState().usage.bySession)).toEqual(['s1']);
  });
});
