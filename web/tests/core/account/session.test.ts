import { IDBFactory } from 'fake-indexeddb';
import { describe, expect, it, vi } from 'vitest';
import { preparePrivateOrg } from '../../../src/core/account/genesis';
import { signIn, unlockWithRecoveryCode } from '../../../src/core/account/login';
import { Prefs, type PrefStorage } from '../../../src/core/account/prefs';
import { register } from '../../../src/core/account/register';
import { AppSession, meOrgs, settingsLocale } from '../../../src/core/account/session';
import { Identity } from '../../../src/core/crypto/keys';
import type { LiveConnection, LiveWiring } from '../../../src/core/net/live';
import { Persist } from '../../../src/core/persist';
import { createStore } from '../../../src/core/store';
import { setActiveOrg, setLocale, setPhase, setPrfMissing } from '../../../src/core/store/slices/session';
import { FakeAuthenticator, giveRecoveryCode } from './authenticator';
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

  it('reports session notices of the signed client', async () => {
    let now = T0;
    const context = await setup({ now: () => now });
    await onboarded(context);
    now = T0 + 43080_000 - 60_000;
    context.hub.now = now;
    await context.app.loadMe();
    expect(context.store.getState().connection.session).toBe('expiring');
    expect(context.app.config).toBe(CONFIG);
  });
});
