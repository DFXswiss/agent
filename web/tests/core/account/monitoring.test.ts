// The monitoring view of the signed-in browser (06-web.md section 9.1, 08-monitoring.md section 6): `metrics` frames
// are requested (`monitoring=1`) only while at least one monitoring view of the active org is open.
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
  const flags: boolean[] = [];
  const wirings: LiveWiring[] = [];
  const live = vi.fn(async (wiring: LiveWiring): Promise<LiveConnection> => {
    wirings.push(wiring);
    return { channel: { setMonitoring: (on: boolean) => flags.push(on) } as never, stop: () => undefined };
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
  return { app, flags, wirings, org: prepared.org };
}

describe('monitoring views of the app session', () => {
  it('opens the active channel with the flag while a view of its org is open', async () => {
    const { app, flags, wirings, org } = await setup();
    const release = app.watchMonitoring(org);
    app.watchMonitoring('other-org');
    expect(flags).toEqual([]);
    app.activate(org);
    expect(wirings.at(-1)!.monitoring).toBe(true);
    release();
    release();
    await vi.waitFor(() => expect(flags.at(-1)).toBe(false));
  });

  it('keeps the flag until the last view closes', async () => {
    const { app, flags, wirings, org } = await setup();
    app.activate(org);
    expect(wirings.at(-1)!.monitoring).toBe(false);
    const first = app.watchMonitoring(org);
    const second = app.watchMonitoring(org);
    await vi.waitFor(() => expect(flags).toEqual([true, true]));
    // the channel ignores an unchanged flag; the session passes the current one after every change
    first();
    await vi.waitFor(() => expect(flags).toEqual([true, true, true]));
    second();
    await vi.waitFor(() => expect(flags.at(-1)).toBe(false));
  });

  it('does nothing without a channel', async () => {
    const { app, flags, org } = await setup();
    app.watchMonitoring(org)();
    expect(flags).toEqual([]);
  });
});
