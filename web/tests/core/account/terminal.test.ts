// The terminal side of the signed-in browser (06-web.md section 5.5): the watch list that the live channel of the
// active org follows (`terminal=`), refused sessions, and reading pulled chunks with the event reader without storing.
import { IDBFactory } from 'fake-indexeddb';
import { describe, expect, it, vi } from 'vitest';
import { preparePrivateOrg } from '../../../src/core/account/genesis';
import { signIn } from '../../../src/core/account/login';
import { Prefs, type PrefStorage } from '../../../src/core/account/prefs';
import { register } from '../../../src/core/account/register';
import { AppSession } from '../../../src/core/account/session';
import type { LiveConnection, LiveWiring } from '../../../src/core/net/live';
import { Persist } from '../../../src/core/persist';
import { createStore } from '../../../src/core/store';
import { show } from '../../../src/core/store/focus';
import { FakeAuthenticator } from './authenticator';
import { CONFIG, ORIGIN, SimHub, T0 } from './hub';

class MemoryStorage implements PrefStorage {
  readonly items = new Map<string, string>();
  getItem(key: string) {
    return this.items.get(key) ?? null;
  }
  setItem(key: string, value: string) {
    this.items.set(key, value);
  }
  removeItem(key: string) {
    this.items.delete(key);
  }
}

async function setup() {
  const hub = new SimHub();
  const auth = new FakeAuthenticator();
  const store = createStore();
  const persist = await Persist.open(new IDBFactory());
  const lists: string[][] = [];
  const wirings: LiveWiring[] = [];
  const live = vi.fn(async (wiring: LiveWiring): Promise<LiveConnection> => {
    wirings.push(wiring);
    return { channel: { setTerminals: (list: string[]) => lists.push(list) } as never, stop: () => undefined };
  });
  const app = new AppSession({
    store,
    fetch: hub.fetch,
    config: CONFIG,
    rootKey: CONFIG.platform_root_key,
    persist,
    prefs: new Prefs(new MemoryStorage()),
    now: () => T0,
    live,
  });
  const invite = hub.addInvite();
  const registration = await register(app.anon, auth, invite);
  await app.begin(await signIn({ anon: app.anon, credentials: auth, origin: ORIGIN, now: () => T0 }));
  await app.unlock(registration.identity);
  const prepared = await preparePrivateOrg({
    userId: registration.userId,
    identity: registration.identity,
    displayName: 'Ada',
    locale: 'en',
    platformInviteId: await hub.idOf(invite),
    now: () => T0,
  });
  await app.client.request('POST', '/v1/orgs', { body: prepared.body });
  await app.installOrg(prepared);
  return { hub, app, store, lists, wirings, org: prepared.org };
}

describe('watched terminals of the app session', () => {
  it('passes the watched sessions of the active org to its channel, newest last, until released', async () => {
    const { app, lists, wirings, org } = await setup();
    const releaseA = app.watchTerminal(org, 'a');
    app.watchTerminal('other-org', 'x');
    expect(lists).toEqual([]);
    app.activate(org);
    expect(wirings[0]!.terminals).toEqual(['a']);
    const releaseB = app.watchTerminal(org, 'b');
    const releaseA2 = app.terminal.watch(org, 'a');
    await vi.waitFor(() => expect(lists.at(-1)).toEqual(['b', 'a']));
    // one of two views of `a` closes (twice): still watched
    releaseA();
    releaseA();
    await new Promise((resolve) => setTimeout(resolve, 10));
    expect(lists).toHaveLength(2);
    releaseA2();
    await vi.waitFor(() => expect(lists.at(-1)).toEqual(['b']));
    releaseB();
    await vi.waitFor(() => expect(lists.at(-1)).toEqual([]));
  });

  it('drops sessions the hub refused and applies the frame to the store', async () => {
    const { app, store, wirings, lists, org } = await setup();
    const release = app.watchTerminal(org, 'gone');
    app.watchTerminal(org, 'kept');
    app.activate(org);
    store.update('terminal', () => ({
      sessions: { gone: { session: 'gone', status: 'loading', code: null } as never },
    }));
    wirings[0]!.onFrame!('terminals_refused', { sessions: ['gone', 7] });
    wirings[0]!.onFrame!('terminals_refused', null);
    expect(store.getState().terminal.sessions.gone).toMatchObject({ status: 'error', code: 'not_found' });
    release();
    app.watchTerminal(org, 'new');
    await vi.waitFor(() => expect(lists.at(-1)).toEqual(['kept', 'new']));
  });

  it('makes the mounted views read their newest page after resync', async () => {
    const { app, store, wirings, org } = await setup();
    app.activate(org);
    const refresh = vi.fn();
    const release = show(store, { scope: org, task: 't1', refresh });
    wirings[0]!.onFrame!('task', {});
    expect(refresh).not.toHaveBeenCalled();
    wirings[0]!.onFrame!('resync', { reason: 'buffer_overflow' });
    expect(refresh).toHaveBeenCalledTimes(1);
    release();
  });

  it('reads pulled events with the reader and stores nothing', async () => {
    const { app, hub, store, org } = await setup();
    const event = hub.orgs.get(org)!.events[0];
    const before = store.getState().events;
    const entries = await app.terminal.read(org, org, [{ scope_seq: 1, received_at: null, event }]);
    expect(entries).toHaveLength(1);
    expect(entries[0]).toMatchObject({ org, scopeId: org, scopeSeq: 1, status: 'verified' });
    expect(store.getState().events).toBe(before);
    expect(app.terminal.now()).toBe(T0);
    await app.signOut();
  });
});
