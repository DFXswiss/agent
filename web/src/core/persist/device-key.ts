// The optional browser device key (06-web.md sections 2.2 and 3.3, 04-crypto.md section 5.4, ADR-006): "Remember this
// browser" for passkeys without PRF. A non-extractable AES-256-GCM `CryptoKey` is generated in WebCrypto and stored by
// structured clone in IndexedDB together with the identity bundle it encrypts; its key bytes never reach JavaScript
// and the bundle never leaves the browser (the hub knows nothing about it). The next sign-in on this browser profile
// opens the bundle with it instead of the recovery code. "Forget" deletes the entry.
//
// Format (local only): `b64u(nonce12 || AES-GCM(device key, bundle plaintext, AAD))` with the bundle plaintext of
// 04-crypto.md section 4.1 and `AAD = P || "device-bundle\0" || user_id`. One entry per user and browser profile, in a
// database of its own (`agent-platform-device`), so sign-out (which clears the session stores) keeps it.
import { b64uDecode, b64uEncode } from '../encoding/b64u';
import { concat, utf8 } from '../encoding/bytes';
import { isPrintableAscii } from '../encoding/json';
import * as aead from '../crypto/aead';
import { bundlePlaintext, parseBundlePlaintext } from '../crypto/bundle';
import { fail } from '../crypto/errors';
import type { Identity } from '../crypto/keys';
import { label } from '../crypto/labels';
import { subtle } from '../crypto/subtle';
import { committed, done } from './index';

export const DEVICE_DB_NAME = 'agent-platform-device';
export const DEVICE_DB_VERSION = 1;
const KEYS = 'keys';
export const LABEL_DEVICE_BUNDLE = 'device-bundle\0';

/** What is stored per user. */
export interface DeviceEntry {
  readonly userId: string;
  /** Non-extractable AES-256-GCM key (encrypt/decrypt). */
  readonly key: CryptoKey;
  /** b64u `nonce || ciphertext` of the identity bundle. */
  readonly bundle: string;
  /** Unix ms of "Remember this browser". */
  readonly createdAt: number;
}

/** What the settings show: no key material. */
export interface DeviceInfo {
  readonly userId: string;
  readonly createdAt: number;
}

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);

/** A usable device key: AES-GCM 256, not extractable, exactly encrypt and decrypt. */
export function isDeviceKey(value: unknown): value is CryptoKey {
  if (!(value instanceof CryptoKey) || value.extractable || value.type !== 'secret') return false;
  const algorithm = value.algorithm as Partial<AesKeyAlgorithm>;
  const usages = [...value.usages].sort();
  return (
    algorithm.name === 'AES-GCM' &&
    algorithm.length === 256 &&
    usages.length === 2 &&
    usages[0] === 'decrypt' &&
    usages[1] === 'encrypt'
  );
}

function isEntry(value: unknown): value is DeviceEntry {
  return (
    isRecord(value) &&
    typeof value.userId === 'string' &&
    value.userId !== '' &&
    isDeviceKey(value.key) &&
    typeof value.bundle === 'string' &&
    Number.isSafeInteger(value.createdAt) &&
    (value.createdAt as number) >= 0
  );
}

/** `P || "device-bundle\0" || user_id` (`header_invalid`, `non_ascii`). */
export function deviceBundleAad(userId: unknown): Uint8Array<ArrayBuffer> {
  if (typeof userId !== 'string' || userId === '') fail('header_invalid');
  if (!isPrintableAscii(userId)) fail('non_ascii');
  return concat(label(LABEL_DEVICE_BUNDLE), utf8(userId));
}

/** A new non-extractable AES-256-GCM key. */
export function generateDeviceKey(): Promise<CryptoKey> {
  return subtle.generateKey({ name: 'AES-GCM', length: 256 }, false, ['encrypt', 'decrypt']) as Promise<CryptoKey>;
}

/** Encrypts the identity under the device key; `nonce` only for tests. */
export async function sealDeviceBundle(
  key: CryptoKey,
  userId: string,
  identity: Identity,
  nonce?: Uint8Array,
): Promise<string> {
  const aad = deviceBundleAad(userId);
  return b64uEncode(await aead.sealBlob(key, bundlePlaintext(identity), aad, nonce));
}

/** Decrypts a device bundle (`decrypt_failed` for another key or user) and parses the identity. */
export async function openDeviceBundle(key: CryptoKey, userId: string, sealed: string): Promise<Identity> {
  const aad = deviceBundleAad(userId);
  return parseBundlePlaintext(await aead.openBlob(key, b64uDecode(sealed), aad));
}

/** Opens (and creates) the device-key database. */
export function openDeviceDatabase(factory: IDBFactory = globalThis.indexedDB): Promise<IDBDatabase> {
  const request = factory.open(DEVICE_DB_NAME, DEVICE_DB_VERSION);
  request.onupgradeneeded = () => {
    request.result.createObjectStore(KEYS);
  };
  return done(request);
}

/** The device keys of this browser profile. */
export class DeviceKeys {
  readonly #db: IDBDatabase;
  readonly #now: () => number;

  constructor(db: IDBDatabase, now: () => number = Date.now) {
    this.#db = db;
    this.#now = now;
  }

  static async open(factory?: IDBFactory, now?: () => number): Promise<DeviceKeys> {
    return new DeviceKeys(await openDeviceDatabase(factory), now);
  }

  close(): void {
    this.#db.close();
  }

  async #entry(userId: string): Promise<DeviceEntry | null> {
    const value = await done(this.#db.transaction(KEYS, 'readonly').objectStore(KEYS).get(userId));
    return isEntry(value) && value.userId === userId ? value : null;
  }

  /** "Remember this browser": a new device key encrypts `identity` (replaces an older entry of the user). */
  async remember(userId: string, identity: Identity): Promise<DeviceInfo> {
    const key = await generateDeviceKey();
    const entry: DeviceEntry = {
      userId,
      key,
      bundle: await sealDeviceBundle(key, userId, identity),
      createdAt: this.#now(),
    };
    const transaction = this.#db.transaction(KEYS, 'readwrite');
    transaction.objectStore(KEYS).put(entry, userId);
    await committed(transaction);
    return { userId, createdAt: entry.createdAt };
  }

  /** The entry of `userId` without key material, or null. */
  async info(userId: string): Promise<DeviceInfo | null> {
    const entry = await this.#entry(userId);
    return entry === null ? null : { userId, createdAt: entry.createdAt };
  }

  /**
   * The identity of `userId` from this browser, or null without a usable entry. An entry that does not open (another
   * browser's data, tampering) is `decrypt_failed`.
   */
  async unlock(userId: string): Promise<Identity | null> {
    const entry = await this.#entry(userId);
    return entry === null ? null : openDeviceBundle(entry.key, userId, entry.bundle);
  }

  /** "Forget": deletes the entry of `userId`. */
  async forget(userId: string): Promise<void> {
    const transaction = this.#db.transaction(KEYS, 'readwrite');
    transaction.objectStore(KEYS).delete(userId);
    await committed(transaction);
  }
}
