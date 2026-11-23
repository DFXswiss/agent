// Recovery with the recovery code (04-crypto.md section 5.5, `core/account/recover.ts`): the crypto steps against the
// shared vectors (code text → `recovery_id`, `KEK_rec`, recovery slot → identity, proof bytes), the whole flow against
// the simulated hub (begin, proof, new passkey, sign-in, new recovery code replacing the old one), the rate limit with
// its waiting time, and every refusal of malformed answers.
import { IDBFactory } from 'fake-indexeddb';
import { describe, expect, it, vi } from 'vitest';
import { CeremonyCancelled } from '../../../src/core/account/ceremony';
import { signIn, unlockWithRecoveryCode } from '../../../src/core/account/login';
import { Prefs } from '../../../src/core/account/prefs';
import {
  beginRecovery,
  finishRecovery,
  newRecoveryCode,
  RecoveryRateLimited,
  recoveryClient,
  replaceRecoveryCode,
  recoveryRefusal,
  rotateRecoveryCode,
  retryAfterSeconds,
  waitText,
} from '../../../src/core/account/recover';
import { register } from '../../../src/core/account/register';
import { AppSession } from '../../../src/core/account/session';
import { b64uDecode, b64uEncode } from '../../../src/core/encoding/b64u';
import { fromUtf8 } from '../../../src/core/encoding/bytes';
import { CryptoError } from '../../../src/core/crypto/errors';
import { Identity } from '../../../src/core/crypto/keys';
import { parseRecoveryCode, proofText, recoveryId } from '../../../src/core/crypto/recovery';
import { HubError } from '../../../src/core/net/signedFetch';
import { Persist } from '../../../src/core/persist';
import { createStore } from '../../../src/core/store';
import { VECTOR_FILES } from '../crypto/vector-suite';
import { FakeAuthenticator } from './authenticator';
import { CONFIG, HubFailure, ORIGIN, SimHub, T0 } from './hub';

// eslint-disable-next-line @typescript-eslint/no-explicit-any -- vector files are untyped JSON
const RECOVERY: any = VECTOR_FILES.recovery;
// eslint-disable-next-line @typescript-eslint/no-explicit-any -- vector files are untyped JSON
const BUNDLE: any = VECTOR_FILES.bundle;

const codeOf = async (run: () => Promise<unknown>): Promise<string> => {
  try {
    await run();
  } catch (error) {
    if (error instanceof CryptoError || error instanceof HubError) return error.code;
    throw error;
  }
  throw new Error('no error');
};

/** A recovery client on a fetch answering every request with `answer(target, body)`; records the bodies. */
function stubClient(answer: (target: string, body: Record<string, unknown>) => unknown) {
  const sent: { target: string; body: Record<string, unknown> }[] = [];
  const fetch = vi.fn<typeof globalThis.fetch>(async (input, init) => {
    const body = JSON.parse(fromUtf8(init!.body as Uint8Array)) as Record<string, unknown>;
    sent.push({ target: String(input), body });
    return Response.json(answer(String(input), body));
  });
  return { client: recoveryClient(fetch), sent };
}

async function setup() {
  const hub = new SimHub();
  const auth = new FakeAuthenticator();
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
  return { hub, auth, app, registration, client: recoveryClient(hub.fetch) };
}

describe('recovery crypto steps against the vectors', () => {
  it('derives recovery_id from the code text, opens the recovery slot and signs the vector proof', async () => {
    const proof = RECOVERY.proof;
    expect(BUNDLE.recovery.code).toBe(RECOVERY.code);
    expect(BUNDLE.user_id).toBe(proof.user_id);
    expect(b64uEncode(parseRecoveryCode(RECOVERY.text))).toBe(RECOVERY.code);
    expect(await recoveryId(parseRecoveryCode(RECOVERY.text))).toBe(RECOVERY.recovery_id);
    const { client, sent } = stubClient((target) =>
      target === '/v1/recovery/begin'
        ? { user_id: proof.user_id, bundle_ct: BUNDLE.recovery.sealed, challenge: proof.challenge }
        : { ceremony_id: 'stop' },
    );
    const start = await beginRecovery(client, RECOVERY.text.toLowerCase());
    expect(sent[0]).toEqual({ target: '/v1/recovery/begin', body: { recovery_id: RECOVERY.recovery_id } });
    expect(start.userId).toBe(proof.user_id);
    expect(start.challenge).toEqual(b64uDecode(proof.challenge));
    expect(start.identity.public().sig).toBe(proof.identity_sig_pk);
    // the proof is the vector's (Ed25519 is deterministic); the run stops at the options of the stub
    expect(await codeOf(() => finishRecovery(client, new FakeAuthenticator(), start))).toBe('header_invalid');
    expect(sent[1]!.target).toBe('/v1/recovery/finish');
    expect(sent[1]!.body).toEqual({ user_id: proof.user_id, challenge: proof.challenge, proof: proof.proof });
    expect(b64uEncode(proofText(proof.user_id, start.challenge))).toBe(proof.text);
  });

  it('refuses text that is no recovery code before anything is sent', async () => {
    const { client, sent } = stubClient(() => ({}));
    for (const invalid of RECOVERY.invalid.filter((item: { op: string }) => item.op === 'parse_recovery_code')) {
      expect(await codeOf(() => beginRecovery(client, invalid.args.text))).toBe(invalid.error);
    }
    expect(sent).toEqual([]);
  });
});

describe('recovery flow', () => {
  it('registers a new passkey, signs in with it and replaces the recovery code once', async () => {
    const { hub, auth, app, registration, client } = await setup();
    const start = await beginRecovery(client, registration.recoveryCode);
    expect(start.userId).toBe(registration.userId);
    expect(start.identity.public()).toEqual(registration.identity.public());
    const passkey = await finishRecovery(client, auth, start);
    expect(passkey).toMatchObject({ userId: registration.userId, prfAtCreate: true });
    const user = hub.users.get(registration.userId)!;
    expect(user.credentials.size).toBe(2);
    expect(user.credentials.has(passkey.credentialId)).toBe(true);
    // the hub excludes the existing passkey from the new registration
    expect(auth.lastCreate!.excludeCredentials).toHaveLength(1);
    const login = await signIn({ anon: app.anon, credentials: auth, origin: ORIGIN, now: () => T0 });
    expect(login.credentialId).toBe(passkey.credentialId);
    await app.begin(login);
    await app.unlock(start.identity);
    expect(await app.uploadPasskeySlot(login, start.identity)).toBe(true);
    const prepared = await newRecoveryCode(start.userId, start.identity);
    const request = (method: string, target: string, options?: { body: unknown }) =>
      app.client.request(method, target, options);
    await replaceRecoveryCode(request, prepared);
    expect(user.recoveryId).toBe(await recoveryId(parseRecoveryCode(prepared.code)));
    const reopened = await unlockWithRecoveryCode(start.userId, prepared.code, user.bundles.get('recovery')!);
    expect(reopened.public()).toEqual(registration.identity.public());
    // the old code is gone; the replacement does not happen twice
    expect(await codeOf(() => beginRecovery(client, registration.recoveryCode))).toBe('not_found');
    const again = await newRecoveryCode(start.userId, start.identity);
    expect(await codeOf(() => replaceRecoveryCode(request, again))).toBe('forbidden');
    // an answer lost after the replacement was stored: the repeat counts as done
    await replaceRecoveryCode(request, prepared);
  });

  it('turns 429 into the waiting time of Retry-After', async () => {
    const { hub, registration, client } = await setup();
    hub.retryAfter = '3600';
    const limited = await beginRecovery(client, registration.recoveryCode).catch((error: unknown) => error);
    expect(limited).toBeInstanceOf(RecoveryRateLimited);
    expect(limited).toMatchObject({ seconds: 3600, message: 'rate_limited' });
    expect(client.retryAfter()).toBe(3600);
    hub.retryAfter = '';
    expect(await beginRecovery(client, registration.recoveryCode).catch((error: unknown) => error)).toMatchObject({
      seconds: null,
    });
    hub.retryAfter = null;
    await beginRecovery(client, registration.recoveryCode);
    expect(client.retryAfter()).toBeNull();
  });

  it('passes other refusals of begin through and refuses a slot the code does not open', async () => {
    const { hub, registration, client } = await setup();
    hub.override.set('POST /v1/recovery/begin', new HubFailure(503, 'unavailable'));
    expect(await codeOf(() => beginRecovery(client, registration.recoveryCode))).toBe('unavailable');
    hub.override.delete('POST /v1/recovery/begin');
    const user = hub.users.get(registration.userId)!;
    const other = await newRecoveryCode(registration.userId, await Identity.generate());
    user.bundles.set('recovery', other.body.bundle_ct);
    expect(await codeOf(() => beginRecovery(client, registration.recoveryCode))).toBe('decrypt_failed');
  });

  it('refuses malformed answers of begin', async () => {
    const good = {
      user_id: RECOVERY.proof.user_id,
      bundle_ct: BUNDLE.recovery.sealed,
      challenge: RECOVERY.proof.challenge,
    };
    for (const answer of [
      null,
      [],
      { ...good, user_id: 'not-a-uuid' },
      { ...good, bundle_ct: 7 },
      { ...good, challenge: b64uEncode(new Uint8Array(16)) },
    ]) {
      const { client } = stubClient(() => answer);
      expect(await codeOf(() => beginRecovery(client, RECOVERY.text))).toBe('header_invalid');
    }
  });

  it('refuses malformed answers of finish and register/finish, and reports a cancelled gesture', async () => {
    const { hub, auth, registration, client } = await setup();
    const start = async (): Promise<Awaited<ReturnType<typeof beginRecovery>>> =>
      beginRecovery(client, registration.recoveryCode);
    for (const answer of [null, { ceremony_id: '' }, { ceremony_id: 5 }]) {
      hub.override.set('POST /v1/recovery/finish', () => answer);
      expect(await codeOf(async () => finishRecovery(client, auth, await start()))).toBe('header_invalid');
    }
    hub.override.delete('POST /v1/recovery/finish');
    // options of another user
    const real = await start();
    expect(
      await codeOf(() =>
        finishRecovery(client, auth, {
          ...real,
          userId: registration.userId.replace(/.$/, (last) => (last === '0' ? '1' : '0')),
        }),
      ),
    ).toBe('ceremony_invalid');
    const finish = hub.override;
    finish.set('POST /v1/passkeys/register/finish', () => ({ user_id: registration.userId, credential_id: 'other' }));
    expect(await codeOf(async () => finishRecovery(client, auth, await start()))).toBe('header_invalid');
    finish.set('POST /v1/passkeys/register/finish', () => null);
    expect(await codeOf(async () => finishRecovery(client, auth, await start()))).toBe('header_invalid');
    finish.delete('POST /v1/passkeys/register/finish');
    auth.refuse = 'NotAllowedError';
    await expect(finishRecovery(client, auth, await start())).rejects.toBeInstanceOf(CeremonyCancelled);
    // a forged proof is refused by the hub
    const forged = { ...(await start()), identity: await Identity.generate() };
    expect(await codeOf(() => finishRecovery(client, auth, forged))).toBe('signature_invalid');
  });

  it('refuses creation options bound to another user', async () => {
    const { hub, auth, registration, client } = await setup();
    // creation options the hub issued for somebody else (a new registration)
    const foreign = (await client.anon('POST', '/v1/passkeys/register/begin', {
      invite_id: await hub.addInviteId(),
    })) as {
      options: unknown;
    };
    hub.override.set('POST /v1/recovery/finish', () => ({ ceremony_id: 'c', options: foreign.options }));
    const start = await beginRecovery(client, registration.recoveryCode);
    expect(await codeOf(() => finishRecovery(client, auth, start))).toBe('header_invalid');
    // nothing was created for the foreign user
    expect(auth.credentials).toHaveLength(1);
  });
});

describe('recovery helpers', () => {
  it('words the waiting time in minutes below two hours and in hours beyond', () => {
    expect(waitText(0, 'en')).toBe('1 minute');
    expect(waitText(61, 'en')).toBe('2 minutes');
    expect(waitText(7140, 'en')).toBe('119 minutes');
    expect(waitText(7199, 'en')).toBe('2 hours');
    expect(waitText(86400, 'de')).toBe('24 Stunden');
  });

  it('maps refusals of begin onto messages or catalog codes', () => {
    expect(recoveryRefusal(new RecoveryRateLimited(90), 'en')).toEqual({
      key: 'recover.rate-limited',
      params: { time: '2 minutes' },
    });
    expect(recoveryRefusal(new RecoveryRateLimited(null), 'en')).toEqual({
      key: 'recover.rate-limited.later',
      params: {},
    });
    expect(recoveryRefusal(new HubError('not_found', 404), 'en')).toEqual({ key: 'recover.unknown', params: {} });
    expect(recoveryRefusal(new HubError('unavailable', 503), 'en')).toEqual({ code: 'unavailable' });
    expect(recoveryRefusal(new CryptoError('recovery_code_length'), 'en')).toEqual({ code: 'recovery_code_length' });
    expect(recoveryRefusal(new Error('x'), 'en')).toEqual({ code: 'unavailable' });
  });

  it('reads delta-seconds of Retry-After only', () => {
    expect(retryAfterSeconds(null)).toBeNull();
    expect(retryAfterSeconds(' 120 ')).toBe(120);
    expect(retryAfterSeconds('0')).toBe(0);
    expect(retryAfterSeconds('soon')).toBeNull();
    expect(retryAfterSeconds('Wed, 21 Oct 2026 07:28:00 GMT')).toBeNull();
    expect(retryAfterSeconds('12345678901')).toBeNull();
  });

  it('draws a new code whose slot opens to the identity', async () => {
    const identity = await Identity.generate();
    const prepared = await newRecoveryCode(RECOVERY.proof.user_id, identity);
    expect(prepared.code).toMatch(/^[A-Z2-7]{5}(-[A-Z2-7]{5}){3}-[A-Z2-7]{6}$/);
    expect(prepared.body.new_recovery_id).toBe(await recoveryId(parseRecoveryCode(prepared.code)));
    const opened = await unlockWithRecoveryCode(RECOVERY.proof.user_id, prepared.code, prepared.body.bundle_ct);
    expect(opened.public()).toEqual(identity.public());
    expect((await newRecoveryCode(RECOVERY.proof.user_id, identity)).code).not.toBe(prepared.code);
  });

  it('replaces only when the hub stored our slot', async () => {
    const prepared = await newRecoveryCode(RECOVERY.proof.user_id, await Identity.generate());
    const forbidden = new HubError('forbidden', 403);
    const run = (stored: unknown, first: unknown = forbidden) =>
      replaceRecoveryCode(async (method) => {
        if (method === 'PUT') throw first;
        return stored;
      }, prepared);
    await run({ bundle_ct: prepared.body.bundle_ct });
    await expect(run({ bundle_ct: 'other' })).rejects.toBe(forbidden);
    await expect(run(null)).rejects.toBe(forbidden);
    const unavailable = new HubError('unavailable', 503);
    await expect(run(null, unavailable)).rejects.toBe(unavailable);
    const plain = new Error('network');
    await expect(run(null, plain)).rejects.toBe(plain);
  });
});

describe('a new recovery code at any time (KOR-01)', () => {
  async function signedIn() {
    const context = await setup();
    const { app, auth, registration } = context;
    const login = await signIn({ anon: app.anon, credentials: auth, origin: ORIGIN, now: () => T0 });
    await app.begin(login);
    await app.unlock(registration.identity);
    const request = (method: string, target: string, options?: { body: unknown }) =>
      app.client.request(method, target, options);
    return { ...context, request };
  }

  it('proves the user with a passkey and replaces the old code with the new one', async () => {
    const { hub, auth, registration, client, request } = await signedIn();
    const user = hub.users.get(registration.userId)!;
    const prepared = await newRecoveryCode(registration.userId, registration.identity);
    await rotateRecoveryCode(request, auth, prepared);
    // the get() ran over the challenge of the ceremony
    const begun = hub.log.find((entry) => entry.target === '/v1/me/recovery/begin');
    expect(begun?.signed).toBe(true);
    expect(auth.lastGet!.challenge).toHaveLength(32);
    expect(auth.lastGet!.userVerification).toBe('required');
    const put = hub.log.filter((entry) => entry.target === '/v1/me/bundles/recovery').at(-1)!;
    const sent = JSON.parse(put.body) as Record<string, Record<string, string>>;
    expect(Object.keys(sent.assertion!).sort()).toEqual([
      'authenticator_data',
      'ceremony_id',
      'client_data_json',
      'credential_id',
      'signature',
    ]);
    expect(sent.assertion!.credential_id).toBe(registration.credentialId);
    // the code itself never left the browser
    expect(JSON.stringify(hub.log)).not.toContain(prepared.code);
    expect(user.recoveryId).toBe(await recoveryId(parseRecoveryCode(prepared.code)));
    expect(await codeOf(() => beginRecovery(client, registration.recoveryCode))).toBe('not_found');
    const start = await beginRecovery(client, prepared.code);
    expect(start.identity.public()).toEqual(registration.identity.public());
  });

  it('changes nothing when the gesture is cancelled or the hub refuses', async () => {
    const { hub, auth, registration, request } = await signedIn();
    const user = hub.users.get(registration.userId)!;
    const before = user.recoveryId;
    const prepared = await newRecoveryCode(registration.userId, registration.identity);
    auth.refuse = 'NotAllowedError';
    await expect(rotateRecoveryCode(request, auth, prepared)).rejects.toBeInstanceOf(CeremonyCancelled);
    auth.refuse = null;
    hub.override.set('PUT /v1/me/bundles/recovery', new HubFailure(400, 'ceremony_invalid'));
    expect(await codeOf(() => rotateRecoveryCode(request, auth, prepared))).toBe('ceremony_invalid');
    expect(user.recoveryId).toBe(before);
  });

  it('refuses malformed answers of begin', async () => {
    const { hub, auth, registration, request } = await signedIn();
    const prepared = await newRecoveryCode(registration.userId, registration.identity);
    const begin = 'POST /v1/me/recovery/begin';
    for (const answer of [null, { challenge: 'x' }, { ceremony_id: 'c', challenge: 'short', options: {} }]) {
      hub.override.set(begin, () => answer);
      expect(await codeOf(() => rotateRecoveryCode(request, auth, prepared))).toBe('header_invalid');
    }
  });
});
