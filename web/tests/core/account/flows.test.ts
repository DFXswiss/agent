import { describe, expect, it, vi } from 'vitest';
import { anonClient } from '../../../src/core/account/anon';
import { CeremonyCancelled, createCredential, getAssertion, isCancel } from '../../../src/core/account/ceremony';
import {
  createOrg,
  DISPLAY_NAME_MAX,
  displayName,
  preparePrivateOrg,
  PRIVATE_NAMES,
} from '../../../src/core/account/genesis';
import {
  passkeyBundle,
  sameIdentity,
  signIn,
  slotOf,
  unlockWithPrf,
  unlockWithRecoveryCode,
} from '../../../src/core/account/login';
import { abandonRegistration, codeToInviteId, lookupInvite, register } from '../../../src/core/account/register';
import { isUnsetRecoverySlot } from '../../../src/core/crypto/recovery';
import { b64uDecode } from '../../../src/core/encoding/b64u';
import { fromUtf8 } from '../../../src/core/encoding/bytes';
import { CryptoError } from '../../../src/core/crypto/errors';
import { openEvent } from '../../../src/core/crypto/event';
import { formatCode, generateCode, inviteId } from '../../../src/core/crypto/invite';
import { Identity } from '../../../src/core/crypto/keys';
import { epochKey } from '../../../src/core/log/state';
import { checkBatch } from '../../../src/core/log/rotation';
import { HubError } from '../../../src/core/net/signedFetch';
import { IDBFactory } from 'fake-indexeddb';
import { Prefs } from '../../../src/core/account/prefs';
import { AppSession } from '../../../src/core/account/session';
import { Persist } from '../../../src/core/persist';
import { createStore } from '../../../src/core/store';
import { FakeAuthenticator, giveRecoveryCode } from './authenticator';
import { CONFIG, HubFailure, ORIGIN, SimHub, T0 } from './hub';

const code = (error: unknown) => (error as { code: string }).code;

async function registered(hub = new SimHub(), auth = new FakeAuthenticator()) {
  const id = hub.addInvite();
  const anon = anonClient(hub.fetch);
  const registration = await register(anon, auth, id);
  return { hub, auth, anon, id, registration };
}

describe('anonClient', () => {
  it('sends JSON without credentials and parses the answer', async () => {
    const fetch = vi.fn<typeof globalThis.fetch>(async () => Response.json({ ok: 1 }));
    expect(await anonClient(fetch)('POST', '/v1/x', { a: 1 })).toEqual({ ok: 1 });
    const init = fetch.mock.calls[0]![1]!;
    expect(init).toMatchObject({ method: 'POST', credentials: 'omit', cache: 'no-store', redirect: 'error' });
    expect(init.headers).toEqual({ accept: 'application/json', 'content-type': 'application/json' });
    expect(fromUtf8(init.body as Uint8Array)).toBe('{"a":1}');
  });

  it('sends no body without one', async () => {
    const fetch = vi.fn<typeof globalThis.fetch>(async () => Response.json({}));
    await anonClient(fetch)('GET', '/v1/x');
    expect(fetch.mock.calls[0]![1]!.body).toBeUndefined();
    expect(fetch.mock.calls[0]![1]!.headers).toEqual({ accept: 'application/json' });
  });

  it('maps error answers and network failures onto catalog codes', async () => {
    const refused = anonClient(async () => Response.json({ error: 'invite_unknown' }, { status: 404 }));
    await expect(refused('POST', '/v1/x', {})).rejects.toMatchObject({ code: 'invite_unknown', status: 404 });
    const down = anonClient(async () => {
      throw new TypeError('network');
    });
    await expect(down('POST', '/v1/x', {})).rejects.toMatchObject({ code: 'unavailable', status: 0 });
  });
});

describe('ceremonies', () => {
  const failing = (name: string) => ({
    create: () => Promise.reject(Object.assign(new Error('x'), { name })),
    get: () => Promise.reject(Object.assign(new Error('x'), { name })),
  });
  const options = {} as PublicKeyCredentialCreationOptions;

  it('turns AbortError, NotAllowedError and no credential into CeremonyCancelled', async () => {
    await expect(createCredential(failing('AbortError'), options)).rejects.toBeInstanceOf(CeremonyCancelled);
    await expect(
      getAssertion(failing('NotAllowedError'), {} as PublicKeyCredentialRequestOptions),
    ).rejects.toBeInstanceOf(CeremonyCancelled);
    const none = { create: async () => null, get: async () => undefined };
    await expect(createCredential(none, options)).rejects.toBeInstanceOf(CeremonyCancelled);
    await expect(getAssertion(none, {} as PublicKeyCredentialRequestOptions)).rejects.toBeInstanceOf(CeremonyCancelled);
  });

  it('passes other errors through', async () => {
    await expect(createCredential(failing('InvalidStateError'), options)).rejects.toMatchObject({
      name: 'InvalidStateError',
    });
    expect(isCancel('AbortError')).toBe(false);
    expect(new CeremonyCancelled().name).toBe('CeremonyCancelled');
  });
});

describe('invitation lookup', () => {
  it('derives the invite_id from the code text and asks the hub', async () => {
    const raw = generateCode();
    const id = await codeToInviteId(formatCode(raw).toLowerCase());
    expect(id).toBe(await inviteId(raw));
    const hub = new SimHub();
    const invite = await hub.addInviteId();
    const found = await lookupInvite(anonClient(hub.fetch), invite);
    expect(found).toMatchObject({ inviteId: invite, kind: 'user', scope: 'platform', registered: false });
    expect(found.raw.scope).toBe('platform');
    expect(hub.log[0]).toMatchObject({ target: '/v1/invites/lookup', signed: false });
  });

  it('refuses malformed codes and answers', async () => {
    await expect(codeToInviteId('ABC')).rejects.toMatchObject({ code: 'invite_code_length' });
    await expect(codeToInviteId('1'.repeat(26))).rejects.toMatchObject({ code: 'invite_code_invalid' });
    await expect(lookupInvite(async () => ({ scope: 'x' }), 'i')).rejects.toMatchObject({ code: 'header_invalid' });
    await expect(lookupInvite(async () => null, 'i')).rejects.toMatchObject({ code: 'header_invalid' });
    const org = await lookupInvite(
      async () => ({ scope: 'org', kind: 'user', expires_at: 't', registered: true }),
      'i',
    );
    expect(org).toMatchObject({ scope: 'org', registered: true });
    await expect(lookupInvite(anonClient(new SimHub().fetch), 'nope')).rejects.toMatchObject({
      code: 'invite_unknown',
    });
  });
});

describe('register', () => {
  it('creates identity, an unset recovery slot and CK_1 in the browser and sends only public data', async () => {
    const { hub, id, registration, auth } = await registered();
    expect(registration.prfAtCreate).toBe(true);
    expect(auth.lastCreate?.pubKeyCredParams).toEqual([{ type: 'public-key', alg: -7 }]);
    const user = hub.users.get(registration.userId)!;
    expect(user.identity).toEqual({
      kem_pub: registration.identity.public().kem,
      sig_pub: registration.identity.public().sig,
    });
    expect(hub.invites.get(await hub.idOf(id))!.registered).toBe(registration.userId);
    // nothing secret reached the hub: not the private keys
    const sent = hub.log.map((entry) => entry.body).join('\n');
    registration.identity.revealPrivate((kem, sig) => {
      expect(sent).not.toContain(Buffer.from(kem).toString('base64url'));
      expect(sent).not.toContain(Buffer.from(sig).toString('base64url'));
    });
    const finish = JSON.parse(hub.log.find((entry) => entry.target.endsWith('register/finish'))!.body);
    expect(finish.cred_wrap.epoch).toBe(1);
    expect(finish.credential.transports).toEqual(['internal']);
  });

  it('reports no PRF support at create()', async () => {
    const auth = new FakeAuthenticator();
    auth.prf = false;
    const { registration } = await registered(new SimHub(), auth);
    expect(registration.prfAtCreate).toBe(false);
  });

  it('makes no recovery code: the slot is the unset one, whatever create() reports (KOR-22)', async () => {
    const auth = new FakeAuthenticator();
    auth.prfAtCreate = false;
    const { hub, registration } = await registered(new SimHub(), auth);
    const user = hub.users.get(registration.userId)!;
    const slot = user.bundles.get('recovery')!;
    expect(await isUnsetRecoverySlot(registration.userId, registration.identity, slot)).toBe(true);
    // no code opens it
    await expect(unlockWithRecoveryCode(registration.userId, formatCode(generateCode()), slot)).rejects.toMatchObject({
      code: 'decrypt_failed',
    });
    // an ordinary slot for the hub: a recovery ID, a bundle of the usual length
    const finish = JSON.parse(hub.log.find((entry) => entry.target.endsWith('register/finish'))!.body);
    expect(finish.recovery.recovery_id).toMatch(/^[A-Za-z0-9_-]{22}$/);
    expect(user.recoveryId).toBe(finish.recovery.recovery_id);
    const other = await registered();
    await giveRecoveryCode(other.hub, other.registration);
    const real = other.hub.users.get(other.registration.userId)!.bundles.get('recovery')!;
    expect(b64uDecode(slot).length).toBe(b64uDecode(real).length);
    expect(await isUnsetRecoverySlot(other.registration.userId, other.registration.identity, real)).toBe(false);
    // another identity or user does not see the mark
    expect(await isUnsetRecoverySlot(registration.userId, other.registration.identity, slot)).toBe(false);
    expect(await isUnsetRecoverySlot(other.registration.userId, registration.identity, slot)).toBe(false);
  });

  it('withdraws an unfinished registration; a refusal or an older hub answers false', async () => {
    const { hub, auth, anon, id, registration } = await registered();
    const app = new AppSession({
      store: createStore(),
      fetch: hub.fetch,
      config: CONFIG,
      rootKey: CONFIG.platform_root_key,
      persist: await Persist.open(new IDBFactory()),
      prefs: new Prefs(null),
      now: () => T0,
    });
    await app.begin(await signIn({ anon, credentials: auth, origin: ORIGIN, now: () => T0 }));
    const request = (method: string, target: string, options?: { body: unknown }) =>
      app.client.request(method, target, options);
    hub.override.set('DELETE /v1/me/registration', new HubFailure(404, 'not_found'));
    expect(await abandonRegistration(request)).toBe(false);
    hub.override.delete('DELETE /v1/me/registration');
    expect(await abandonRegistration(request)).toBe(true);
    expect(hub.users.has(registration.userId)).toBe(false);
    expect(hub.invites.get(await hub.idOf(id))!.registered).toBeNull();
  });

  it('rejects a cancelled gesture and leaves the invitation unbound', async () => {
    const hub = new SimHub();
    const auth = new FakeAuthenticator();
    auth.refuse = 'NotAllowedError';
    const id = hub.addInvite();
    await expect(register(anonClient(hub.fetch), auth, id)).rejects.toBeInstanceOf(CeremonyCancelled);
    expect(hub.invites.get(await hub.idOf(id))!.registered).toBeNull();
    // a new attempt starts with a new begin
    await register(anonClient(hub.fetch), auth, id);
    expect(hub.log.filter((entry) => entry.target.endsWith('register/begin'))).toHaveLength(2);
  });

  it('refuses inconsistent hub answers', async () => {
    const hub = new SimHub();
    const auth = new FakeAuthenticator();
    const id = hub.addInvite();
    const anon = anonClient(hub.fetch);
    hub.override.set('POST /v1/passkeys/register/begin', () => null);
    await expect(register(anon, auth, id)).rejects.toMatchObject({ code: 'header_invalid' });
    hub.override.set('POST /v1/passkeys/register/begin', () => ({ user_id: 'not-a-uuid', ceremony_id: 'c' }));
    await expect(register(anon, auth, id)).rejects.toMatchObject({ code: 'header_invalid' });
    hub.override.set('POST /v1/passkeys/register/begin', () => ({ user_id: 7, ceremony_id: 'c' }));
    await expect(register(anon, auth, id)).rejects.toMatchObject({ code: 'header_invalid' });
    hub.override.delete('POST /v1/passkeys/register/begin');
    hub.override.set('POST /v1/passkeys/register/finish', () => ({ user_id: 'someone-else' }));
    await expect(register(anon, auth, id)).rejects.toMatchObject({ code: 'header_invalid' });
    hub.override.set('POST /v1/passkeys/register/finish', () => 'x');
    await expect(register(anon, auth, id)).rejects.toMatchObject({ code: 'header_invalid' });
    hub.override.set('POST /v1/passkeys/register/finish', new HubFailure(404, 'invite_unknown'));
    await expect(register(anon, auth, id)).rejects.toBeInstanceOf(HubError);
  });

  it('refuses options for another user', async () => {
    const hub = new SimHub();
    const anon = anonClient(hub.fetch);
    const id = hub.addInvite();
    const real = await anon('POST', '/v1/passkeys/register/begin', { invite_id: await hub.idOf(id) });
    const other = (await anon('POST', '/v1/passkeys/register/begin', { invite_id: await hub.addInviteId() })) as Record<
      string,
      unknown
    >;
    hub.override.set('POST /v1/passkeys/register/begin', () => ({ ...(real as object), options: other.options }));
    await expect(register(anon, new FakeAuthenticator(), id)).rejects.toMatchObject({ code: 'header_invalid' });
  });
});

describe('signIn and unlock', () => {
  it('certifies a new session key and unlocks the passkey slot with the PRF output', async () => {
    const { hub, auth, anon, registration } = await registered();
    const first = await signIn({ anon, credentials: auth, origin: ORIGIN, now: () => T0 });
    expect(first.userId).toBe(registration.userId);
    expect(first.keyId).toMatch(/^session\.[0-9a-f]{32}$/);
    expect(first.privateKey.extractable).toBe(false);
    expect(first.prf).not.toBeNull();
    expect(first.bundleCt).toBeNull();
    expect(first.prfExpected).toBe(false);
    expect(first.expiresAt).toBe(T0 + 43080_000);
    expect(await unlockWithPrf(first)).toBeNull();
    // the PRF output never leaves the browser
    const prf = first.prf!.reveal((raw) => Buffer.from(raw).toString('base64url'));
    expect(hub.log.map((entry) => entry.body).join('\n')).not.toContain(prf);
    // upload the passkey slot, then a later sign-in unlocks with PRF alone
    const slot = await passkeyBundle(first, registration.identity);
    hub.users.get(registration.userId)!.bundles.set(slotOf(first), slot!);
    hub.users.get(registration.userId)!.prfSeen.add(first.credentialId);
    const second = await signIn({ anon, credentials: auth, origin: ORIGIN });
    expect(second.prfExpected).toBe(true);
    expect(second.keyId).not.toBe(first.keyId);
    const identity = await unlockWithPrf(second);
    expect(identity!.public()).toEqual(registration.identity.public());
    expect(auth.lastGet?.allowCredentials).toEqual([]);
  });

  it('reports a sign-in without PRF output', async () => {
    const { auth, anon, registration } = await registered();
    auth.prf = false;
    const result = await signIn({ anon, credentials: auth, origin: ORIGIN });
    expect(result.prf).toBeNull();
    expect(await unlockWithPrf({ ...result, bundleCt: 'x' })).toBeNull();
    expect(await passkeyBundle(result, registration.identity)).toBeNull();
  });

  it('refuses a slot that does not open and a session key ID the hub made up', async () => {
    const { auth, anon, hub, registration } = await registered();
    const result = await signIn({ anon, credentials: auth, origin: ORIGIN });
    const foreign = await passkeyBundle(
      { ...result, userId: '0190f000-0000-7000-8000-000000000000' },
      registration.identity,
    );
    await expect(unlockWithPrf({ ...result, bundleCt: foreign })).rejects.toMatchObject({ code: 'decrypt_failed' });
    const real = hub.fetch;
    const forged: typeof globalThis.fetch = async (input, init) => {
      const response = await real(input, init);
      if (!String(input).endsWith('login/finish')) return response;
      return Response.json({ ...(await response.json()), key_id: 'session.' + '0'.repeat(32) });
    };
    await expect(signIn({ anon: anonClient(forged), credentials: auth, origin: ORIGIN })).rejects.toMatchObject({
      code: 'header_invalid',
    });
    hub.override.set('POST /v1/passkeys/login/finish', () => [1]);
    await expect(signIn({ anon, credentials: auth, origin: ORIGIN })).rejects.toMatchObject({ code: 'header_invalid' });
    hub.override.set('POST /v1/passkeys/login/finish', () => ({ user_id: 'nope', key_id: 'x' }));
    await expect(signIn({ anon, credentials: auth, origin: ORIGIN })).rejects.toMatchObject({ code: 'header_invalid' });
    hub.override.set('POST /v1/passkeys/login/begin', () => ({ ceremony_id: '' }));
    await expect(signIn({ anon, credentials: auth, origin: ORIGIN })).rejects.toMatchObject({ code: 'header_invalid' });
    hub.override.set('POST /v1/passkeys/login/begin', () => 'x');
    await expect(signIn({ anon, credentials: auth, origin: ORIGIN })).rejects.toMatchObject({ code: 'header_invalid' });
  });

  it('tags the ceremony with an agent pairing when asked (11-agent-access.md section 5.1)', async () => {
    const { auth, anon } = await registered();
    const begun: unknown[] = [];
    const tagging: typeof anon = (method, target, body) => {
      if (target.endsWith('/login/begin')) begun.push(body);
      return anon(method, target, body);
    };
    const pairingId = '01a1ee56-5e00-7242-9e24-f87629ad2977';
    await signIn({ anon: tagging, credentials: auth, origin: ORIGIN, pairingId });
    await signIn({ anon: tagging, credentials: auth, origin: ORIGIN });
    expect(begun).toEqual([{ pairing_id: pairingId }, {}]);
  });

  it('rejects a cancelled sign-in', async () => {
    const { auth, anon } = await registered();
    auth.refuse = 'AbortError';
    await expect(signIn({ anon, credentials: auth, origin: ORIGIN })).rejects.toBeInstanceOf(CeremonyCancelled);
  });

  it('unlocks with the recovery code and refuses a wrong one', async () => {
    const { hub, registration } = await registered();
    const code = await giveRecoveryCode(hub, registration);
    const bundle = hub.users.get(registration.userId)!.bundles.get('recovery')!;
    const typed = code.toLowerCase().replace(/-/g, ' ');
    expect((await unlockWithRecoveryCode(registration.userId, typed, bundle)).public()).toEqual(
      registration.identity.public(),
    );
    await expect(unlockWithRecoveryCode(registration.userId, 'AAAAA', bundle)).rejects.toMatchObject({
      code: 'recovery_code_length',
    });
    const other = formatCode(generateCode());
    await expect(unlockWithRecoveryCode(registration.userId, other, bundle)).rejects.toMatchObject({
      code: 'decrypt_failed',
    });
  });

  it('compares an identity with the registered public keys', async () => {
    const identity = await Identity.generate();
    const own = identity.public();
    expect(sameIdentity(identity, { kem_pub: own.kem, sig_pub: own.sig })).toBe(true);
    expect(sameIdentity(identity, { kem_pub: own.kem, sig_pub: own.kem })).toBe(false);
    expect(sameIdentity(identity, { kem_pub: own.sig, sig_pub: own.sig })).toBe(false);
    expect(sameIdentity(identity, { kem_pub: 1, sig_pub: own.sig })).toBe(false);
    expect(sameIdentity(identity, { kem_pub: own.kem })).toBe(false);
    expect(sameIdentity(identity, null)).toBe(false);
  });
});

describe('private organization', () => {
  it('accepts display names of 1 to 80 code points', () => {
    expect(displayName('  Ada  ')).toBe('Ada');
    expect(displayName('   ')).toBeNull();
    expect(displayName('ü'.repeat(DISPLAY_NAME_MAX))).toBe('ü'.repeat(80));
    expect(displayName('😀'.repeat(80))).toBe('😀'.repeat(80));
    expect(displayName('x'.repeat(81))).toBeNull();
  });

  it('builds genesis, self-wraps and the profile events, all verifiable', async () => {
    const identity = await Identity.generate();
    const userId = '0190f000-0000-7000-8000-000000000001';
    const prepared = await preparePrivateOrg({
      userId,
      identity,
      displayName: ' Grüezi ',
      locale: 'de',
      platformInviteId: 'AAAAAAAAAAAAAAAAAAAAAA',
      now: () => T0,
    });
    const body = prepared.body as {
      genesis: { body: Record<string, unknown> };
      wraps: { scope: string }[];
      events: { header: Record<string, unknown> }[];
      platform_invite_id: string;
    };
    expect(body.platform_invite_id).toBe('AAAAAAAAAAAAAAAAAAAAAA');
    expect(body.genesis.body).toMatchObject({ type: 'genesis', private: true, default_wg: prepared.defaultWg });
    expect(body.wraps.map((wrap) => wrap.scope).sort()).toEqual(['org', 'wg']);
    await checkBatch(null, [body.genesis], body.wraps as never, userId, { founder: identity.public().sig });
    const orgKey = prepared.keys.get(epochKey('org', prepared.org, 1))!;
    expect(prepared.keys.has(epochKey('wg', prepared.defaultWg, 1))).toBe(true);
    const opened = await Promise.all(
      body.events.map(async (event) => ({
        header: event.header,
        body: JSON.parse(fromUtf8(await openEvent(orgKey, event, b64uDecode(identity.public().sig)))),
      })),
    );
    expect(opened.map((event) => [event.header.kind, event.header.lh, event.header.epoch, event.body])).toEqual([
      ['org.profile', 0, 1, { name: PRIVATE_NAMES.de.org }],
      ['wg.profile', 0, 1, { name: 'Standard' }],
      ['member.profile', 0, 1, { display_name: 'Grüezi' }],
    ]);
    expect(opened[1]!.header.wg).toBe(prepared.defaultWg);
    expect(opened[2]!.header.ref).toBe(userId);
    expect(prepared.log.head.seq).toBe(0);
  });

  it('refuses an invalid display name and a hub answer for another org', async () => {
    const identity = await Identity.generate();
    const input = { userId: '0190f000-0000-7000-8000-000000000001', identity, locale: 'en' as const };
    await expect(
      preparePrivateOrg({ ...input, displayName: '', platformInviteId: 'AAAAAAAAAAAAAAAAAAAAAA' }),
    ).rejects.toBeInstanceOf(CryptoError);
    const prepared = await preparePrivateOrg({
      ...input,
      displayName: 'A',
      platformInviteId: 'AAAAAAAAAAAAAAAAAAAAAA',
    });
    expect(PRIVATE_NAMES.en).toEqual({ org: 'Private', wg: 'Default' });
    const ok = vi.fn(async () => ({ org_id: prepared.org, default_wg: prepared.defaultWg }));
    await createOrg(ok, prepared);
    expect(ok).toHaveBeenCalledWith('POST', '/v1/orgs', { body: prepared.body });
    await expect(createOrg(async () => ({ org_id: prepared.org, default_wg: 'x' }), prepared)).rejects.toMatchObject({
      code: 'header_invalid',
    });
    await expect(createOrg(async () => null, prepared)).rejects.toMatchObject({ code: 'header_invalid' });
    expect(code(new CryptoError('decrypt_failed'))).toBe('decrypt_failed');
  });
});
