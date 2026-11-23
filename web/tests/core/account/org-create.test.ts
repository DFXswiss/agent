// U-WEB-ui-orgs (core part, WEB-06): a further, non-private organization (06-web.md section 7.5, ADR-026) through the
// genesis builder, and the `interval` trigger of the signed-in browser (06-web.md section 11: catch-up and pending
// rotations every 10 minutes), with an injected and with the default timer.
import { IDBFactory } from 'fake-indexeddb';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { prepareOrg, preparePrivateOrg } from '../../../src/core/account/genesis';
import { signIn } from '../../../src/core/account/login';
import { Prefs, type PrefStorage } from '../../../src/core/account/prefs';
import { openEvent } from '../../../src/core/crypto/event';
import { register } from '../../../src/core/account/register';
import { AppSession } from '../../../src/core/account/session';
import type { JobRegistration, Trigger } from '../../../src/core/jobs';
import { INTERVAL_MS } from '../../../src/core/jobs/catch-up';
import { Persist } from '../../../src/core/persist';
import { createStore } from '../../../src/core/store';
import { FakeAuthenticator } from './authenticator';
import { CONFIG, ORIGIN, SimHub, T0 } from './hub';

afterEach(() => vi.useRealTimers());

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

type Timer = { start: (tick: () => void, ms: number) => unknown; stop: (handle: unknown) => void };

async function signedIn(options: { timer?: Timer } = {}) {
  const hub = new SimHub();
  const auth = new FakeAuthenticator();
  const runs: { trigger: Trigger; org: string }[] = [];
  const probe: JobRegistration = {
    name: 'probe',
    order: 1,
    triggers: ['login', 'interval', 'log'],
    run: async (_context, run) => {
      runs.push({ trigger: run.trigger, org: run.org });
    },
  };
  const app = new AppSession({
    store: createStore(),
    fetch: hub.fetch,
    config: CONFIG,
    rootKey: CONFIG.platform_root_key,
    persist: await Persist.open(new IDBFactory()),
    prefs: new Prefs(new MemoryStorage()),
    now: () => T0,
    jobs: [probe],
    ...(options.timer && { timer: options.timer }),
  });
  const invite = hub.addInvite();
  const registration = await register(app.anon, auth, invite);
  const login = await signIn({ anon: app.anon, credentials: auth, origin: ORIGIN, now: () => T0 });
  await app.begin(login);
  const prepared = await preparePrivateOrg({
    userId: registration.userId,
    identity: registration.identity,
    displayName: 'Ada',
    locale: 'en',
    platformInviteId: await hub.idOf(invite),
    now: () => T0,
  });
  return { hub, app, registration, prepared, runs };
}

describe('prepareOrg', () => {
  it('builds a non-private genesis with the chosen name and the localized default workgroup', async () => {
    const { app, registration } = await signedIn();
    await app.unlock(registration.identity);
    const prepared = await prepareOrg({
      userId: registration.userId,
      identity: registration.identity,
      displayName: ' Ada ',
      orgName: ' Acme AG ',
      locale: 'de',
      now: () => T0,
    });
    const body = prepared.body as { genesis: { body: Record<string, unknown> }; platform_invite_id?: string };
    expect(body.genesis.body).toMatchObject({ type: 'genesis', private: false, default_wg: prepared.defaultWg });
    expect(body.platform_invite_id).toBeUndefined();
    expect(prepared.log.state.private).toBe(false);
    await app.client.request('POST', '/v1/orgs', { body: prepared.body });
    await app.installOrg(prepared);
    expect(app.store.getState().orgs.byId[prepared.org]!.log!.head).toEqual(prepared.log.head);
    // the names are inside the sealed events (opened by the reader like any other event)
    const key = prepared.keys.get(`org ${prepared.org} 1`)!;
    const events = (prepared.body as { events: { header: { kind: string } }[] }).events;
    const texts = await Promise.all(
      events.map(async (event) =>
        JSON.parse(new TextDecoder().decode(await openEvent(key, event, registration.identity.sigPublic()))),
      ),
    );
    expect(texts).toEqual([{ name: 'Acme AG' }, { name: 'Standard' }, { display_name: 'Ada' }]);
  });

  it('refuses empty or long org names', async () => {
    const { registration } = await signedIn();
    const input = {
      userId: registration.userId,
      identity: registration.identity,
      displayName: 'Ada',
      locale: 'en' as const,
    };
    await expect(prepareOrg({ ...input, orgName: '  ' })).rejects.toMatchObject({ code: 'header_invalid' });
    await expect(prepareOrg({ ...input, orgName: 'x'.repeat(81) })).rejects.toMatchObject({ code: 'header_invalid' });
  });
});

describe('the interval trigger', () => {
  it('starts once after unlock, runs the interval jobs for every org and stops at sign-out', async () => {
    const timer = {
      start: vi.fn<Timer['start']>(() => 42),
      stop: vi.fn<Timer['stop']>(),
    };
    const { app, hub, registration, prepared, runs } = await signedIn({ timer });
    await app.unlock(registration.identity);
    await app.client.request('POST', '/v1/orgs', { body: prepared.body });
    await app.installOrg(prepared);
    expect(timer.start).toHaveBeenCalledTimes(1);
    expect(timer.start.mock.calls[0]![1]).toBe(INTERVAL_MS);
    // a second unlock (renewal) keeps the one timer
    await app.unlock(registration.identity);
    expect(timer.start).toHaveBeenCalledTimes(1);
    runs.length = 0;
    const tick = timer.start.mock.calls[0]![0];
    tick();
    await vi.waitFor(() => expect(runs).toEqual([{ trigger: 'interval', org: prepared.org }]));
    await app.signOut();
    expect(timer.stop).toHaveBeenCalledWith(42);
    expect(hub.users.size).toBe(1);
  });

  it('uses setInterval by default', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
    const { app, registration, prepared, runs } = await signedIn();
    await app.unlock(registration.identity);
    await app.client.request('POST', '/v1/orgs', { body: prepared.body });
    await app.installOrg(prepared);
    runs.length = 0;
    vi.advanceTimersByTime(INTERVAL_MS);
    await vi.waitFor(() => expect(runs).toHaveLength(1));
    await app.signOut();
    vi.advanceTimersByTime(INTERVAL_MS);
    expect(runs).toHaveLength(1);
  });
});
