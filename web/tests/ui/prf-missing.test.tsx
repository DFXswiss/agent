// `prf_missing` in jsdom (06-web.md section 3.3): the device key unlocks the sign-in and gives a PRF-capable passkey its
// missing slot (also when the upload is refused), a device entry that does not open is forgotten and the guidance is
// shown, and the registration's "Remember this browser" offer when IndexedDB refuses.
import { fireEvent, screen, waitFor } from '@testing-library/react';
import { IDBFactory } from 'fake-indexeddb';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { preparePrivateOrg } from '../../src/core/account/genesis';
import { signIn } from '../../src/core/account/login';
import { register, type Registration } from '../../src/core/account/register';
import { b64uEncode } from '../../src/core/encoding/b64u';
import { committed } from '../../src/core/persist';
import { DeviceKeys, openDeviceDatabase } from '../../src/core/persist/device-key';
import { deviceKeys } from '../../src/ui/prf-missing/device';
import { FakeAuthenticator } from '../core/account/authenticator';
import { HubFailure, ORIGIN, SimHub } from '../core/account/hub';
import { type makeApp, renderApp } from './harness';

type Made = Awaited<ReturnType<typeof makeApp>>;

afterEach(() => vi.unstubAllGlobals());

/** A user with a private org, signed out again; this browser remembered (device key) if `factory` is given. */
async function registeredUser(made: Made, factory: IDBFactory | null) {
  const invite = made.hub.addInvite();
  const registration = await register(made.app.anon, made.auth, invite);
  await made.app.begin(await signIn({ anon: made.app.anon, credentials: made.auth, origin: ORIGIN }));
  await made.app.unlock(registration.identity);
  const prepared = await preparePrivateOrg({
    userId: registration.userId,
    identity: registration.identity,
    displayName: 'Ada',
    locale: 'en',
    platformInviteId: await made.hub.idOf(invite),
  });
  await made.app.client.request('POST', '/v1/orgs', { body: prepared.body });
  await made.app.installOrg(prepared);
  await made.app.signOut();
  if (factory !== null) await (await DeviceKeys.open(factory)).remember(registration.userId, registration.identity);
  return { registration, org: prepared.org };
}

async function loginPage(factory: IDBFactory | null) {
  if (factory !== null) vi.stubGlobal('indexedDB', factory);
  const hub = new SimHub();
  const auth = new FakeAuthenticator();
  let user: { registration: Registration; org: string } | null = null;
  const harness = await renderApp('/login', { hub, auth }, async (made) => {
    user = await registeredUser(made, factory);
  });
  await screen.findByTestId('login.submit');
  return { ...harness, user: user! };
}

describe('device key on sign-in', () => {
  it('unlocks without the recovery code and uploads the missing passkey slot', async () => {
    const { router, user, hub } = await loginPage(new IDBFactory());
    fireEvent.click(screen.getByTestId('login.submit'));
    await waitFor(() => expect(router.state.location.pathname).toBe(`/o/${user.org}`));
    expect(screen.queryByTestId('prf-missing.page')).toBeNull();
    const bundles = hub.users.get(user.registration.userId)!.bundles;
    expect([...bundles.keys()].some((slot) => slot.startsWith('passkey:'))).toBe(true);
  });

  it('keeps the unlock when the slot upload is refused', async () => {
    const { router, user, hub, auth } = await loginPage(new IDBFactory());
    hub.override.set(`PUT /v1/me/bundles/passkey:${b64uEncode(auth.credentials[0]!.id)}`, new HubFailure(503, 'x'));
    fireEvent.click(screen.getByTestId('login.submit'));
    await waitFor(() => expect(router.state.location.pathname).toBe(`/o/${user.org}`));
    expect([...hub.users.get(user.registration.userId)!.bundles.keys()]).toEqual(['recovery']);
  });

  it('forgets a device entry that does not open and shows the guidance', async () => {
    const factory = new IDBFactory();
    const { user } = await loginPage(factory);
    const db = await openDeviceDatabase(factory);
    const transaction = db.transaction('keys', 'readwrite');
    const store = transaction.objectStore('keys');
    const entry = await new Promise<Record<string, unknown>>((resolve) => {
      const request = store.get(user.registration.userId);
      request.onsuccess = () => resolve(request.result as Record<string, unknown>);
    });
    store.put({ ...entry, bundle: b64uEncode(new Uint8Array(48)) }, user.registration.userId);
    await committed(transaction);
    db.close();
    fireEvent.click(screen.getByTestId('login.submit'));
    expect(await screen.findByTestId('prf-missing.explain')).toBeInTheDocument();
    await waitFor(async () => expect(await (await DeviceKeys.open(factory)).info(user.registration.userId)).toBeNull());
  });
});

describe('device key that cannot be read', () => {
  it('shows the guidance when the device-key database is gone', async () => {
    const factory = new IDBFactory();
    await loginPage(factory);
    // the page's device keys are open; the database goes away under them
    (await deviceKeys(factory)).close();
    fireEvent.click(screen.getByTestId('login.submit'));
    expect(await screen.findByTestId('prf-missing.explain')).toBeInTheDocument();
  });
});

describe('remember offer of the registration', () => {
  it('reports a browser that cannot store the key and continues without it', async () => {
    const hub = new SimHub();
    const auth = new FakeAuthenticator();
    auth.prf = false;
    const code = hub.addInvite();
    window.history.replaceState({ key: 'invite' }, '', `/invite#${code}`);
    // no IndexedDB in this page: the device key cannot be stored
    const harness = await renderApp('/invite', { hub, auth });
    fireEvent.change(await screen.findByTestId('invite.display-name'), { target: { value: 'Ada' } });
    fireEvent.click(screen.getByTestId('invite.create-passkey'));
    fireEvent.click(await screen.findByTestId('invite.unlock'));
    const recovery = (await screen.findByTestId('recovery-code.code')).textContent!;
    fireEvent.change(screen.getByTestId('recovery-code.confirm'), { target: { value: recovery.slice(-6) } });
    fireEvent.click(screen.getByTestId('recovery-code.continue'));
    fireEvent.click(await screen.findByTestId('prf-missing.offer.remember'));
    expect(await screen.findByTestId('prf-missing.offer.failed')).toBeInTheDocument();
    fireEvent.click(screen.getByTestId('prf-missing.offer.skip'));
    await waitFor(() => expect(harness.router.state.location.pathname).toMatch(/^\/o\//));
  });
});
