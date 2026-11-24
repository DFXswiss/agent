// U-WEB-crypto (formats): bundle, wrap, event, credential record, device request, invitation, recovery, enrollment and
// dedupe – the refusal paths and round trips beyond the shared vectors.
import { describe, expect, it } from 'vitest';
import { b64uDecode, b64uEncode } from '../../../src/core/encoding/b64u';
import { utf8 } from '../../../src/core/encoding/bytes';
import { uuid7 } from '../../../src/core/encoding/ids';
import { bodyJson, canonical } from '../../../src/core/encoding/json';
import * as aead from '../../../src/core/crypto/aead';
import * as bundle from '../../../src/core/crypto/bundle';
import * as credential from '../../../src/core/crypto/credential';
import * as dedupe from '../../../src/core/crypto/dedupe';
import * as enroll from '../../../src/core/crypto/enroll';
import * as event from '../../../src/core/crypto/event';
import * as F from '../../../src/core/crypto/fields';
import * as hpke from '../../../src/core/crypto/hpke';
import * as invite from '../../../src/core/crypto/invite';
import * as kdf from '../../../src/core/crypto/kdf';
import { Identity } from '../../../src/core/crypto/keys';
import * as recovery from '../../../src/core/crypto/recovery';
import * as request from '../../../src/core/crypto/request';
import { Secret } from '../../../src/core/crypto/secret';
import * as signed from '../../../src/core/crypto/signed';
import * as wrap from '../../../src/core/crypto/wrap';
import { actor, actorId, codeOf } from './helpers';
import { VECTOR_FILES } from './vector-suite';

const zero = (n: number) => new Uint8Array(n);
const ORG = uuid7(1_795_000_000_000, zero(10));
const WG = uuid7(1_795_000_000_001, zero(10));
const TS = '2026-11-21T12:00:00.000Z';

describe('fields', () => {
  it('checks key sets, integers, kinds and texts', async () => {
    expect(await codeOf(() => F.requireKeys([], []))).toBe('header_invalid');
    expect(await codeOf(() => F.requireKeys({ a: 1, b: 2 }, ['a', 'c']))).toBe('header_invalid');
    expect(await codeOf(() => F.requireKeys({ a: 1 }, ['a', 'b']))).toBe('header_invalid');
    expect(F.requireInt(5, 1, 5)).toBe(5);
    for (const value of [true, '1', 1.5, 0, 6])
      expect(await codeOf(() => F.requireInt(value, 1, 5))).toBe('header_invalid');
    expect(await codeOf(() => F.requireBytes(zero(3), 4, 'prf_length'))).toBe('prf_length');
    for (const value of ['a'.repeat(65), 'A.b', 'a..b', 'a.', 7])
      expect(await codeOf(() => F.kind(value))).toBe('header_invalid');
    expect(F.kind('session.output')).toBe('session.output');
    expect(await codeOf(() => F.text(''))).toBe('header_invalid');
    expect(await codeOf(() => F.literal(1, 'x'))).toBe('header_invalid');
    expect(F.uuidOrEmpty('')).toBe('');
  });
});

describe('identity bundle', () => {
  it('refuses malformed slots, users and plaintexts', async () => {
    const alice = await actor('alice');
    const kek = await aead.aesKey(zero(32));
    expect(await codeOf(() => bundle.passkeySlot(zero(0)))).toBe('header_invalid');
    for (const slot of ['passkey:', 'passkey:Zg=', 'other', 7])
      expect(await codeOf(() => bundle.checkSlot(slot))).toBe('header_invalid');
    expect(await codeOf(() => bundle.bundleAad('', 'recovery'))).toBe('header_invalid');
    expect(await codeOf(() => bundle.bundleAad(7, 'recovery'))).toBe('header_invalid');
    const sealed = await bundle.sealBundle(kek, 'u', 'recovery', alice);
    expect((await bundle.openBundle(kek, 'u', 'recovery', sealed)).public()).toEqual(alice.public());
    const aad = bundle.bundleAad('u', 'recovery');
    const plain = bundle.bundlePlaintext(alice);
    const spaced = utf8(new TextDecoder().decode(plain).replace(',', ', '));
    const reordered = await aead.sealBlob(kek, spaced, aad);
    expect(await codeOf(() => bundle.openBundle(kek, 'u', 'recovery', b64uEncode(reordered)))).toBe('header_invalid');
    const shortKey = canonical({ kem: b64uEncode(zero(31)), sig: b64uEncode(zero(32)), v: 1 });
    const short = await aead.sealBlob(kek, shortKey, aad);
    expect(await codeOf(() => bundle.openBundle(kek, 'u', 'recovery', b64uEncode(short)))).toBe('header_invalid');
  });
});

describe('wrap', () => {
  const target = { scope: 'wg', scope_id: WG, epoch: 1, recipient: 'r' };

  it('wraps for a recipient and binds the target', async () => {
    const bob = await actor('bob');
    const alice = await actor('alice');
    const key = Secret.random();
    const sealed = await wrap.wrapKey(bob.kemPublic(), target, key);
    const opened = await wrap.unwrapKey(bob.kem, target, sealed);
    expect(opened.reveal((raw) => raw)).toEqual(key.reveal((raw) => raw));
    const sig = await wrap.signWrap(alice.sig.privateKey, target, sealed);
    await wrap.verifyWrap(alice.sigPublic(), target, sealed, sig);
    expect(await codeOf(() => wrap.verifyWrap(bob.sigPublic(), target, sealed, sig))).toBe('signature_invalid');
  });

  it('refuses malformed targets, keys and wrapped values', async () => {
    const bob = await actor('bob');
    for (const bad of [
      { ...target, scope: 'task' },
      { ...target, scope_id: '' },
      { ...target, recipient: '' },
      { ...target, scope_id: 7 as unknown as string },
      { ...target, recipient: null as unknown as string },
      { ...target, epoch: 0 },
    ]) {
      expect(await codeOf(() => wrap.wrapInfo(bad))).toBe('header_invalid');
    }
    expect(await codeOf(() => wrap.wrapKey(bob.kemPublic(), target, Secret.random(16)))).toBe('header_invalid');
    expect(await codeOf(() => wrap.wrapSignatureText(target, 'x='))).toBe('header_invalid');
    const notAKey = b64uEncode(await hpke.seal(bob.kemPublic(), wrap.wrapInfo(target), zero(31)));
    expect(await codeOf(() => wrap.unwrapKey(bob.kem, target, notAKey))).toBe('decrypt_failed');
  });
});

const header = (changes: Record<string, unknown> = {}) =>
  event.newHeader({
    id: uuid7(),
    org: ORG,
    scope: 'wg',
    scope_id: WG,
    epoch: 1,
    author: actorId('alice'),
    kind: 'session.terminal',
    ts: TS,
    lh: 0,
    wg: WG,
    ...changes,
  });

describe('events', () => {
  it('seals with fresh salt and nonce and opens again', async () => {
    const alice = await actor('alice');
    const key = Secret.random();
    const fields = header({ ref: 'operation:1/x' });
    const first = await event.sealEvent(key, fields, utf8('{}'), alice.sig.privateKey);
    const second = await event.sealEvent(key, fields, utf8('{}'), alice.sig.privateKey);
    expect(first.header.salt).not.toBe(second.header.salt);
    expect(await event.openEvent(key, first, alice.sigPublic())).toEqual(utf8('{}'));
    const org = event.newHeader({ ...header(), scope: 'org', scope_id: ORG, wg: WG, kind: 'wg.profile' } as never);
    expect(org.wg).toBe(WG);
  });

  it('refuses headers with salt or nonce, bad refs and oversized terminal chunks', async () => {
    const alice = await actor('alice');
    const fields = header();
    expect(
      await codeOf(() => event.sealEvent(Secret.random(), { ...fields, salt: 'x' }, zero(1), alice.sig.privateKey)),
    ).toBe('header_invalid');
    expect(await codeOf(() => signed.checkFields(event.FORMAT, null))).toBe('header_invalid');
    for (const ref of ['a b', 'x'.repeat(201), 7]) expect(await codeOf(() => header({ ref }))).toBe('header_invalid');
    const big = zero(event.MAX_TERMINAL_CT_BYTES);
    expect(await codeOf(() => event.sealEvent(Secret.random(), fields, big, alice.sig.privateKey))).toBe(
      'size_exceeded',
    );
    const fine = await event.sealEvent(Secret.random(), header({ kind: 'session.output' }), big, alice.sig.privateKey);
    expect(event.maxCtBytes(fine.header)).toBe(event.MAX_CT_BYTES);
  });
});

describe('credential records', () => {
  it('round-trips and refuses an oversized body', async () => {
    const alice = await actor('alice');
    const key = Secret.random();
    const fields = credential.newHeader({
      id: uuid7(),
      owner: actorId('alice'),
      kind: 'mailbox',
      version: 2,
      epoch: 1,
      author: actorId('alice'),
      ts: TS,
    });
    const record = await credential.sealCredential(key, fields, utf8('{"x":1}'), alice.sig.privateKey);
    expect(await credential.verifyCredential(record, alice.sigPublic())).toEqual(record.header);
    expect(await credential.openCredential(key, record, alice.sigPublic())).toEqual(utf8('{"x":1}'));
    const big = zero(credential.MAX_CT_BYTES);
    expect(await codeOf(() => credential.sealCredential(key, fields, big, alice.sig.privateKey))).toBe('size_exceeded');
  });
});

describe('device requests', () => {
  const requestHeader = {
    v: 1,
    id: uuid7(),
    user: uuid7(),
    device: uuid7(),
    kind: 'github.connect',
    follows: '',
    ts: TS,
    enc_alg: request.ENC_ALG,
  };

  it('round-trips requests and responses with fresh ephemeral keys', async () => {
    const alice = await actor('alice');
    const device = await actor('device');
    const env = await request.sealRequest(requestHeader, device.kemPublic(), utf8('{}'), alice.sig.privateKey);
    expect(await request.verifyRequest(env, alice.sigPublic())).toEqual(requestHeader);
    expect(await request.openRequest(device.kem, env, alice.sigPublic())).toEqual(utf8('{}'));
    const responseHeader = { ...requestHeader, in_reply_to: requestHeader.id, seq: 1, final: true } as Record<
      string,
      unknown
    >;
    delete responseHeader.follows;
    const response = await request.sealResponse(responseHeader, alice.kemPublic(), utf8('{}'), device.sig.privateKey);
    expect(await request.verifyResponse(response, device.sigPublic())).toEqual(responseHeader);
  });

  it('refuses oversized ciphertexts on both sides', async () => {
    const alice = await actor('alice');
    const device = await actor('device');
    const big = zero(request.MAX_CT_BYTES);
    expect(await codeOf(() => request.sealRequest(requestHeader, device.kemPublic(), big, alice.sig.privateKey))).toBe(
      'size_exceeded',
    );
    const envelope = { header: requestHeader, ct: b64uEncode(zero(request.MAX_CT_BYTES + 1)), sig: '' };
    expect(await codeOf(() => request.verifyRequest(envelope, alice.sigPublic()))).toBe('size_exceeded');
  });
});

describe('invitations', () => {
  it('generates, formats and parses codes', async () => {
    const code = invite.generateCode();
    expect(code.length).toBe(16);
    expect(invite.parseCode(invite.formatCode(code))).toEqual(code);
    expect(await codeOf(() => invite.formatCode(zero(15)))).toBe('invite_code_length');
    expect(await codeOf(() => invite.parseCode(7))).toBe('invite_code_invalid');
  });

  it('checks the package structure', async () => {
    const pkg = VECTOR_FILES.invite.package;
    const bad = [
      { ...pkg, org_name: 7 },
      { ...pkg, wg_names: [] },
      { ...pkg, wg_names: { [WG]: 7 } },
      { ...pkg, wg_names: { 'not-a-uuid': 'x' } },
      { ...pkg, keys: [] },
      { ...pkg, keys: {} },
      { ...pkg, keys: [{ ...pkg.keys[0], scope: 'cred' }] },
      { ...pkg, hub_key: { ...pkg.hub_key, id: 'Hub' } },
      { ...pkg, hub_key: { ...pkg.hub_key, id: 7 } },
      { ...pkg, message: '' },
      { ...pkg, message: 'a\nb' },
    ];
    for (const value of bad) expect(await codeOf(() => invite.checkPackage(value))).toBe('header_invalid');
    expect(invite.checkPackage(pkg)).toBe(pkg);
    const withMessage = { ...pkg, message: 'Hallo <b>Bob</b>' };
    expect(invite.checkPackage(withMessage)).toBe(withMessage);
  });

  it('passes on failures that are not refusals', async () => {
    const secrets = await invite.inviteSecrets(zero(16));
    const broken = { ...secrets, packageKey: {} as CryptoKey };
    await expect(invite.openPackage(broken, b64uEncode(zero(40)))).rejects.toThrow(TypeError);
  });
});

describe('recovery', () => {
  it('generates codes and refuses malformed proofs', async () => {
    const code = recovery.generateRecoveryCode();
    expect(recovery.parseRecoveryCode(recovery.formatRecoveryCode(code))).toEqual(code);
    expect(await codeOf(() => recovery.formatRecoveryCode(zero(17)))).toBe('recovery_code_length');
    expect(await codeOf(() => recovery.proofText('', zero(32)))).toBe('header_invalid');
    expect(await codeOf(() => recovery.proofText(7, zero(32)))).toBe('header_invalid');
    expect(await codeOf(() => recovery.proofText('usér', zero(32)))).toBe('non_ascii');
    expect(recovery.CHALLENGE_BYTES).toBe(32);
  });
});

describe('enrollment', () => {
  it('refuses a body that is no object or does not name the signing device', async () => {
    const device = await actor('device');
    const other = await Identity.generate();
    const body = VECTOR_FILES.enroll.body;
    expect(await codeOf(() => enroll.enrollmentText([]))).toBe('header_invalid');
    expect(await codeOf(() => enroll.signEnrollment(body, other.sig, device.sig))).toBe('header_invalid');
    const signedBody = await enroll.signEnrollment(body, device.sig, other.sig);
    await enroll.verifyEnrollment(signedBody, other.sigPublic());
  });
});

describe('dedupe', () => {
  it('normalises non-ASCII values and refuses malformed input', async () => {
    const fields = await dedupe.dedupeFields('github.create_branch', { repo: 'Ä/B', branch: 'main' });
    expect(fields.repo).toBe(`sha256:${await dedupe.sha256Hex('Ä/B')}`);
    expect(await codeOf(() => dedupe.sha256Hex('\ud800'))).toBe('header_invalid');
    expect(await codeOf(() => dedupe.dedupeFields('github.push', []))).toBe('header_invalid');
    expect(await codeOf(() => dedupe.dedupeFields('toString', {}))).toBe('header_invalid');
    expect(await codeOf(() => dedupe.idempotencyFields('', uuid7()))).toBe('header_invalid');
    const idem = await dedupe.idempotencyFields('ключ', uuid7());
    expect(String(idem.idempotency_key).startsWith('sha256:')).toBe(true);
    const dk = await kdf.dedupeKey(Secret.random());
    expect(b64uDecode(await dedupe.dedupeTag(dk, 'github.push', fields)).length).toBe(32);
    expect(bodyJson(dedupe.DEDUPE_TYPES)).toEqual(bodyJson([...dedupe.DEDUPE_TYPES].sort()));
  });

  it('hashes the optional body of github.create_issue (empty when left out)', async () => {
    const empty = await dedupe.sha256Hex('');
    const withBody = await dedupe.dedupeFields('github.create_issue', { repo: 'A/B', title: 'T', body: 'text' });
    expect(withBody).toEqual({ repo: 'a/b', title: await dedupe.sha256Hex('T'), body: await dedupe.sha256Hex('text') });
    const without = await dedupe.dedupeFields('github.create_issue', { repo: 'a/b', title: 'T' });
    expect(without.body).toBe(empty);
    const blank = await dedupe.dedupeFields('github.create_issue', { repo: 'a/b', title: 'T', body: '' });
    expect(blank.body).toBe(empty);
    expect(await codeOf(() => dedupe.dedupeFields('github.create_issue', { repo: 'a/b', title: 'T', body: 1 }))).toBe(
      'header_invalid',
    );
  });
});
