// The platform browser side of the signed-in browser (06-web.md section 5.4b): the watched browsers the live channel
// of the active org follows (`browser=`, `browser_kbps`), the hand-over of browser frames to the views, and the
// context of the Browser tab (reader without storage and the watch).
import { IDBFactory } from 'fake-indexeddb';
import { describe, expect, it, vi } from 'vitest';
import { type BrowserListener, BrowserWatchList } from '../../../src/core/browser/watchlist';
import { preparePrivateOrg } from '../../../src/core/account/genesis';
import { signIn } from '../../../src/core/account/login';
import { Prefs, type PrefStorage } from '../../../src/core/account/prefs';
import { register } from '../../../src/core/account/register';
import { AppSession } from '../../../src/core/account/session';
import type { LiveConnection, LiveWiring } from '../../../src/core/net/live';
import { Persist } from '../../../src/core/persist';
import { createStore } from '../../../src/core/store';
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
  const lists: [string[], number][] = [];
  const wirings: LiveWiring[] = [];
  const live = vi.fn(async (wiring: LiveWiring): Promise<LiveConnection> => {
    wirings.push(wiring);
    const setBrowsers = (list: string[], kbps: number) => lists.push([list, kbps]);
    return { channel: { setBrowsers } as never, stop: () => undefined };
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

const listener = (): BrowserListener & Record<string, ReturnType<typeof vi.fn>> => ({
  action: vi.fn(),
  live: vi.fn(),
  resumed: vi.fn(),
  refused: vi.fn(),
});

const watches = (app: AppSession) => app.browserWatches((changed) => new BrowserWatchList(changed));

describe('watched browsers of the app session', () => {
  it('passes the watched browsers and the largest wish of the active org to its channel until released', async () => {
    const { app, lists, wirings, org } = await setup();
    const a = watches(app).watch(org, 'a', listener(), 250);
    watches(app).watch('other-org', 'x', listener(), 3000);
    expect(lists).toEqual([]);
    app.activate(org);
    expect(wirings[0]!.browsers).toEqual(['a']);
    expect(wirings[0]!.browserKbps).toBe(250);
    const b = app.browser.watches((changed) => new BrowserWatchList(changed)).watch(org, 'b', listener(), 1000);
    await vi.waitFor(() => expect(lists.at(-1)).toEqual([['a', 'b'], 1000]));
    b.setKbps(3000);
    await vi.waitFor(() => expect(lists.at(-1)).toEqual([['a', 'b'], 3000]));
    b.release();
    a.release();
    await vi.waitFor(() => expect(lists.at(-1)).toEqual([[], 1000]));
  });

  it('hands browser frames to the views and the rest to the store', async () => {
    const { app, wirings, org } = await setup();
    const view = listener();
    watches(app).watch(org, 's', view, 1000);
    app.activate(org);
    wirings[0]!.onFrame!('browser_live', { session_id: 's', frame: {} });
    wirings[0]!.onFrame!('hello', { stream_id: 'x', heads: {}, hub_time: '' });
    wirings[0]!.onFrame!('browsers_refused', { sessions: ['s'] });
    expect(view.live).toHaveBeenCalledTimes(1);
    expect(view.resumed).toHaveBeenCalledTimes(1);
    expect(view.refused).toHaveBeenCalledTimes(1);
  });

  it('gives the Browser tab a reader without storage', async () => {
    const { app, hub, store, org } = await setup();
    const event = hub.orgs.get(org)!.events[0];
    const before = store.getState().events;
    expect(app.browser.client).toBe(app.client);
    const entries = await app.browser.read(org, org, [{ scope_seq: 1, received_at: null, event }]);
    expect(entries[0]).toMatchObject({ org, scopeId: org, scopeSeq: 1, status: 'verified' });
    expect(store.getState().events).toBe(before);
    expect(app.browser.store).toBe(store);
    await app.signOut();
  });
});
