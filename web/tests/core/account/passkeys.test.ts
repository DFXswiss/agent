// Passkey management (06-web.md section 3.6, 04-crypto.md section 5.6, `core/account/passkeys.ts`) against the
// simulated hub: the list of `GET /v1/me`, adding a passkey on this or another device and the sign-in restricted to it,
// removal including `last_passkey`, whether a recovery code exists, and the session facts the screens read.
import { IDBFactory } from 'fake-indexeddb';
import { describe, expect, it } from 'vitest';
import { CeremonyCancelled } from '../../../src/core/account/ceremony';
import { signIn } from '../../../src/core/account/login';
import {
  credentialBytes,
  hasRecoveryCode,
  passkeysOf,
  placeOptions,
  registerPasskey,
  removePasskey,
} from '../../../src/core/account/passkeys';
import { Prefs } from '../../../src/core/account/prefs';
import { register } from '../../../src/core/account/register';
import { AppSession } from '../../../src/core/account/session';
import { b64uEncode } from '../../../src/core/encoding/b64u';
import { CryptoError } from '../../../src/core/crypto/errors';
import { newRecoveryCode, rotateRecoveryCode } from '../../../src/core/account/recover';
import { HubError } from '../../../src/core/net/signedFetch';
import { Persist } from '../../../src/core/persist';
import { createStore } from '../../../src/core/store';
import { FakeAuthenticator, giveRecoveryCode } from './authenticator';
import { CONFIG, ORIGIN, SimHub, T0 } from './hub';

const codeOf = async (run: () => Promise<unknown>): Promise<string> => {
  try {
    await run();
  } catch (error) {
    if (error instanceof CryptoError || error instanceof HubError) return error.code;
    throw error;
  }
  throw new Error('no error');
};

async function signedIn(auth = new FakeAuthenticator()) {
  const hub = new SimHub();
  const app = new AppSession({
    store: createStore(),
    fetch: hub.fetch,
    config: CONFIG,
    rootKey: CONFIG.platform_root_key,
    persist: await Persist.open(new IDBFactory()),
    prefs: new Prefs(null),
    now: () => T0,
  });
  const registration = await register(app.anon, auth, hub.addInvite());
  const login = await signIn({ anon: app.anon, credentials: auth, origin: ORIGIN, now: () => T0 });
  await app.begin(login);
  await app.unlock(registration.identity);
  const request = (method: string, target: string, options?: { body: unknown }) =>
    app.client.request(method, target, options);
  return { hub, auth, app, registration, login, request };
}

describe('passkey list', () => {
  it('reads the passkeys of GET /v1/me and leaves malformed items out', () => {
    expect(passkeysOf(null)).toEqual([]);
    expect(passkeysOf({ passkeys: 'x' })).toEqual([]);
    const good = {
      credential_id: 'Y3JlZA',
      created_at: '2026-11-21T12:00:00.000Z',
      last_used_at: '2026-11-22T08:30:00.000Z',
      prf_seen: true,
    };
    expect(
      passkeysOf({
        passkeys: [
          good,
          { ...good, credential_id: 'bmV2ZXI', last_used_at: null, prf_seen: false },
          { ...good, credential_id: 'YmFk', last_used_at: 'yesterday', prf_seen: 'yes' },
          { ...good, credential_id: '' },
          { ...good, credential_id: 5 },
          { ...good, created_at: 'never' },
          { ...good, created_at: 17 },
          null,
        ],
      }),
    ).toEqual([
      {
        credentialId: 'Y3JlZA',
        createdAt: Date.UTC(2026, 10, 21, 12),
        lastUsedAt: Date.UTC(2026, 10, 22, 8, 30),
        prfSeen: true,
      },
      { credentialId: 'bmV2ZXI', createdAt: Date.UTC(2026, 10, 21, 12), lastUsedAt: null, prfSeen: false },
      { credentialId: 'YmFk', createdAt: Date.UTC(2026, 10, 21, 12), lastUsedAt: null, prfSeen: false },
    ]);
  });

  it('asks for a roaming authenticator (hybrid/QR, security key) only on another device', () => {
    const options = {
      challenge: new Uint8Array(32),
      rp: { id: 'hub.example', name: 'x' },
      user: { id: new Uint8Array(16), name: 'u', displayName: 'u' },
      pubKeyCredParams: [],
      authenticatorSelection: { residentKey: 'required', userVerification: 'required' },
    } as PublicKeyCredentialCreationOptions;
    expect(placeOptions(options, 'this-device')).toBe(options);
    expect(placeOptions(options, 'other-device')).toMatchObject({
      authenticatorSelection: {
        residentKey: 'required',
        userVerification: 'required',
        authenticatorAttachment: 'cross-platform',
      },
      hints: ['hybrid', 'security-key'],
    });
  });
});

describe('adding a passkey', () => {
  it('registers a second passkey, signs in restricted to it and uploads its slot', async () => {
    const { hub, auth, app, registration, login, request } = await signedIn();
    const added = await registerPasskey(request, auth, registration.userId, 'other-device');
    const user = hub.users.get(registration.userId)!;
    expect(user.credentials.has(added)).toBe(true);
    expect(added).not.toBe(login.credentialId);
    expect(auth.lastCreate!.excludeCredentials).toHaveLength(1);
    expect(auth.lastCreate!.authenticatorSelection!.authenticatorAttachment).toBe('cross-platform');
    const second = await signIn({
      anon: app.anon,
      credentials: auth,
      origin: ORIGIN,
      allowCredential: credentialBytes(added),
    });
    expect(auth.lastGet!.allowCredentials).toEqual([{ type: 'public-key', id: credentialBytes(added) }]);
    expect(second.credentialId).toBe(added);
    expect(second.prf).not.toBeNull();
    await app.begin(second);
    expect(app.credentialId).toBe(added);
    expect(await app.uploadPasskeySlot(second, registration.identity)).toBe(true);
    expect(user.bundles.has(`passkey:${added}`)).toBe(true);
    const listed = passkeysOf(await app.loadMe());
    expect(listed.map((row) => row.credentialId)).toEqual([added, login.credentialId]);
    expect(listed[0]!.prfSeen).toBe(true);
  });

  it('refuses answers that do not belong to the signed-in user and reports a cancelled gesture', async () => {
    const { hub, auth, registration, request } = await signedIn();
    const begin = 'POST /v1/passkeys/register/begin';
    hub.override.set(begin, () => null);
    expect(await codeOf(() => registerPasskey(request, auth, registration.userId, 'this-device'))).toBe(
      'header_invalid',
    );
    hub.override.set(begin, () => ({ ceremony_id: 'c', user_id: 'someone-else', options: {} }));
    expect(await codeOf(() => registerPasskey(request, auth, registration.userId, 'this-device'))).toBe(
      'header_invalid',
    );
    hub.override.set(begin, () => ({ ceremony_id: 7, user_id: registration.userId, options: {} }));
    expect(await codeOf(() => registerPasskey(request, auth, registration.userId, 'this-device'))).toBe(
      'header_invalid',
    );
    // creation options for another user
    hub.override.delete(begin);
    const foreign = (await hub
      .fetch('/v1/passkeys/register/begin', {
        method: 'POST',
        body: new TextEncoder().encode(JSON.stringify({ invite_id: await hub.addInviteId() })),
      })
      .then((response) => response.json())) as { options: unknown };
    hub.override.set(begin, () => ({ ceremony_id: 'c', user_id: registration.userId, options: foreign.options }));
    expect(await codeOf(() => registerPasskey(request, auth, registration.userId, 'this-device'))).toBe(
      'header_invalid',
    );
    hub.override.delete(begin);
    const finish = 'POST /v1/passkeys/register/finish';
    hub.override.set(finish, () => ({ user_id: registration.userId, credential_id: 'other' }));
    expect(await codeOf(() => registerPasskey(request, auth, registration.userId, 'this-device'))).toBe(
      'header_invalid',
    );
    hub.override.set(finish, () => []);
    expect(await codeOf(() => registerPasskey(request, auth, registration.userId, 'this-device'))).toBe(
      'header_invalid',
    );
    hub.override.delete(finish);
    auth.refuse = 'AbortError';
    await expect(registerPasskey(request, auth, registration.userId, 'this-device')).rejects.toBeInstanceOf(
      CeremonyCancelled,
    );
  });
});

describe('removing a passkey', () => {
  it('removes a further passkey, refuses the last one and ends the sessions of a removed passkey', async () => {
    const { hub, auth, app, registration, login, request } = await signedIn();
    expect(await codeOf(() => removePasskey(request, login.credentialId))).toBe('last_passkey');
    const added = await registerPasskey(request, auth, registration.userId, 'this-device');
    await removePasskey(request, added);
    expect(hub.users.get(registration.userId)!.credentials.has(added)).toBe(false);
    expect(await codeOf(() => removePasskey(request, added))).toBe('not_found');
    // removing the passkey of the current session ends it
    const again = await registerPasskey(request, auth, registration.userId, 'this-device');
    await removePasskey(request, login.credentialId);
    expect(await codeOf(() => app.loadMe())).toBe('key_unknown');
    expect(again).toBeTruthy();
  });
});

describe('whether a recovery code exists', () => {
  it('is false for the unset slot of a registration without code, true once one was made', async () => {
    const { app, auth, registration } = await signedIn();
    expect(await hasRecoveryCode(registration.userId, registration.identity, await app.recoveryBundle())).toBe(false);
    const request = (method: string, target: string, options?: { body: unknown }) =>
      app.client.request(method, target, options);
    await rotateRecoveryCode(request, auth, await newRecoveryCode(registration.userId, registration.identity));
    expect(await hasRecoveryCode(registration.userId, registration.identity, await app.recoveryBundle())).toBe(true);
  });

  it('is true for an account with a code', async () => {
    const { app, hub, registration } = await signedIn();
    await giveRecoveryCode(hub, registration);
    expect(await hasRecoveryCode(registration.userId, registration.identity, await app.recoveryBundle())).toBe(true);
  });
});

describe('session facts for the passkey screens', () => {
  it('knows the passkey of the current session and the page fetch', async () => {
    const { hub, app, login, registration } = await signedIn();
    expect(app.fetch).toBe(hub.fetch);
    expect(app.credentialId).toBe(login.credentialId);
    const persist = await Persist.open(new IDBFactory());
    await persist.saveSession({
      keyId: login.keyId,
      privateKey: login.privateKey,
      publicKey: login.publicKey,
      userId: registration.userId,
      credentialId: login.credentialId,
      expiresAt: login.expiresAt,
    });
    const restored = new AppSession({
      store: createStore(),
      fetch: hub.fetch,
      config: CONFIG,
      rootKey: CONFIG.platform_root_key,
      persist,
      prefs: new Prefs(null),
      now: () => T0,
    });
    expect(restored.credentialId).toBeNull();
    await restored.restore();
    expect(restored.credentialId).toBe(login.credentialId);
    await app.signOut();
    expect(app.credentialId).toBeNull();
    expect(b64uEncode(credentialBytes(login.credentialId))).toBe(login.credentialId);
  });
});
