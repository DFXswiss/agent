// U-WEB-crypto (primitives): errors, secrets, AES-GCM, HKDF derivations, HPKE and the raw key formats – every
// refusal path beyond the shared vectors, and the rule that the core uses WebCrypto only.
import { describe, expect, it } from 'vitest';
import { b64uEncode } from '../../../src/core/encoding/b64u';
import { concat, fromHex, utf8 } from '../../../src/core/encoding/bytes';
import * as aead from '../../../src/core/crypto/aead';
import { CRYPTO_CODES, CryptoError, fail, LOG_CODES } from '../../../src/core/crypto/errors';
import * as hpke from '../../../src/core/crypto/hpke';
import * as kdf from '../../../src/core/crypto/kdf';
import * as keys from '../../../src/core/crypto/keys';
import { Secret } from '../../../src/core/crypto/secret';
import { attempt, hmacSha256, random, sha256, subtle } from '../../../src/core/crypto/subtle';
import { codeOf } from './helpers';

const zero = (n: number) => new Uint8Array(n);
// little-endian 32-byte scalars of an Ed25519 signature's S half
const toBigInt = (raw: Uint8Array) => raw.reduceRight((value, byte) => (value << 8n) | BigInt(byte), 0n);
const fromBigInt = (value: bigint) =>
  Uint8Array.from({ length: 32 }, (_, i) => Number((value >> BigInt(8 * i)) & 0xffn));

describe('errors', () => {
  it('carries the code as message and name CryptoError', () => {
    const error = new CryptoError('decrypt_failed');
    expect([error.message, error.code, error.name]).toEqual(['decrypt_failed', 'decrypt_failed', 'CryptoError']);
    expect(() => fail('log_chain')).toThrow(CryptoError);
    expect(new Set([...CRYPTO_CODES, ...LOG_CODES]).size).toBe(CRYPTO_CODES.length + LOG_CODES.length);
  });
});

describe('Secret', () => {
  it('keeps its bytes private and hands out copies', () => {
    const raw = new Uint8Array([1, 2, 3]);
    const secret = Secret.of(raw);
    raw[0] = 9;
    expect(secret.reveal((bytes) => [...bytes])).toEqual([1, 2, 3]);
    secret.reveal((bytes) => bytes.fill(0));
    expect(secret.reveal((bytes) => bytes[0])).toBe(1);
    expect([secret.length, JSON.stringify({ secret }), String(secret)]).toEqual([
      3,
      '{"secret":"[secret]"}',
      '[secret]',
    ]);
    expect(Object.keys(secret)).toEqual([]);
    expect(Secret.random().length).toBe(32);
    expect(Secret.random(16).length).toBe(16);
  });
});

describe('subtle helpers', () => {
  it('hashes, MACs and draws random bytes', async () => {
    expect(b64uEncode(await sha256(utf8('abc')))).toBe('ungWv48Bz-pBQUDeXa4iI7ADYaOWF3qctBD_YfIAFa0');
    expect((await hmacSha256(zero(32), zero(1))).length).toBe(32);
    expect(random(5).length).toBe(5);
    expect(await attempt(async () => 1)).toBe(1);
    expect(await attempt(async () => Promise.reject(new Error('x')))).toBeNull();
  });
});

describe('AES-GCM', () => {
  it('imports keys from bytes or secrets and refuses other sizes', async () => {
    const key = await aead.aesKey(Secret.of(zero(32)));
    expect(key.extractable).toBe(false);
    expect(await codeOf(() => aead.aesKey(zero(16)))).toBe('header_invalid');
    expect(aead.randomNonce().length).toBe(12);
  });

  it('refuses wrong keys, nonces and tampered data', async () => {
    const key = await aead.aesKey(zero(32));
    const other = await aead.aesKey(new Uint8Array(32).fill(1));
    const blob = await aead.sealBlob(key, utf8('hello'), utf8('aad'));
    expect(await aead.openBlob(key, blob, utf8('aad'))).toEqual(utf8('hello'));
    expect(await codeOf(() => aead.openBlob(other, blob, utf8('aad')))).toBe('decrypt_failed');
    expect(await codeOf(() => aead.openBlob(key, blob, utf8('other')))).toBe('decrypt_failed');
    expect(await codeOf(() => aead.openBlob(key, blob.slice(0, 27), utf8('aad')))).toBe('decrypt_failed');
    expect(await codeOf(() => aead.seal(key, zero(16), zero(1), zero(0)))).toBe('header_invalid');
    expect(await codeOf(() => aead.open(key, zero(11), zero(16), zero(0)))).toBe('header_invalid');
    const hmac = await subtle.importKey('raw', zero(32), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
    expect(await codeOf(() => aead.seal(hmac, zero(12), zero(1), zero(0)))).toBe('header_invalid');
    const aes128 = await subtle.importKey('raw', zero(16), 'AES-GCM', false, ['encrypt']);
    expect(await codeOf(() => aead.seal(aes128, zero(12), zero(1), zero(0)))).toBe('header_invalid');
  });
});

describe('derivations', () => {
  it('refuses wrong input lengths with the codes of section 8', async () => {
    const s = (n: number) => Secret.of(zero(n));
    expect(await codeOf(() => kdf.kekFromPrf(s(31), zero(16)))).toBe('prf_length');
    expect(await codeOf(() => kdf.kekFromRecoveryCode(zero(17)))).toBe('recovery_code_length');
    expect(await codeOf(() => kdf.recoveryIdRaw(zero(15)))).toBe('recovery_code_length');
    expect(await codeOf(() => kdf.inviteIdRaw(zero(15)))).toBe('invite_code_length');
    expect(await codeOf(() => kdf.packageKey(zero(15)))).toBe('invite_code_length');
    expect(await codeOf(() => kdf.inviteSignSeed(zero(15)))).toBe('invite_code_length');
    expect(await codeOf(() => kdf.eventKey(s(31), zero(32)))).toBe('header_invalid');
    expect(await codeOf(() => kdf.eventKey(s(32), zero(31)))).toBe('header_invalid');
    expect(await codeOf(() => kdf.credentialKey(s(32), zero(33)))).toBe('header_invalid');
    expect(await codeOf(() => kdf.credentialKey(s(16), zero(32)))).toBe('header_invalid');
    expect(await codeOf(() => kdf.dedupeKey(s(16)))).toBe('header_invalid');
    expect(await codeOf(() => kdf.storageKey(s(16)))).toBe('header_invalid');
  });

  it('returns non-extractable keys and HKDF prefixes for shorter outputs', async () => {
    const key = await kdf.kekFromPrf(Secret.of(zero(32)), zero(16));
    expect(key.extractable).toBe(false);
    expect((await kdf.dedupeKey(Secret.of(zero(32)))).extractable).toBe(false);
    const long = await kdf.hkdf(zero(16), utf8('x'));
    expect(await kdf.hkdf(Secret.of(zero(16)), utf8('x'), new Uint8Array(0), 16)).toEqual(long.slice(0, 16));
    expect(await kdf.hkdf(zero(16), utf8('x'), zero(32))).toEqual(long);
  });
});

describe('HPKE', () => {
  it('seals to a key and opens it again', async () => {
    const recipient = await keys.Identity.generate();
    const sealed = await hpke.seal(recipient.kemPublic(), utf8('info'), utf8('secret'));
    expect(sealed.length).toBe(32 + 6 + 16);
    expect(await hpke.open(recipient.kem, utf8('info'), sealed)).toEqual(utf8('secret'));
    expect(await codeOf(() => hpke.open(recipient.kem, utf8('other'), sealed))).toBe('decrypt_failed');
    expect(await codeOf(() => hpke.open(recipient.kem, utf8('info'), sealed.slice(0, 47)))).toBe('hpke_short');
  });

  it('refuses malformed and small-order keys', async () => {
    expect(await codeOf(() => hpke.seal(zero(31), zero(0), zero(1)))).toBe('header_invalid');
    expect(await codeOf(() => hpke.seal(zero(32), zero(0), zero(1)))).toBe('decrypt_failed');
    expect(await codeOf(() => hpke.setupValues(zero(32), zero(33), zero(0)))).toBe('header_invalid');
    expect([hpke.KEM_ID, hpke.KDF_ID, hpke.AEAD_ID, hpke.ENC_BYTES]).toEqual([0x20, 1, 2, 32]);
  });
});

describe('keys', () => {
  it('imports raw private keys as non-extractable keys', async () => {
    const pair = await keys.x25519KeyPair(zero(32));
    expect(pair.privateKey.extractable).toBe(false);
    expect(pair.publicKey.length).toBe(32);
    expect(await codeOf(() => keys.x25519KeyPair(zero(31)))).toBe('header_invalid');
    expect(await codeOf(() => keys.ed25519KeyPair(zero(33)))).toBe('header_invalid');
  });

  it('generates identities that sign and expose only public keys', async () => {
    const identity = await keys.Identity.generate();
    const message = utf8('m');
    const signature = await identity.sign(message);
    await keys.ed25519Verify(identity.sigPublic(), signature, message);
    expect(JSON.stringify(identity)).toBe(JSON.stringify(identity.public()));
    expect(identity.revealPrivate((kem, sig) => [kem.length, sig.length])).toEqual([32, 32]);
    const copy = identity.kemPublic();
    copy[0] = copy[0]! ^ 1;
    expect(identity.kemPublic()).not.toEqual(copy);
  });

  it('refuses every malformed Ed25519 signature or key', async () => {
    const identity = await keys.Identity.generate();
    const message = utf8('m');
    const signature = await identity.sign(message);
    expect(await codeOf(() => keys.ed25519Verify(zero(31), signature, message))).toBe('signature_invalid');
    expect(await codeOf(() => keys.ed25519Verify(identity.sigPublic(), signature.slice(1), message))).toBe(
      'signature_invalid',
    );
    expect(await codeOf(() => keys.ed25519Verify(identity.sigPublic(), signature, utf8('x')))).toBe(
      'signature_invalid',
    );
    // 2**255 - 1 is not a point encoding WebCrypto accepts
    expect(await codeOf(() => keys.ed25519Verify(new Uint8Array(32).fill(0xff), signature, message))).toBe(
      'signature_invalid',
    );
    expect(await codeOf(() => keys.ed25519VerifyB64u(identity.sigPublic(), 42, message))).toBe('signature_invalid');
    expect(await codeOf(() => keys.ed25519VerifyB64u(identity.sigPublic(), 'a=', message))).toBe('signature_invalid');
    await keys.ed25519VerifyB64u(identity.sigPublic(), b64uEncode(signature), message);
  });

  // The libsodium blacklist: 8 torsion points plus the non-canonical y = p, p + 1 (twin of the Python test).
  const smallOrder = [
    '0000000000000000000000000000000000000000000000000000000000000000',
    '0100000000000000000000000000000000000000000000000000000000000000',
    '26e8958fc2b227b045c3f489f2ef98f0d5dfac05d3c63339b13802886d53fc05',
    'c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac037a',
    'ecffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f',
    'edffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f',
    'eeffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f',
  ].map(fromHex);
  const withSignBit = (key: Uint8Array) => concat(key.slice(0, 31), new Uint8Array([key[31]! | 0x80]));

  it('refuses every small-order Ed25519 key (R = key, S = 0) in both sign-bit forms', async () => {
    for (const key of [...smallOrder, ...smallOrder.map(withSignBit)]) {
      for (const message of [zero(0), utf8('m'), utf8('any message')]) {
        expect(await codeOf(() => keys.ed25519Verify(key, concat(key, zero(32)), message))).toBe('signature_invalid');
        expect(await codeOf(() => keys.ed25519VerifyB64u(key, b64uEncode(concat(key, zero(32))), message))).toBe(
          'signature_invalid',
        );
      }
    }
  });

  it('refuses a non-canonical Ed25519 S (S >= L)', async () => {
    const identity = await keys.Identity.generate();
    const message = utf8('m');
    const signature = await identity.sign(message);
    await keys.ed25519Verify(identity.sigPublic(), signature, message);
    const s = toBigInt(signature.slice(32));
    for (const bad of [s + keys.ED25519_ORDER, keys.ED25519_ORDER, 2n ** 256n - 1n]) {
      const forged = concat(signature.slice(0, 32), fromBigInt(bad));
      expect(await codeOf(() => keys.ed25519Verify(identity.sigPublic(), forged, message))).toBe('signature_invalid');
    }
  });

  it('creates non-extractable session keys and verifies raw P-256 signatures strictly', async () => {
    const session = await keys.generateSessionKey();
    expect(session.privateKey.extractable).toBe(false);
    expect([session.publicKey.length, session.publicKey[0]]).toEqual([65, 4]);
    const message = utf8('statement');
    const signature = await keys.p256SignRaw(session.privateKey, message);
    await keys.p256VerifyRaw(session.publicKey, signature, message);
    const order = Uint8Array.from(keys.P256_ORDER.toString(16).match(/../g)!, (h) => parseInt(h, 16));
    const bad: [Uint8Array, Uint8Array][] = [
      [session.publicKey.slice(0, 64), signature],
      [Uint8Array.of(2, ...session.publicKey.slice(1)), signature],
      [session.publicKey, signature.slice(0, 63)],
      [session.publicKey, Uint8Array.of(...order, ...signature.slice(32))],
      [session.publicKey, Uint8Array.of(...signature.slice(0, 32), ...zero(32))],
      [session.publicKey, Uint8Array.of(...signature.slice(0, 32), ...order)],
      [Uint8Array.of(4, ...zero(64)), signature],
    ];
    for (const [point, sig] of bad)
      expect(await codeOf(() => keys.p256VerifyRaw(point, sig, message))).toBe('signature_invalid');
    expect(await codeOf(() => keys.p256VerifyRaw(session.publicKey, signature, utf8('other')))).toBe(
      'signature_invalid',
    );
  });
});
