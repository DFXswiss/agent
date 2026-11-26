// U-WEB-monitoring-slices: the `monitoring` slice (overview, live samples, series, session links) and the `audit`
// slice (pages with the time filter, verification) against a scripted signed client.
import { describe, expect, it } from 'vitest';
import { NO_FILTERS } from '../../../src/core/monitoring/audit';
import { CHARTS, seriesKey, seriesWindow, rangeOf } from '../../../src/core/monitoring/series';
import { HubError } from '../../../src/core/net/signedFetch';
import { createStore } from '../../../src/core/store';
import { setActiveOrg } from '../../../src/core/store/slices/session';
import {
  AUTO_PAGES,
  auditView,
  canLoadMore,
  loadAudit,
  verifyAudit,
  verifyState,
  viewKey,
  visibleRows,
} from '../../../src/core/store/slices/audit';
import {
  dropSeries,
  frames,
  linkSessions,
  loadOverview,
  loadSeries,
  orgMonitoring,
  selectChart,
} from '../../../src/core/store/slices/monitoring';
import { FakeClient } from '../tasks/world';
import { auditRow, DEV, iso, ORG, overviewBody, S1, S2, sample, T0, TASK } from './fixtures';

function setup() {
  const store = createStore();
  const client = new FakeClient();
  const clock = { now: T0 };
  return { store, client, clock, context: { store, client, now: () => clock.now } };
}

/** A promise with its resolve function (to hold a request open). */
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => (resolve = done));
  return { promise, resolve };
}

describe('monitoring slice', () => {
  it('loads the overview and keeps it visible while reloading', async () => {
    const t = setup();
    expect(orgMonitoring(t.store.getState().monitoring, ORG)).toMatchObject({ status: 'loading', overview: null });
    t.client.on('GET /v1/monitoring/overview', () => overviewBody());
    await loadOverview(t.context, ORG);
    expect(t.client.calls[0]!.target).toBe(`/v1/monitoring/overview?org=${ORG}`);
    const loaded = orgMonitoring(t.store.getState().monitoring, ORG);
    expect(loaded).toMatchObject({ status: 'ready', code: null, loadedAt: T0, unknown: false });
    const gate = deferred<unknown>();
    t.client.on('GET /v1/monitoring/overview', () => gate.promise);
    const reload = loadOverview(t.context, ORG);
    expect(orgMonitoring(t.store.getState().monitoring, ORG).status).toBe('ready');
    gate.resolve(null);
    await reload;
    expect(orgMonitoring(t.store.getState().monitoring, ORG)).toMatchObject({
      status: 'error',
      code: 'unavailable',
      overview: loaded.overview,
    });
  });

  it('drops answers that arrive after a sign-out', async () => {
    const t = setup();
    const ok = deferred<unknown>();
    t.client.on('GET /v1/monitoring/overview', () => ok.promise);
    const first = loadOverview(t.context, ORG);
    t.store.reset();
    ok.resolve(overviewBody());
    await first;
    expect(t.store.getState().monitoring.orgs).toEqual({});
    const failing = deferred<unknown>();
    t.client.on('GET /v1/monitoring/overview', () => failing.promise);
    const second = loadOverview(t.context, ORG);
    t.store.reset();
    failing.resolve(Promise.reject(new HubError('not_found', 404)));
    await second;
    expect(t.store.getState().monitoring.orgs).toEqual({});
  });

  it('applies metrics frames to the active org and asks for unknown entities once', async () => {
    const t = setup();
    let body: unknown = overviewBody();
    t.client.on('GET /v1/monitoring/overview', () => body);
    await loadOverview(t.context, ORG);
    const frame = (kind: string, id: string, ts: number) => ({
      samples: [{ entity_kind: kind, entity_id: id, sample: sample(ts) }],
    });
    // no active org, or an empty frame: nothing changes
    const before = t.store.getState().monitoring;
    frames.metrics(t.store, frame('device', DEV, T0 + 15_000));
    setActiveOrg(t.store, ORG);
    frames.metrics(t.store, { samples: [] });
    frames.metrics(t.store, frame('device', DEV, T0));
    expect(t.store.getState().monitoring).toBe(before);
    frames.metrics(t.store, frame('device', DEV, T0 + 15_000));
    expect(orgMonitoring(t.store.getState().monitoring, ORG).overview!.devices[0]!.latest!.ts).toBe(iso(T0 + 15_000));
    expect(orgMonitoring(t.store.getState().monitoring, ORG).unknown).toBe(false);
    // the active org's overview not loaded yet
    setActiveOrg(t.store, 'other');
    const unloaded = t.store.getState().monitoring;
    frames.metrics(t.store, frame('device', DEV, T0 + 30_000));
    expect(t.store.getState().monitoring).toBe(unloaded);
    setActiveOrg(t.store, ORG);
    // unknown entities are pending until a read; one the read still does not list is skipped from then on
    frames.metrics(t.store, frame('vm', 'vm-0000000f', T0));
    frames.metrics(t.store, frame('vm', 'vm-0000000f', T0));
    expect(orgMonitoring(t.store.getState().monitoring, ORG)).toMatchObject({
      unknown: true,
      pending: ['vm vm-0000000f'],
    });
    const gate = deferred<unknown>();
    t.client.on('GET /v1/monitoring/overview', () => gate.promise);
    const reading = loadOverview(t.context, ORG);
    frames.metrics(t.store, frame('session', 'new', T0));
    gate.resolve(body);
    await reading;
    expect(orgMonitoring(t.store.getState().monitoring, ORG)).toMatchObject({
      unknown: true,
      pending: ['session new'],
      skip: ['vm vm-0000000f'],
    });
    const skipped = t.store.getState().monitoring;
    frames.metrics(t.store, frame('vm', 'vm-0000000f', T0));
    expect(t.store.getState().monitoring).toBe(skipped);
    // the next read lists the new session: nothing pending
    const listed = overviewBody();
    body = { ...listed, sessions: [...listed.sessions, { ...listed.sessions[1], session_id: 'new' }] };
    t.client.on('GET /v1/monitoring/overview', () => body);
    await loadOverview(t.context, ORG);
    expect(orgMonitoring(t.store.getState().monitoring, ORG)).toMatchObject({ unknown: false, pending: [] });
  });

  it('applies only the newest of overlapping reads', async () => {
    const t = setup();
    const slow = deferred<unknown>();
    const answers = [() => slow.promise, () => overviewBody()];
    t.client.on('GET /v1/monitoring/overview', () => answers.shift()!());
    const first = loadOverview(t.context, ORG);
    await loadOverview(t.context, ORG);
    const newest = orgMonitoring(t.store.getState().monitoring, ORG);
    slow.resolve({ devices: [], vms: [], sessions: [] });
    await first;
    expect(orgMonitoring(t.store.getState().monitoring, ORG)).toBe(newest);
    const failing = deferred<unknown>();
    answers.push(
      () => failing.promise,
      () => overviewBody(),
    );
    const late = loadOverview(t.context, ORG);
    await loadOverview(t.context, ORG);
    const kept = orgMonitoring(t.store.getState().monitoring, ORG);
    failing.resolve(Promise.reject(new HubError('unavailable', 503)));
    await late;
    expect(orgMonitoring(t.store.getState().monitoring, ORG)).toBe(kept);
  });

  it('loads a series once and again after a failure, and prunes others', async () => {
    const t = setup();
    const request = {
      kind: 'device' as const,
      id: DEV,
      metric: 'cpu_ratio' as const,
      window: seriesWindow(rangeOf('15m'), T0),
    };
    const key = seriesKey(request);
    t.client.on('GET /v1/monitoring/series', () => {
      throw new HubError('request_invalid', 400);
    });
    await loadSeries(t.context, request);
    expect(t.store.getState().monitoring.series[key]).toEqual({ status: 'error', code: 'request_invalid', points: [] });
    t.client.on('GET /v1/monitoring/series', () => ({ points: [[iso(T0), 0.5]] }));
    await loadSeries(t.context, request);
    expect(t.store.getState().monitoring.series[key]).toEqual({ status: 'ready', code: null, points: [[T0, 0.5]] });
    await loadSeries(t.context, request);
    expect(t.client.of('GET /v1/monitoring/series')).toHaveLength(2);
    const before = t.store.getState().monitoring;
    dropSeries(t.store, ['other']);
    expect(t.store.getState().monitoring).toBe(before);
    dropSeries(t.store, [key]);
    expect(t.store.getState().monitoring.series).toEqual({});
  });

  it('does not store the answer of a series dropped while it loaded', async () => {
    const t = setup();
    const request = {
      kind: 'device' as const,
      id: DEV,
      metric: 'cpu_ratio' as const,
      window: seriesWindow(rangeOf('6h'), T0),
    };
    for (const answer of [Promise.resolve({ points: [] }), Promise.reject(new HubError('not_found', 404))]) {
      const gate = deferred<unknown>();
      t.client.on('GET /v1/monitoring/series', () => gate.promise);
      const loading = loadSeries(t.context, request);
      dropSeries(t.store, [seriesKey(request)]);
      gate.resolve(answer);
      await loading;
      expect(t.store.getState().monitoring.series).toEqual({});
    }
  });

  it('does not load a series twice at once and drops late answers', async () => {
    const t = setup();
    const request = {
      kind: 'vm' as const,
      id: 'vm-1',
      metric: 'net_rx_bytes' as const,
      window: seriesWindow(rangeOf('1h'), T0),
    };
    const gate = deferred<unknown>();
    t.client.on('GET /v1/monitoring/series', () => gate.promise);
    const first = loadSeries(t.context, request);
    await loadSeries(t.context, request);
    expect(t.client.calls).toHaveLength(1);
    t.store.reset();
    gate.resolve({ points: [] });
    await first;
    expect(t.store.getState().monitoring.series).toEqual({});
    const failing = deferred<unknown>();
    t.client.on('GET /v1/monitoring/series', () => failing.promise);
    const second = loadSeries(t.context, request);
    t.store.reset();
    failing.resolve(Promise.reject(new Error('down')));
    await second;
    expect(t.store.getState().monitoring.series).toEqual({});
  });

  it('selects a chart: loading, failed, or the aligned data, stable while the series stay', async () => {
    const t = setup();
    const memory = CHARTS.find((chart) => chart.id === 'memory')!;
    const window = seriesWindow(rangeOf('15m'), T0);
    const requests = memory.metrics.map((metric) => ({ kind: 'vm' as const, id: 'vm-1', metric, window }));
    const select = selectChart(memory, requests);
    expect(select(t.store.getState())).toEqual({ status: 'loading', code: null, data: null });
    t.client.on('GET /v1/monitoring/series', (_body, target) => {
      if (target.includes('mem_limit_bytes')) throw new HubError('not_found', 404);
      return { points: [[iso(T0), 5]] };
    });
    await loadSeries(t.context, requests[0]!);
    expect(select(t.store.getState()).status).toBe('loading');
    await loadSeries(t.context, requests[1]!);
    expect(select(t.store.getState())).toEqual({ status: 'error', code: 'not_found', data: null });
    t.client.on('GET /v1/monitoring/series', () => ({ points: [[iso(T0), 8]] }));
    await loadSeries(t.context, requests[1]!);
    const ready = select(t.store.getState());
    expect(ready.status).toBe('ready');
    expect(ready.data!.series.map((item) => item.values)).toEqual([[5], [8]]);
    t.store.update('monitoring', (state) => ({ ...state, links: { x: null } }));
    expect(select(t.store.getState())).toBe(ready);
  });

  it('links sessions to the VM of their rows once', async () => {
    const t = setup();
    t.client.on(`GET /v1/sessions/${S1}`, () => ({ session: { session_id: S1, vm_id: 'vm-00000001' } }));
    t.client.on(`GET /v1/sessions/${S2}`, () => ({ session: { session_id: S2, vm_id: null } }));
    t.client.on('GET /v1/sessions/bad', () => null);
    t.client.on('GET /v1/sessions/hidden', () => {
      throw new HubError('forbidden', 403);
    });
    t.client.on('GET /v1/sessions/flaky', () => {
      throw new HubError('unavailable', 503);
    });
    await linkSessions(t.context, [S1, S2, 'gone', 'bad', 'hidden', 'flaky', S1]);
    expect(t.store.getState().monitoring.links).toEqual({
      [S1]: 'vm-00000001',
      [S2]: null,
      gone: null,
      bad: null,
      hidden: null,
    });
    expect(t.client.calls).toHaveLength(6);
    // a failure that may pass is read again; overlapping calls read a row once
    const gate = deferred<unknown>();
    t.client.on('GET /v1/sessions/flaky', () => gate.promise);
    const first = linkSessions(t.context, ['flaky']);
    await linkSessions(t.context, ['flaky']);
    gate.resolve({ session: { vm_id: 'vm-00000002' } });
    await first;
    expect(t.store.getState().monitoring.links.flaky).toBe('vm-00000002');
    expect(t.client.of('GET /v1/sessions/flaky')).toHaveLength(2);
    const late = deferred<unknown>();
    t.client.on('GET /v1/sessions/late', () => late.promise);
    const pending = linkSessions(t.context, ['late']);
    t.store.reset();
    late.resolve({ session: { vm_id: 'vm-2' } });
    await pending;
    expect(t.store.getState().monitoring.links).toEqual({});
  });

  it('updates the link from session frames', () => {
    const t = setup();
    frames.session(t.store, null);
    frames.session(t.store, { session: { vm_id: 'vm-1' } });
    expect(t.store.getState().monitoring.links).toEqual({});
    frames.session(t.store, { session: { session_id: S1, vm_id: 'vm-1' } });
    expect(t.store.getState().monitoring.links).toEqual({ [S1]: 'vm-1' });
    const before = t.store.getState().monitoring;
    frames.session(t.store, { session: { session_id: S1, vm_id: 'vm-1' } });
    expect(t.store.getState().monitoring).toBe(before);
    frames.session(t.store, { session: { session_id: S1 } });
    expect(t.store.getState().monitoring.links).toEqual({ [S1]: null });
  });
});

describe('audit slice', () => {
  const page = (rows: unknown[], next: string | null) => ({ rows, next_before: next });
  const AGENT_1 = { task_id: TASK, number: 4, agent_path: null, parent_session_id: null };
  const AGENT_2 = { task_id: TASK, number: 4, agent_path: '1', parent_session_id: S1 };

  it('loads the first page and the next one, newest first', async () => {
    const t = setup();
    const key = viewKey(ORG, NO_FILTERS);
    expect(auditView(t.store.getState().audit, key)).toMatchObject({ status: 'loading', rows: [] });
    t.client.on(`GET /v1/orgs/${ORG}/audit`, (_body, target) =>
      target.includes('before=')
        ? { ...page([auditRow(1, T0)], null), actors: { [S2]: AGENT_2 } }
        : { ...page([auditRow(3, T0 + 2), auditRow(2, T0 + 1)], 'c2'), actors: { [S1]: AGENT_1 } },
    );
    await loadAudit(t.context, ORG, NO_FILTERS);
    let view = auditView(t.store.getState().audit, key);
    expect(view).toMatchObject({ status: 'ready', next: 'c2' });
    expect(view.rows.map((row) => row.seq)).toEqual([3, 2]);
    expect(canLoadMore(view, NO_FILTERS)).toBe(true);
    await loadAudit(t.context, ORG, NO_FILTERS, true);
    view = auditView(t.store.getState().audit, key);
    expect(view.rows.map((row) => row.seq)).toEqual([3, 2, 1]);
    expect(view.next).toBeNull();
    // the agent actors of every page stay with the view
    expect(view.actors).toEqual({ [S1]: AGENT_1, [S2]: AGENT_2 });
    await loadAudit(t.context, ORG, NO_FILTERS, true);
    expect(t.client.calls).toHaveLength(2);
  });

  it('pages on its own until the time range shows up, bounded', async () => {
    const t = setup();
    const filters = { ...NO_FILTERS, to: T0 };
    let served = 0;
    t.client.on(`GET /v1/orgs/${ORG}/audit`, () => {
      served += 1;
      return page([auditRow(100 - served, T0 + 1000 * (10 - served))], `c${served}`);
    });
    await loadAudit(t.context, ORG, filters);
    expect(served).toBe(AUTO_PAGES);
    const view = auditView(t.store.getState().audit, viewKey(ORG, filters));
    expect(visibleRows(view, filters)).toEqual([]);
    expect(view.rows).toHaveLength(AUTO_PAGES);
    // the next load reaches the range (rows at or before `to`)
    await loadAudit(t.context, ORG, filters, true);
    expect(served).toBe(AUTO_PAGES + 5);
    const more = auditView(t.store.getState().audit, viewKey(ORG, filters));
    expect(visibleRows(more, filters).map((row) => row.seq)).toEqual([90]);
  });

  it('stops paging once the rows are older than the range', async () => {
    const t = setup();
    const filters = { ...NO_FILTERS, from: T0 + 5000 };
    t.client.on(`GET /v1/orgs/${ORG}/audit`, () => page([auditRow(1, T0)], 'more'));
    await loadAudit(t.context, ORG, filters);
    const view = auditView(t.store.getState().audit, viewKey(ORG, filters));
    expect(t.client.calls).toHaveLength(1);
    expect(canLoadMore(view, filters)).toBe(false);
  });

  it('keeps loaded rows on a failure and drops late answers', async () => {
    const t = setup();
    t.client.on(`GET /v1/orgs/${ORG}/audit`, () => page([auditRow(2, T0)], 'c'));
    await loadAudit(t.context, ORG, NO_FILTERS);
    t.client.on(`GET /v1/orgs/${ORG}/audit`, () => {
      throw new HubError('rate_limited', 429);
    });
    await loadAudit(t.context, ORG, NO_FILTERS, true);
    const key = viewKey(ORG, NO_FILTERS);
    expect(auditView(t.store.getState().audit, key)).toMatchObject({
      status: 'error',
      code: 'rate_limited',
      next: 'c',
    });
    expect(auditView(t.store.getState().audit, key).rows).toHaveLength(1);
    const ok = deferred<unknown>();
    t.client.on(`GET /v1/orgs/${ORG}/audit`, () => ok.promise);
    const late = loadAudit(t.context, ORG, NO_FILTERS);
    t.store.reset();
    ok.resolve(page([], null));
    await late;
    expect(t.store.getState().audit.views).toEqual({});
    const failing = deferred<unknown>();
    t.client.on(`GET /v1/orgs/${ORG}/audit`, () => failing.promise);
    const lateError = loadAudit(t.context, ORG, NO_FILTERS);
    t.store.reset();
    failing.resolve(Promise.reject(new HubError('not_found', 404)));
    await lateError;
    expect(t.store.getState().audit.views).toEqual({});
  });

  it('verifies the chain', async () => {
    const t = setup();
    expect(verifyState(t.store.getState().audit, ORG)).toEqual({ status: 'ready', code: null, result: null, at: null });
    const head = { seq: 4, hash: 'a'.repeat(64) };
    t.client.on(`GET /v1/orgs/${ORG}/audit/verify`, () => ({ ok: true, head, checkpoint: null }));
    await verifyAudit(t.context, ORG);
    expect(verifyState(t.store.getState().audit, ORG)).toEqual({
      status: 'ready',
      code: null,
      result: { ok: true, head, checkpoint: null },
      at: T0,
    });
    t.client.on(`GET /v1/orgs/${ORG}/audit/verify`, () => {
      throw new HubError('forbidden', 403);
    });
    await verifyAudit(t.context, ORG);
    expect(verifyState(t.store.getState().audit, ORG)).toMatchObject({ status: 'error', code: 'forbidden' });
    for (const answer of [Promise.resolve({ ok: true, head, checkpoint: null }), Promise.reject(new Error('x'))]) {
      const gate = deferred<unknown>();
      t.client.on(`GET /v1/orgs/${ORG}/audit/verify`, () => gate.promise);
      const late = verifyAudit(t.context, ORG);
      t.store.reset();
      gate.resolve(answer);
      await late;
      expect(t.store.getState().audit.verify).toEqual({});
    }
  });
});
