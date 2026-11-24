// The browser device key (06-web.md section 3.3 option b, `core/persist/device-key.ts`) with fake-indexeddb: the key is
// non-extractable and survives the structured clone, the identity bundle opens only with it and only for its user,
// "Forget" deletes the entry, and malformed or tampered entries never unlock.
import { IDBFactory } from 'fake-indexeddb';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { b64uDecode, b64uEncode } from '../../../src/core/encoding/b64u';
import { concat, utf8 } from '../../../src/core/encoding/bytes';
import { CryptoError } from '../../../src/core/crypto/errors';
import { Identity } from '../../../src/core/crypto/keys';
import { P } from '../../../src/core/crypto/labels';
import { subtle } from '../../../src/core/crypto/subtle';
import { committed } from '../../../src/core/persist';
import {
  DEVICE_DB_NAME,
  deviceBundleAad,
  DeviceKeys,
  generateDeviceKey,
  isDeviceKey,
  openDeviceBundle,
  openDeviceDatabase,
  sealDeviceBundle,
} from '../../../src/core/persist/device-key';

const USER = '01930000-0000-7000-8000-000000000001';
const OTHER = '01930000-0000-7000-8000-000000000002';

const code = async (run: () => unknown): Promise<string> => {
  try {
    await run();
  } catch (error) {
    if (error instanceof CryptoError) return error.code;
    throw error;
  }
  throw new Error('no error');
};

async function putRaw(factory: IDBFactory, key: string, value: unknown): Promise<void> {
  const db = await openDeviceDatabase(factory);
  const transaction = db.transaction('keys', 'readwrite');
  transaction.objectStore('keys').put(value, key);
  await committed(transaction);
  db.close();
}

afterEach(() => vi.unstubAllGlobals());

describe('device key format', () => {
  it('generates a non-extractable AES-256-GCM key for encrypt and decrypt only', async () => {
    const key = await generateDeviceKey();
    expect(key.extractable).toBe(false);
    expect(isDeviceKey(key)).toBe(true);
    await expect(subtle.exportKey('raw', key)).rejects.toThrow();
  });

  it('binds the bundle to the prefix, the label and the user', async () => {
    expect(deviceBundleAad(USER)).toEqual(concat(P, utf8(`device-bundle\0${USER}`)));
    expect(await code(() => deviceBundleAad(''))).toBe('header_invalid');
    expect(await code(() => deviceBundleAad(7))).toBe('header_invalid');
    expect(await code(() => deviceBundleAad('ü'))).toBe('non_ascii');
  });

  it('seals deterministically with a fixed nonce and opens only for the same key and user', async () => {
    const identity = await Identity.generate();
    const key = await generateDeviceKey();
    const nonce = new Uint8Array(12).fill(7);
    const sealed = await sealDeviceBundle(key, USER, identity, nonce);
    expect(await sealDeviceBundle(key, USER, identity, nonce)).toBe(sealed);
    expect(b64uDecode(sealed).subarray(0, 12)).toEqual(nonce);
    expect((await openDeviceBundle(key, USER, sealed)).public()).toEqual(identity.public());
    expect(await code(() => openDeviceBundle(key, OTHER, sealed))).toBe('decrypt_failed');
    const otherKey = await generateDeviceKey();
    expect(await code(() => openDeviceBundle(otherKey, USER, sealed))).toBe('decrypt_failed');
    const tampered = b64uDecode(sealed);
    tampered[20]! ^= 1;
    expect(await code(() => openDeviceBundle(key, USER, b64uEncode(tampered)))).toBe('decrypt_failed');
  });

  it('accepts only usable device keys', async () => {
    const aes = (length: number, extractable: boolean, usages: KeyUsage[], name = 'AES-GCM') =>
      subtle.generateKey({ name, length }, extractable, usages) as Promise<CryptoKey>;
    expect(isDeviceKey(null)).toBe(false);
    expect(isDeviceKey({ type: 'secret' })).toBe(false);
    expect(isDeviceKey(await aes(256, true, ['encrypt', 'decrypt']))).toBe(false);
    expect(isDeviceKey(await aes(128, false, ['encrypt', 'decrypt']))).toBe(false);
    expect(isDeviceKey(await aes(256, false, ['encrypt']))).toBe(false);
    expect(isDeviceKey(await aes(256, false, ['decrypt', 'wrapKey']))).toBe(false);
    expect(isDeviceKey(await aes(256, false, ['encrypt', 'wrapKey']))).toBe(false);
    expect(isDeviceKey(await aes(256, false, ['encrypt', 'decrypt'], 'AES-CBC'))).toBe(false);
    const pair = (await subtle.generateKey({ name: 'Ed25519' }, false, ['sign', 'verify'])) as CryptoKeyPair;
    expect(isDeviceKey(pair.privateKey)).toBe(false);
  });
});

describe('DeviceKeys', () => {
  it('remembers, survives a reload, unlocks the same identity and forgets', async () => {
    const factory = new IDBFactory();
    const identity = await Identity.generate();
    const first = await DeviceKeys.open(factory, () => 1234);
    expect(await first.info(USER)).toBeNull();
    expect(await first.unlock(USER)).toBeNull();
    expect(await first.remember(USER, identity)).toEqual({ userId: USER, createdAt: 1234 });
    first.close();
    const second = await DeviceKeys.open(factory);
    expect(await second.info(USER)).toEqual({ userId: USER, createdAt: 1234 });
    expect(await second.info(OTHER)).toBeNull();
    expect((await second.unlock(USER))!.public()).toEqual(identity.public());
    await second.forget(USER);
    expect(await second.info(USER)).toBeNull();
    expect(await second.unlock(USER)).toBeNull();
    second.close();
  });

  it('stores the key itself non-extractable (structured clone of the CryptoKey)', async () => {
    const factory = new IDBFactory();
    const keys = await DeviceKeys.open(factory);
    await keys.remember(USER, await Identity.generate());
    const db = await openDeviceDatabase(factory);
    const stored = await new Promise<{ key: CryptoKey; bundle: string }>((resolve) => {
      const request = db.transaction('keys').objectStore('keys').get(USER);
      request.onsuccess = () => resolve(request.result as { key: CryptoKey; bundle: string });
    });
    db.close();
    expect(stored.key.extractable).toBe(false);
    await expect(subtle.exportKey('raw', stored.key)).rejects.toThrow();
    expect(Object.keys(stored).sort()).toEqual(['bundle', 'createdAt', 'key', 'userId']);
  });

  it('replaces an older entry of the same user', async () => {
    const keys = await DeviceKeys.open(new IDBFactory(), () => 1);
    await keys.remember(USER, await Identity.generate());
    const newer = await Identity.generate();
    await keys.remember(USER, newer);
    expect((await keys.unlock(USER))!.public()).toEqual(newer.public());
  });

  it('treats malformed entries as absent and refuses a tampered bundle', async () => {
    const factory = new IDBFactory();
    const key = await generateDeviceKey();
    const identity = await Identity.generate();
    const good = { userId: USER, key, bundle: await sealDeviceBundle(key, USER, identity), createdAt: 5 };
    const extractable = (await subtle.generateKey({ name: 'AES-GCM', length: 256 }, true, [
      'encrypt',
      'decrypt',
    ])) as CryptoKey;
    const keys = await DeviceKeys.open(factory);
    for (const value of [
      null,
      [good],
      'text',
      { ...good, userId: '' },
      { ...good, userId: 7 },
      { ...good, key: extractable },
      { ...good, bundle: 5 },
      { ...good, createdAt: -1 },
      { ...good, createdAt: 1.5 },
      { ...good, userId: OTHER },
    ]) {
      await putRaw(factory, USER, value);
      expect(await keys.info(USER)).toBeNull();
    }
    await putRaw(factory, USER, { ...good, bundle: b64uEncode(new Uint8Array(40)) });
    expect(await code(() => keys.unlock(USER))).toBe('decrypt_failed');
    await putRaw(factory, USER, good);
    expect((await keys.unlock(USER))!.public()).toEqual(identity.public());
  });

  it('opens the page IndexedDB by default in its own database', async () => {
    const factory = new IDBFactory();
    vi.stubGlobal('indexedDB', factory);
    const keys = await DeviceKeys.open();
    await keys.remember(USER, await Identity.generate());
    keys.close();
    const names = (await factory.databases()).map((db) => db.name);
    expect(names).toEqual([DEVICE_DB_NAME]);
  });
});
