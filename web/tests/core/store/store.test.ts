// The store, its subscription API, the base slices and the selectors.
import { storeSlices } from '../../../src/core/store/slices';
import { describe, expect, it } from 'vitest';
import { Secret } from '../../../src/core/crypto/secret';
import { appStore, createStore, Store, subscribe } from '../../../src/core/store';
import {
  memoize,
  selectConfig,
  selectConnection,
  selectJobs,
  selectMe,
  selectOrg,
  selectOrgBlock,
  selectScopeEvents,
} from '../../../src/core/store/selectors';
import { setConfig } from '../../../src/core/store/slices/config';
import {
  banner,
  noteSession,
  sessionRenewed,
  setLiveState,
  setOnline,
} from '../../../src/core/store/slices/connection';
import { type EventEntry, findEvents, MAX_EVENTS, putEvents, touchEvents } from '../../../src/core/store/slices/events';
import { setQueue, syncing } from '../../../src/core/store/slices/jobs';
import { setIdentity, setProfile, setSession } from '../../../src/core/store/slices/me';
import {
  addKeys,
  blockOrg,
  ensureOrg,
  orgEntry,
  recordWraps,
  setLog,
  setPlatform,
  wrapUnauthorized,
} from '../../../src/core/store/slices/orgs';
import type { PlatformConfig } from '../../../src/core/config/check';
import type { VerifiedLog } from '../../../src/core/log/verify';
import type { VerifiedPlatformLog } from '../../../src/core/log/platform';
import type { Identity } from '../../../src/core/crypto/keys';

const entry = (scopeId: string, scopeSeq: number, status: EventEntry['status'] = 'pending'): EventEntry => ({
  org: 'o',
  scopeId,
  scopeSeq,
  receivedAt: null,
  event: {},
  status,
  code: null,
  header: null,
  body: null,
});

describe('store', () => {
  it('starts with the initial state of every discovered slice', () => {
    const store = createStore();
    expect(Object.keys(store.getState())).toEqual(storeSlices().map((slice) => slice.name));
    expect(Object.keys(store.getState())).toEqual(
      expect.arrayContaining(['config', 'connection', 'events', 'jobs', 'me', 'orgs']),
    );
    expect(store.getState().config).toEqual({ config: null });
    expect(appStore).toBeInstanceOf(Store);
  });

  it('replaces only the updated slice and notifies listeners; no notification without a change', () => {
    const store = createStore();
    const before = store.getState();
    let calls = 0;
    const off = store.subscribe(() => (calls += 1));
    setOnline(store, true);
    expect(calls).toBe(0);
    expect(store.getState()).toBe(before);
    setOnline(store, false);
    expect(calls).toBe(1);
    expect(store.getState().me).toBe(before.me);
    expect(store.getState().connection).not.toBe(before.connection);
    off();
    setOnline(store, true);
    expect(calls).toBe(1);
  });

  it('refuses unknown slices and resets everything', () => {
    const store = createStore();
    expect(() => store.update('nope' as 'me', (me) => me)).toThrow('unknown store slice "nope"');
    setOnline(store, false);
    let calls = 0;
    store.subscribe(() => (calls += 1));
    store.reset();
    expect(store.getState().connection.online).toBe(true);
    expect(calls).toBe(1);
  });

  it('subscribe(selector) fires only when the selected value changes', () => {
    const store = createStore();
    const seen: [boolean, boolean][] = [];
    const off = subscribe(
      store,
      (s) => s.connection.online,
      (next, previous) => seen.push([next, previous]),
    );
    setLiveState(store, { status: 'live', retryAt: null, attempt: 0, error: null });
    setOnline(store, false);
    setOnline(store, true);
    off();
    setOnline(store, false);
    expect(seen).toEqual([
      [false, true],
      [true, false],
    ]);
    const custom: number[] = [];
    subscribe(
      store,
      (s) => ({ attempt: s.connection.attempt }),
      (next) => custom.push(next.attempt),
      (a, b) => a.attempt === b.attempt,
    );
    setLiveState(store, { status: 'reconnecting', retryAt: 5, attempt: 1, error: 'unavailable' });
    setOnline(store, true);
    expect(custom).toEqual([1]);
  });
});

describe('base slices', () => {
  it('config and me', () => {
    const store = createStore();
    const config = { rp_id: 'hub.example' } as unknown as PlatformConfig;
    setConfig(store, config);
    expect(selectConfig(store.getState())).toBe(config);
    setSession(store, 'u1', { keyId: 'session.x', expiresAt: 5 });
    setProfile(store, { user_id: 'u1' });
    const identity = {} as Identity;
    setIdentity(store, identity);
    expect(selectMe(store.getState())).toEqual({
      userId: 'u1',
      session: { keyId: 'session.x', expiresAt: 5 },
      profile: { user_id: 'u1' },
      identity,
    });
  });

  it('connection: live state, online, session notices and the banner', () => {
    const store = createStore();
    noteSession(store, 'expiring');
    noteSession(store, 'expiring');
    expect(store.getState().connection.session).toBe('expiring');
    noteSession(store, 'expired');
    noteSession(store, 'expiring');
    expect(selectConnection(store.getState()).session).toBe('expired');
    sessionRenewed(store);
    const renewed = store.getState().connection;
    sessionRenewed(store);
    expect(store.getState().connection).toBe(renewed);
    expect(renewed.session).toBe('ok');

    const c = store.getState().connection;
    expect(banner(c, 0)).toEqual({ kind: 'none' });
    expect(banner({ ...c, status: 'connecting' }, 0)).toEqual({ kind: 'connecting' });
    expect(banner({ ...c, status: 'syncing' }, 0)).toEqual({ kind: 'connecting' });
    expect(banner({ ...c, status: 'live' }, 0)).toEqual({ kind: 'live' });
    expect(banner({ ...c, status: 'live', online: false }, 0)).toEqual({ kind: 'offline' });
    expect(banner({ ...c, status: 'offline' }, 0)).toEqual({ kind: 'offline' });
    expect(banner({ ...c, status: 'reconnecting', retryAt: 4500 }, 1000)).toEqual({ kind: 'reconnecting', seconds: 4 });
    expect(banner({ ...c, status: 'reconnecting', retryAt: 500 }, 1000)).toEqual({ kind: 'reconnecting', seconds: 0 });
    expect(banner({ ...c, status: 'reconnecting', retryAt: null }, 1000)).toEqual({ kind: 'reconnecting', seconds: 0 });
  });

  it('orgs: logs, blocks (first one stays), keys, wraps, platform', () => {
    const store = createStore();
    ensureOrg(store, 'o1');
    const known = store.getState().orgs;
    ensureOrg(store, 'o1');
    expect(store.getState().orgs).toBe(known);
    expect(orgEntry(store.getState().orgs, 'o2')).toEqual({
      id: 'o2',
      log: null,
      blocked: null,
      keys: new Map(),
      refusedWraps: new Map(),
    });
    const log = {} as VerifiedLog;
    setLog(store, 'o1', log);
    expect(selectOrg('o1')(store.getState())!.log).toBe(log);
    expect(selectOrg('o9')(store.getState())).toBeNull();
    blockOrg(store, 'o1', { code: 'log_fork', stored: { seq: 3, hash: 'h' }, served: null, lh: 5 });
    blockOrg(store, 'o1', { code: 'log_chain', stored: null, served: null, lh: null });
    expect(selectOrgBlock('o1')(store.getState())).toEqual({
      code: 'log_fork',
      stored: { seq: 3, hash: 'h' },
      served: null,
      lh: 5,
    });
    expect(selectOrgBlock('o2')(store.getState())).toBeNull();
    expect(selectOrgBlock('o1')(store.getState())).not.toHaveProperty('reason');
    blockOrg(store, 'o3', { code: 'log_type', stored: null, served: null, lh: null });
    expect(selectOrgBlock('o3')(store.getState())).toEqual({
      code: 'log_type',
      stored: null,
      served: null,
      lh: null,
      reason: 'outdated',
    });
    const key = Secret.of(new Uint8Array(32));
    const beforeKeys = store.getState().orgs;
    addKeys(store, 'o1', new Map());
    expect(store.getState().orgs).toBe(beforeKeys);
    addKeys(store, 'o1', new Map([['wg w 1', key]]));
    expect(orgEntry(store.getState().orgs, 'o1').keys.get('wg w 1')).toBe(key);

    recordWraps(
      store,
      'o1',
      new Map([
        ['s1', 'wrap_unauthorized'],
        ['s2', 'signature_invalid'],
      ]),
      [],
    );
    const once = store.getState().orgs;
    recordWraps(store, 'o1', new Map([['s1', 'wrap_unauthorized']]), ['s9']);
    expect(store.getState().orgs).toBe(once);
    recordWraps(store, 'o1', new Map([['s1', 'decrypt_failed']]), []);
    expect(orgEntry(store.getState().orgs, 'o1').refusedWraps.get('s1')).toBe('decrypt_failed');
    expect(wrapUnauthorized(orgEntry(store.getState().orgs, 'o1'))).toBe(1);
    recordWraps(store, 'o1', new Map(), ['s2']);
    expect(wrapUnauthorized(orgEntry(store.getState().orgs, 'o1'))).toBe(0);
    expect(orgEntry(store.getState().orgs, 'o1').refusedWraps.size).toBe(1);

    const platform = {} as VerifiedPlatformLog;
    setPlatform(store, platform);
    expect(store.getState().orgs.platform).toBe(platform);
  });

  it('events: insert, replace, LRU bound, touch and find', () => {
    const store = createStore();
    putEvents(store, 'a', []);
    expect(store.getState().events).toEqual({});
    putEvents(store, 'a', [entry('a', 2), entry('a', 1)]);
    putEvents(store, 'a', [{ ...entry('a', 2), status: 'verified' }]);
    expect(selectScopeEvents('a')(store.getState()).map((e) => [e.scopeSeq, e.status])).toEqual([
      [1, 'pending'],
      [2, 'verified'],
    ]);
    expect([...store.getState().events.a!.entries.keys()]).toEqual([1, 2]);
    touchEvents(store, 'a', [1, 7]);
    expect([...store.getState().events.a!.entries.keys()]).toEqual([2, 1]);
    const untouched = store.getState().events;
    touchEvents(store, 'zz', [1]);
    expect(store.getState().events).toBe(untouched);

    putEvents(
      store,
      'b',
      Array.from({ length: MAX_EVENTS + 2 }, (_, i) => entry('b', i + 1)),
    );
    const b = store.getState().events.b!.entries;
    expect(b.size).toBe(MAX_EVENTS);
    expect(b.has(1) || b.has(2)).toBe(false);
    expect(findEvents(store.getState().events, (e) => e.status === 'verified').map((e) => e.scopeSeq)).toEqual([2]);
  });

  it('jobs: queue status and the sync indicator', () => {
    const store = createStore();
    expect(syncing(store.getState().jobs)).toBe(false);
    setQueue(store, 'o1', { waiting: 1 });
    expect(syncing(selectJobs(store.getState()))).toBe(true);
    setQueue(store, 'o1', { waiting: 0, running: 'log-verify' });
    expect(syncing(store.getState().jobs)).toBe(true);
    setQueue(store, 'o1', { running: null, last: { name: 'log-verify', ok: true, code: null, at: 1 } });
    expect(syncing(store.getState().jobs)).toBe(false);
    expect(store.getState().jobs.o1).toEqual({
      running: null,
      waiting: 0,
      last: { name: 'log-verify', ok: true, code: null, at: 1 },
    });
  });
});

describe('selectors', () => {
  it('return stable snapshots for unchanged state', () => {
    const store = createStore();
    const select = selectScopeEvents('a');
    const empty = select(store.getState());
    expect(select(store.getState())).toBe(empty);
    putEvents(store, 'a', [entry('a', 1)]);
    const first = select(store.getState());
    setOnline(store, false);
    expect(select(store.getState())).toBe(first);
    putEvents(store, 'a', [entry('a', 2)]);
    expect(select(store.getState())).not.toBe(first);
  });

  it('memoize caches per input object', () => {
    let calls = 0;
    const derive = memoize((input: { n: number }) => {
      calls += 1;
      return input.n * 2;
    });
    const input = { n: 2 };
    expect([derive(input), derive(input), derive({ n: 3 })]).toEqual([4, 4, 6]);
    expect(calls).toBe(2);
  });
});
