import { IDBFactory } from 'fake-indexeddb';
import { describe, expect, it, vi } from 'vitest';
import { preparePrivateOrg } from '../../../src/core/account/genesis';
import { signIn, unlockWithRecoveryCode } from '../../../src/core/account/login';
import { Prefs, type PrefStorage } from '../../../src/core/account/prefs';
import { register } from '../../../src/core/account/register';
import { AppSession, exclusiveOf, meOrgs, RENEW_EVERY_MS, settingsLocale } from '../../../src/core/account/session';
import { Identity } from '../../../src/core/crypto/keys';
import type { LiveConnection, LiveWiring } from '../../../src/core/net/live';
import { Persist } from '../../../src/core/persist';
import { DeviceKeys } from '../../../src/core/persist/device-key';
import { createStore } from '../../../src/core/store';
import { setProfile } from '../../../src/core/store/slices/me';
import { setActiveOrg, setLocale, setPhase, setPrfMissing } from '../../../src/core/store/slices/session';
import { FakeAuthenticator, giveRecoveryCode } from './authenticator';
import { CONFIG, HubFailure, ORIGIN, RENEW_MS, SimHub, T0 } from './hub';

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

async function setup(options: { now?: () => number; live?: (wiring: LiveWiring) => Promise<LiveConnection> } = {}) {
  const hub = new SimHub();
  const auth = new FakeAuthenticator();
  const store = createStore();
  const persist = await Persist.open(new IDBFactory());
  const storage = new MemoryStorage();
  const app = new AppSession({
    store,
    fetch: hub.fetch,
    config: CONFIG,
    rootKey: CONFIG.platform_root_key,
    persist,
    prefs: new Prefs(storage),
    now: options.now ?? (() => T0),
    ...(options.live && { live: options.live }),
  });
  return { hub, auth, store, persist, storage, app };
}

/** Registration, sign-in and the private org through the real flows. */
async function onboarded(context: Awaited<ReturnType<typeof setup>>) {
  const { hub, auth, app } = context;
  const invite = hub.addInvite();
  const registration = await register(app.anon, auth, invite);
  const login = await signIn({ anon: app.anon, credentials: auth, origin: ORIGIN, now: () => T0 });
  await app.begin(login);
  await app.unlock(registration.identity);
  expect(await app.uploadPasskeySlot(login, registration.identity)).toBe(true);
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
  return { registration, login, prepared };
}

describe('session slice', () => {
  it('changes only what differs', () => {
    const store = createStore();
    const listener = vi.fn();
    store.subscribe(listener);
    setPhase(store, 'starting');
    expect(listener).not.toHaveBeenCalled();
    setPhase(store, 'locked');
    setLocale(store, 'de');
    setActiveOrg(store, 'o');
    setPrfMissing(store, true);
    expect(store.getState().session).toEqual({
      phase: 'locked',
      locale: 'de',
      activeOrg: 'o',
      prfMissing: true,
    });
    expect(listener).toHaveBeenCalledTimes(4);
  });
});

describe('Prefs', () => {
  it('keeps the last org per user and drops the old recovery reminder', () => {
    const storage = new MemoryStorage();
    const prefs = new Prefs(storage);
    expect(prefs.lastOrg('u')).toBeNull();
    prefs.setLastOrg('u', 'o');
    expect(prefs.lastOrg('u')).toBe('o');
    // a flag stored before KOR-22 (the skipped code) is removed
    storage.setItem('agent-platform.recovery-unconfirmed.u', '1');
    prefs.forgetRecoveryReminder('u');
    expect(storage.items.has('agent-platform.recovery-unconfirmed.u')).toBe(false);
  });

  it('keeps "Stay signed in" on unless it was switched off', () => {
    const storage = new MemoryStorage();
    const prefs = new Prefs(storage);
    expect(prefs.staySignedIn()).toBe(true);
    prefs.setStaySignedIn(false);
    expect(storage.items.get('agent-platform.stay-signed-in')).toBe('off');
    expect(prefs.staySignedIn()).toBe(false);
    prefs.setStaySignedIn(true);
    expect(storage.items.size).toBe(0);
    expect(new Prefs(null).staySignedIn()).toBe(true);
  });

  it('behaves as empty without or with a refusing storage', () => {
    const none = new Prefs(null);
    none.setLastOrg('u', 'o');
    expect(none.lastOrg('u')).toBeNull();
    const refusing = new Prefs({
      getItem: () => {
        throw new Error('denied');
      },
      setItem: () => {
        throw new Error('denied');
      },
      removeItem: () => {
        throw new Error('denied');
      },
    });
    refusing.setLastOrg('u', 'o');
    refusing.forgetRecoveryReminder('u');
    expect(refusing.lastOrg('u')).toBeNull();
  });
});

describe('profile helpers', () => {
  it('reads orgs and locale defensively', () => {
    expect(meOrgs(null)).toEqual([]);
    expect(meOrgs({ orgs: 'x' })).toEqual([]);
    const org = { org_id: 'o', role: 'admin', private: true, default_wg: 'w' };
    expect(meOrgs({ orgs: [org, { org_id: 1 }, null] })).toEqual([org]);
    expect(settingsLocale({ settings: { locale: 'de' } })).toBe('de');
    expect(settingsLocale({ settings: { locale: 'fr' } })).toBeNull();
    expect(settingsLocale({ settings: null })).toBeNull();
    expect(settingsLocale(null)).toBeNull();
  });
});

/** A timer whose ticks the test runs by hand. */
function manualTimer() {
  const ticks = new Map<number, () => void>();
  let next = 0;
  return {
    ticks,
    timer: {
      start: (tick: () => void) => {
        next += 1;
        ticks.set(next, tick);
        return next;
      },
      stop: (handle: unknown) => void ticks.delete(handle as number),
    },
  };
}

describe('AppSession: stay signed in (rule E-40)', () => {
  /** An onboarded browser whose app has this browser's device keys; `again` is a reload of it (same IndexedDB). */
  async function staying(options: { stay?: boolean; now?: () => number } = {}) {
    const context = await setup(options.now === undefined ? {} : { now: options.now });
    const devices = new IDBFactory();
    const keys = await DeviceKeys.open(devices);
    const manual = manualTimer();
    const prefs = new Prefs(context.storage);
    if (options.stay === false) prefs.setStaySignedIn(false);
    const make = (extra: Partial<ConstructorParameters<typeof AppSession>[0]> = {}) =>
      new AppSession({
        store: createStore(),
        fetch: context.hub.fetch,
        config: CONFIG,
        rootKey: CONFIG.platform_root_key,
        persist: context.persist,
        prefs,
        now: options.now ?? (() => T0),
        timer: manual.timer,
        device: async () => keys,
        ...extra,
      });
    const app = make();
    const ctx = { ...context, app, store: app.store };
    const flows = await onboarded(ctx);
    await app.renewIfDue();
    return { ...ctx, ...flows, keys, manual, prefs, again: make };
  }

  it('keeps the identity after an unlock and restores straight to unlocked', async () => {
    const { app, keys, again, registration, prepared, hub } = await staying();
    expect(app.staysSignedIn).toBe(true);
    expect(await keys.info(registration.userId)).not.toBeNull();
    const reloaded = again();
    await reloaded.restore();
    const state = reloaded.store.getState();
    expect(state.session.phase).toBe('unlocked');
    expect(state.me.identity!.public()).toEqual(registration.identity.public());
    expect(state.orgs.byId[prepared.org]!.keys.size).toBe(2);
    expect(reloaded.staysSignedIn).toBe(true);
    // no passkey ceremony was needed
    expect(hub.log.filter((entry) => entry.target === '/v1/passkeys/login/begin')).toHaveLength(1);
  });

  it('with "Stay signed in" off forgets the entry and restores locked as before', async () => {
    const { app, keys, again, registration, hub } = await staying({ stay: false });
    expect(app.staysSignedIn).toBe(false);
    expect(await keys.info(registration.userId)).toBeNull();
    expect(hub.renewals).toEqual([]);
    const reloaded = again();
    await reloaded.restore();
    expect(reloaded.store.getState().session.phase).toBe('locked');
  });

  it('stays locked without device keys, without an entry or when the hub cannot be reached', async () => {
    const { again, keys, registration, hub } = await staying();
    const none = again({ device: () => Promise.reject(new Error('no IndexedDB')) });
    await none.restore();
    expect(none.store.getState().session.phase).toBe('locked');
    hub.override.set('GET /v1/me', new HubFailure(503, 'unavailable'));
    const offline = again();
    await offline.restore();
    expect(offline.store.getState().session.phase).toBe('locked');
    // the entry stays: the next start unlocks again
    expect(await keys.info(registration.userId)).not.toBeNull();
    hub.override.delete('GET /v1/me');
    await keys.forget(registration.userId);
    const forgotten = again();
    await forgotten.restore();
    expect(forgotten.store.getState().session.phase).toBe('locked');
    expect(forgotten.staysSignedIn).toBe(false);
  });

  it('deletes an entry that does not open or holds another identity', async () => {
    const { again, keys, registration } = await staying();
    vi.spyOn(keys, 'unlock').mockRejectedValueOnce(new Error('decrypt_failed'));
    const tampered = again();
    await tampered.restore();
    expect(tampered.store.getState().session.phase).toBe('locked');
    expect(await keys.info(registration.userId)).toBeNull();
    await keys.remember(registration.userId, await Identity.generate());
    const other = again();
    await other.restore();
    expect(other.store.getState().session.phase).toBe('locked');
    expect(await keys.info(registration.userId)).toBeNull();
  });

  it('keeps nothing when the device key cannot be stored', async () => {
    const context = await setup();
    const app = new AppSession({
      store: context.store,
      fetch: context.hub.fetch,
      config: CONFIG,
      rootKey: CONFIG.platform_root_key,
      persist: context.persist,
      prefs: new Prefs(null),
      now: () => T0,
      device: async () => ({
        remember: () => Promise.reject(new Error('quota')),
        unlock: async () => null,
        forget: async () => undefined,
      }),
    });
    await onboarded({ ...context, app });
    expect(app.staysSignedIn).toBe(false);
    // "Stay signed in" off with an IndexedDB that refuses to delete: the unlock and the sign-out still go through
    const refusing = await setup();
    const forget = vi.fn(() => Promise.reject(new Error('blocked')));
    const prefs = new Prefs(refusing.storage);
    prefs.setStaySignedIn(false);
    const off = new AppSession({
      store: refusing.store,
      fetch: refusing.hub.fetch,
      config: CONFIG,
      rootKey: CONFIG.platform_root_key,
      persist: refusing.persist,
      prefs,
      now: () => T0,
      device: async () => ({ remember: async () => ({ userId: '', createdAt: 0 }), unlock: async () => null, forget }),
    });
    await onboarded({ ...refusing, app: off });
    await off.signOut();
    expect(forget).toHaveBeenCalledTimes(2);
    expect(off.store.getState().session.phase).toBe('anonymous');
  });

  it('renews the session right after the sign-in and then at most once an hour', async () => {
    let now = T0;
    const { app, hub, persist, login, manual, store } = await staying({ now: () => now });
    expect(hub.renewals).toEqual([login.keyId]);
    const stored = (await persist.loadSession())!;
    expect(stored.expiresAt).toBe(T0 + RENEW_MS);
    expect(stored.renewedAt).toBe(T0);
    expect(store.getState().me.session!.expiresAt).toBe(T0 + RENEW_MS);
    // the minute check: not due yet
    now = T0 + RENEW_EVERY_MS - 1;
    hub.now = now;
    for (const tick of manual.ticks.values()) tick();
    await app.renewIfDue();
    expect(hub.renewals).toHaveLength(1);
    now = T0 + RENEW_EVERY_MS;
    hub.now = now;
    await app.renewIfDue();
    expect(hub.renewals).toHaveLength(2);
    expect((await persist.loadSession())!.expiresAt).toBe(now + RENEW_MS);
    expect(store.getState().connection.session).toBe('ok');
  });

  it('renews a session about to end at once and runs one renewal per tab at a time', async () => {
    const now = T0;
    const { app, hub, persist } = await staying({ now: () => now });
    const stored = (await persist.loadSession())!;
    await persist.saveSession({ ...stored, expiresAt: T0 + 60_000 });
    const first = app.renewIfDue();
    const second = app.renewIfDue();
    expect(second).toBe(first);
    await first;
    expect(hub.renewals).toHaveLength(2);
    expect((await persist.loadSession())!.expiresAt).toBe(now + RENEW_MS);
  });

  it('takes over a session another tab renewed or signed in, and ignores another user', async () => {
    const { app, hub, persist, store, auth } = await staying();
    const stored = (await persist.loadSession())!;
    // another tab renewed: this tab takes the later end without asking the hub
    await persist.saveSession({ ...stored, expiresAt: stored.expiresAt + 5_000 });
    await app.renewIfDue();
    expect(store.getState().me.session!.expiresAt).toBe(stored.expiresAt + 5_000);
    expect(hub.renewals).toHaveLength(1);
    // another tab signed in again (new key): this tab signs with it from now on
    const login = await signIn({ anon: app.anon, credentials: auth, origin: ORIGIN, now: () => T0 });
    await persist.saveSession({
      ...stored,
      keyId: login.keyId,
      privateKey: login.privateKey,
      publicKey: login.publicKey,
    });
    await app.renewIfDue();
    expect(store.getState().me.session!.keyId).toBe(login.keyId);
    await app.loadMe();
    // another user's session in IndexedDB: nothing to take over
    await persist.saveSession({ ...stored, userId: 'someone-else' });
    await app.renewIfDue();
    expect(store.getState().me.session!.keyId).toBe(login.keyId);
  });

  it('leaves the session as it is when the renewal fails', async () => {
    let now = T0;
    const { app, hub, store } = await staying({ now: () => now });
    now = T0 + RENEW_EVERY_MS;
    hub.now = now;
    hub.override.set('POST /v1/session/renew', () => ({ key_id: 'session.other', expires_at: 'x' }));
    await app.renewIfDue();
    expect(hub.log.at(-1)!.target).toBe('/v1/session/renew');
    expect(store.getState().me.session!.expiresAt).toBe(T0 + RENEW_MS);
    hub.override.set('POST /v1/session/renew', new HubFailure(401, 'session_expired'));
    await app.renewIfDue();
    expect(store.getState().connection.session).toBe('expired');
  });

  it('stores nothing again when the tab signed out while the hub renewed', async () => {
    let now = T0;
    const { app, hub, persist, login } = await staying({ now: () => now });
    now = T0 + RENEW_EVERY_MS;
    hub.now = now;
    let release = () => undefined as void;
    hub.override.set('POST /v1/session/renew', async () => {
      await new Promise<void>((resolve) => (release = resolve));
      return { key_id: login.keyId, expires_at: '2026-12-21T12:00:00.000Z' };
    });
    const pending = app.renewIfDue();
    await vi.waitFor(() => expect(hub.log.at(-1)!.target).toBe('/v1/session/renew'));
    await app.signOut();
    release();
    await pending;
    expect(await persist.loadSession()).toBeNull();
  });

  it('starts and stops renewing when the settings change the entry, and not without a session', async () => {
    const { app, manual, hub } = await staying();
    app.noteRemembered(false);
    expect(app.staysSignedIn).toBe(false);
    expect(manual.ticks.size).toBe(1); // the interval trigger of the jobs only
    await app.renewIfDue();
    expect(hub.renewals).toHaveLength(1);
    app.noteRemembered(true);
    app.noteRemembered(true);
    expect(manual.ticks.size).toBe(2);
    await app.signOut();
    expect(manual.ticks.size).toBe(0);
    await app.renewIfDue();
    expect(hub.renewals).toHaveLength(1);
  });

  it('runs a renewal under the lock of the page, or directly without one', async () => {
    const request = vi.fn(async (_name: string, run: () => Promise<unknown>) => run());
    const exclusive = exclusiveOf({ request });
    await expect(exclusive(async () => 7)).resolves.toBe(7);
    expect(request).toHaveBeenCalledWith('agent-platform.session-renew', expect.any(Function));
    await expect(exclusiveOf(undefined)(async () => 8)).resolves.toBe(8);
    const { again, hub } = await staying();
    const locked = again({ exclusive });
    await locked.restore();
    expect(request).toHaveBeenCalledTimes(2);
    expect(hub.renewals).toHaveLength(1);
  });
});

describe('AppSession', () => {
  it('starts anonymous without a stored session and clears an expired one', async () => {
    const context = await setup();
    await context.app.restore();
    expect(context.store.getState().session.phase).toBe('anonymous');
    const login = await (async () => {
      const invite = context.hub.addInvite();
      await register(context.app.anon, context.auth, invite);
      return signIn({ anon: context.app.anon, credentials: context.auth, origin: ORIGIN });
    })();
    await context.app.begin(login);
    expect(await context.persist.loadSession()).not.toBeNull();
    const later = new AppSession({
      store: createStore(),
      fetch: context.hub.fetch,
      config: CONFIG,
      rootKey: CONFIG.platform_root_key,
      persist: context.persist,
      prefs: new Prefs(null),
      now: () => T0 + 43080_000,
    });
    vi.spyOn(context.persist, 'clearSession').mockRejectedValueOnce(new Error('blocked'));
    await later.restore();
    expect(later.store.getState().session.phase).toBe('anonymous');
    await later.restore();
    expect(await context.persist.loadSession()).toBeNull();
  });

  it('treats an unreadable persistence as no session', async () => {
    const context = await setup();
    const app = new AppSession({
      store: createStore(),
      fetch: context.hub.fetch,
      config: CONFIG,
      rootKey: CONFIG.platform_root_key,
      persist: { ...context.persist, loadSession: () => Promise.reject(new Error('blocked')) } as never,
      prefs: new Prefs(null),
    });
    await app.restore();
    expect(app.store.getState().session.phase).toBe('anonymous');
  });

  it('registers, unlocks, creates the private org and restores a locked session after a reload', async () => {
    const context = await setup();
    const { app, store, hub, persist } = context;
    const { registration, prepared } = await onboarded(context);
    const state = store.getState();
    expect(state.session.phase).toBe('unlocked');
    expect(state.me.identity).toBe(registration.identity);
    expect(state.orgs.byId[prepared.org]!.log!.head.seq).toBe(0);
    expect(state.orgs.byId[prepared.org]!.keys.size).toBe(2);
    expect(await persist.loadPinned(prepared.org)).toEqual({
      founder: registration.identity.public().sig,
      head: prepared.log.head,
    });
    expect(app.landingOrg()).toBe(prepared.org);
    expect(context.storage.items.get(`agent-platform.last-org.${registration.userId}`)).toBe(prepared.org);
    // a reload: the stored session key works, the identity is gone
    const reloaded = new AppSession({
      store: createStore(),
      fetch: hub.fetch,
      config: CONFIG,
      rootKey: CONFIG.platform_root_key,
      persist,
      prefs: new Prefs(context.storage),
      now: () => T0,
    });
    await reloaded.restore();
    expect(reloaded.store.getState().session.phase).toBe('locked');
    expect(reloaded.userId).toBe(registration.userId);
    const me = await reloaded.loadMe();
    expect(meOrgs(me).map((org) => org.org_id)).toEqual([prepared.org]);
    // unlock again with the passkey (PRF) runs the log and wrap jobs of every org
    const login = await signIn({ anon: reloaded.anon, credentials: context.auth, origin: ORIGIN, now: () => T0 });
    await reloaded.begin(login);
    expect(login.bundleCt).not.toBeNull();
    const identity = (await import('../../../src/core/account/login')).unlockWithPrf(login);
    await reloaded.unlock((await identity)!);
    const org = reloaded.store.getState().orgs.byId[prepared.org]!;
    expect(org.blocked).toBeNull();
    expect(org.log!.head).toEqual(prepared.log.head);
    expect(org.keys.size).toBe(2);
    expect(reloaded.store.getState().session.phase).toBe('unlocked');
  });

  it('refuses an identity the hub did not register and a profile of someone else', async () => {
    const context = await setup();
    const { app, hub, auth } = context;
    await register(app.anon, auth, hub.addInvite());
    const login = await signIn({ anon: app.anon, credentials: auth, origin: ORIGIN });
    await app.begin(login);
    await expect(app.unlock(await Identity.generate())).rejects.toMatchObject({ code: 'decrypt_failed' });
    expect(context.store.getState().session.phase).toBe('locked');
    hub.override.set('GET /v1/me', () => ({ user_id: 'other' }));
    await expect(app.loadMe()).rejects.toMatchObject({ code: 'header_invalid' });
  });

  it('unlocks with the recovery bundle and records the prf_missing event', async () => {
    const context = await setup();
    const { app, hub, auth } = context;
    auth.prf = false;
    const registration = await register(app.anon, auth, hub.addInvite());
    const login = await signIn({ anon: app.anon, credentials: auth, origin: ORIGIN });
    await app.begin(login);
    app.notePrf(login, false);
    expect(context.store.getState().session.prfMissing).toBe(true);
    expect(await app.uploadPasskeySlot(login, registration.identity)).toBe(false);
    const code = await giveRecoveryCode(hub, registration);
    const bundle = await app.recoveryBundle();
    const identity = await unlockWithRecoveryCode(registration.userId, code, bundle);
    await app.unlock(identity);
    expect(context.store.getState().session.phase).toBe('unlocked');
    app.notePrf({ ...login, prf: null }, true);
    expect(context.store.getState().session.prfMissing).toBe(false);
    hub.override.set('GET /v1/me/bundles/recovery', () => ({}));
    await expect(app.recoveryBundle()).rejects.toMatchObject({ code: 'header_invalid' });
  });

  it('keeps the last org per user and drops an old recovery reminder at sign-in', async () => {
    const context = await setup();
    const { app, hub, auth } = context;
    app.rememberOrg('o0');
    expect(context.storage.items.size).toBe(0);
    const registration = await register(app.anon, auth, hub.addInvite());
    const reminder = `agent-platform.recovery-unconfirmed.${registration.userId}`;
    context.storage.setItem(reminder, '1');
    await app.begin(await signIn({ anon: app.anon, credentials: auth, origin: ORIGIN }));
    expect(context.storage.items.has(reminder)).toBe(false);
    app.rememberOrg('o');
    await app.signOut();
    app.rememberOrg('o2');
    expect(context.storage.items.get(`agent-platform.last-org.${registration.userId}`)).toBe('o');
    expect(app.landingOrg()).toBeNull();
  });

  it('signs a different user in after clearing the previous one', async () => {
    const context = await setup();
    const { app, hub, auth, store } = context;
    const first = await onboarded(context);
    const second = await register(app.anon, auth, hub.addInvite());
    const login = await signIn({ anon: app.anon, credentials: auth, origin: ORIGIN });
    await app.begin(login);
    expect(store.getState().me.userId).toBe(second.userId);
    expect(store.getState().me.identity).toBeNull();
    expect(store.getState().orgs.byId[first.prepared.org]).toBeUndefined();
    expect(store.getState().session.phase).toBe('locked');
    // a renewal of the same user keeps the identity and the phase
    await app.unlock(second.identity);
    await app.begin(await signIn({ anon: app.anon, credentials: auth, origin: ORIGIN }));
    expect(store.getState().me.identity).toBe(second.identity);
    expect(store.getState().session.phase).toBe('unlocked');
  });

  it('lands in the last org used, else the first one that is not private, else the first', async () => {
    const { app, store } = await setup();
    const own = { org_id: 'p', role: 'admin', private: true, default_wg: 'w' };
    const team = { org_id: 't', role: 'member', private: false, default_wg: 'w' };
    setProfile(store, { orgs: [own, team] });
    expect(app.landingOrg()).toBe('t');
    setProfile(store, { orgs: [own] });
    expect(app.landingOrg()).toBe('p');
  });

  it('installs an org only with an unlocked identity', async () => {
    const context = await setup();
    const prepared = { org: 'o' } as never;
    await expect(context.app.installOrg(prepared)).rejects.toMatchObject({ code: 'decrypt_failed' });
  });

  it('runs the live channel of the active org only while unlocked', async () => {
    const stops: string[] = [];
    const live = vi.fn(async (wiring: LiveWiring) => ({ channel: {} as never, stop: () => stops.push(wiring.org) }));
    const context = await setup({ live });
    const { app, store } = context;
    app.activate('early');
    expect(live).not.toHaveBeenCalled();
    expect(store.getState().session.activeOrg).toBe('early');
    const { prepared } = await onboarded(context);
    app.activate(prepared.org);
    app.activate(prepared.org);
    expect(live).toHaveBeenCalledTimes(1);
    expect(live.mock.calls[0]![0].org).toBe(prepared.org);
    app.activate('other');
    await vi.waitFor(() => expect(stops).toEqual([prepared.org]));
    expect(app.landingOrg()).toBe(prepared.org);
    await app.signOut();
    await vi.waitFor(() => expect(stops).toEqual([prepared.org, 'other']));
  });

  it('survives a live channel that does not start', async () => {
    const live = vi.fn(async () => {
      throw new Error('no stream');
    });
    const context = await setup({ live });
    const { prepared } = await onboarded(context);
    context.app.activate(prepared.org);
    await context.app.signOut();
    expect(live).toHaveBeenCalledOnce();
  });

  it('starts the real live channel by default', async () => {
    const context = await setup();
    const { prepared } = await onboarded(context);
    context.app.activate(prepared.org);
    await vi.waitFor(() => expect(context.hub.log.some((entry) => entry.target.startsWith('/v1/stream'))).toBe(true));
    await context.app.signOut();
  });

  it('saves the locale and keeps the model settings', async () => {
    const context = await setup();
    const { app, hub, store } = context;
    const { registration } = await onboarded(context);
    hub.users.get(registration.userId)!.settings = { model_access: 'platform', model_id: 'm', locale: 'en' };
    await app.loadMe();
    await app.saveLocale('de');
    expect(hub.users.get(registration.userId)!.settings).toEqual({
      model_access: 'platform',
      model_id: 'm',
      locale: 'de',
    });
    expect(store.getState().session.locale).toBe('de');
    expect((store.getState().me.profile!.settings as { locale: string }).locale).toBe('de');
    // a profile without settings sends the defaults; an odd answer keeps what was sent
    hub.override.set('PUT /v1/me/settings', () => 'ok');
    store.update('me', (me) => ({ ...me, profile: { settings: { model_access: 1, model_id: 2 } } }));
    await app.saveLocale('en');
    expect(store.getState().me.profile!.settings).toEqual({ model_access: 'auto', model_id: null, locale: 'en' });
    store.update('me', (me) => ({ ...me, profile: null }));
    await app.saveLocale('de');
    expect(store.getState().me.profile!.settings).toEqual({ model_access: 'auto', model_id: null, locale: 'de' });
  });

  it('signs out at the hub, clears IndexedDB and memory, also when the hub is gone', async () => {
    const context = await setup();
    const { app, hub, store, persist } = context;
    const { login } = await onboarded(context);
    await app.signOut();
    expect(hub.sessions.get(login.keyId)!.ended).toBe(true);
    expect(await persist.loadSession()).toBeNull();
    expect(store.getState().me.identity).toBeNull();
    expect(store.getState().session.phase).toBe('anonymous');
    // anonymous: no hub call
    const calls = hub.log.length;
    await app.signOut();
    expect(hub.log.length).toBe(calls);
    // a failing hub and a failing persistence do not stop the sign-out
    const other = await setup();
    await onboarded(other);
    other.hub.override.set('POST /v1/session/end', () => {
      throw new Error('down');
    });
    vi.spyOn(other.persist, 'clearSession').mockRejectedValue(new Error('blocked'));
    await expect(other.app.signOut()).resolves.toBeUndefined();
    expect(other.store.getState().session.phase).toBe('anonymous');
  });

  it('forgets this browser device-key entry at sign-out, also when the device keys fail', async () => {
    const context = await setup();
    const { app } = context;
    await onboarded(context);
    const store = await DeviceKeys.open(new IDBFactory());
    const userId = app.userId!;
    await store.remember(userId, context.store.getState().me.identity!);
    const withDevice = new AppSession({
      store: createStore(),
      fetch: context.hub.fetch,
      config: CONFIG,
      rootKey: CONFIG.platform_root_key,
      persist: context.persist,
      prefs: new Prefs(null),
      now: () => T0,
      device: async () => store,
    });
    await withDevice.restore();
    expect(withDevice.store.getState().session.phase).toBe('unlocked');
    await withDevice.signOut();
    expect(await store.info(userId)).toBeNull();
    // a device store that refuses does not stop the sign-out
    const broken = await setup();
    await onboarded(broken);
    const failing = new AppSession({
      store: broken.store,
      fetch: broken.hub.fetch,
      config: CONFIG,
      rootKey: CONFIG.platform_root_key,
      persist: broken.persist,
      prefs: new Prefs(null),
      device: () => Promise.reject(new Error('no IndexedDB')),
    });
    await expect(failing.signOut()).resolves.toBeUndefined();
  });

  it('reports session notices of the signed client', async () => {
    let now = T0;
    const context = await setup({ now: () => now });
    await onboarded(context);
    now = T0 + 43080_000 - 60_000;
    context.hub.now = now;
    await context.app.loadMe();
    expect(context.store.getState().connection.session).toBe('expiring');
    expect(context.app.config).toBe(CONFIG);
    expect(context.app.rootKey).toBe(CONFIG.platform_root_key);
  });
});
