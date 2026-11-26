// IndexedDB persistence with fake-indexeddb: exactly the listed items, a non-extractable session key that survives
// the structured clone, sign-out, and refusal of malformed or extractable values.
import { IDBFactory } from 'fake-indexeddb';
import { describe, expect, it } from 'vitest';
import { generateSessionKey } from '../../../src/core/crypto/keys';
import { subtle } from '../../../src/core/crypto/subtle';
import { utf8 } from '../../../src/core/encoding/bytes';
import { p256VerifyRaw, p256SignRaw } from '../../../src/core/crypto/keys';
import { committed, DB_NAME, done, openDatabase, Persist, type StoredSession } from '../../../src/core/persist';

async function session(): Promise<StoredSession> {
  const pair = await generateSessionKey();
  return {
    keyId: 'session.0123456789abcdef0123456789abcdef',
    privateKey: pair.privateKey,
    publicKey: pair.publicKey,
    userId: '01930000-0000-7000-8000-000000000001',
    credentialId: 'Y3JlZA',
    expiresAt: 1_800_000_000_000,
  };
}

async function raw(factory: IDBFactory, store: string, key: string, value: unknown): Promise<void> {
  const db = await openDatabase(factory);
  const transaction = db.transaction(store, 'readwrite');
  transaction.objectStore(store).put(value, key);
  await committed(transaction);
  db.close();
}

describe('persist', () => {
  it('stores the session key non-extractable and it still signs after a reload', async () => {
    const factory = new IDBFactory();
    const first = await Persist.open(factory);
    expect(await first.loadSession()).toBeNull();
    const stored = await session();
    await first.saveSession(stored);
    first.close();
    const second = await Persist.open(factory);
    const loaded = (await second.loadSession())!;
    expect(loaded.keyId).toBe(stored.keyId);
    expect(loaded.privateKey.extractable).toBe(false);
    await expect(subtle.exportKey('pkcs8', loaded.privateKey)).rejects.toThrow();
    const message = utf8('agent-request-v1');
    await p256VerifyRaw(loaded.publicKey, await p256SignRaw(loaded.privateKey, message), message);
    second.close();
  });

  it('refuses to store an extractable or malformed session', async () => {
    const persist = await Persist.open(new IDBFactory());
    const extractable = (await subtle.generateKey({ name: 'ECDSA', namedCurve: 'P-256' }, true, [
      'sign',
    ])) as CryptoKeyPair;
    await expect(persist.saveSession({ ...(await session()), privateKey: extractable.privateKey })).rejects.toThrow(
      'extractable',
    );
    await expect(persist.saveSession({ ...(await session()), keyId: '' })).rejects.toThrow();
    await expect(persist.saveSession({ ...(await session()), publicKey: new Uint8Array(64) })).rejects.toThrow();
    const ed = (await subtle.generateKey({ name: 'Ed25519' }, false, ['sign'])) as CryptoKeyPair;
    await expect(persist.saveSession({ ...(await session()), privateKey: ed.privateKey })).rejects.toThrow();
    const p384 = (await subtle.generateKey({ name: 'ECDSA', namedCurve: 'P-384' }, false, ['sign'])) as CryptoKeyPair;
    await expect(persist.saveSession({ ...(await session()), privateKey: p384.privateKey })).rejects.toThrow();
    await expect(persist.saveSession({ ...(await session()), privateKey: p384.publicKey })).rejects.toThrow();
    const both = (await subtle.generateKey({ name: 'ECDSA', namedCurve: 'P-256' }, false, [
      'sign',
      'verify',
    ])) as CryptoKeyPair;
    await expect(persist.saveSession({ ...(await session()), privateKey: both.publicKey })).rejects.toThrow();
    const ecdh = (await subtle.generateKey({ name: 'ECDH', namedCurve: 'P-256' }, false, [
      'deriveBits',
    ])) as CryptoKeyPair;
    await expect(persist.saveSession({ ...(await session()), privateKey: ecdh.privateKey })).rejects.toThrow();
    await expect(persist.saveSession({ ...(await session()), renewedAt: -1 })).rejects.toThrow();
  });

  it('keeps the time of the last renewal (rule E-37)', async () => {
    const persist = await Persist.open(new IDBFactory());
    await persist.saveSession({ ...(await session()), renewedAt: 1_700_000_000_000 });
    expect((await persist.loadSession())!.renewedAt).toBe(1_700_000_000_000);
  });

  it('keeps pinned heads, the platform head and cursors; sign-out deletes session and cursors only', async () => {
    const factory = new IDBFactory();
    const persist = await Persist.open(factory);
    expect(await persist.loadPinned('o1')).toBeNull();
    expect(await persist.loadPlatformHead()).toBeNull();
    expect(await persist.loadCursors('o1')).toEqual({});
    await persist.saveSession(await session());
    await persist.savePinned('o1', { founder: 'F', head: { seq: 3, hash: 'h3' } });
    await persist.savePinned('o2', { founder: 'G', head: null });
    await persist.savePlatformHead({ seq: 1, hash: 'p1' });
    await persist.saveCursors('o1', { s1: 4 });
    expect(await persist.loadPinned('o1')).toEqual({ founder: 'F', head: { seq: 3, hash: 'h3' } });
    expect(await persist.loadPinned('o2')).toEqual({ founder: 'G', head: null });
    expect(await persist.loadPlatformHead()).toEqual({ seq: 1, hash: 'p1' });
    expect(await persist.loadCursors('o1')).toEqual({ s1: 4 });
    await persist.clearSession();
    expect(await persist.loadSession()).toBeNull();
    expect(await persist.loadCursors('o1')).toEqual({});
    expect(await persist.loadPinned('o1')).toEqual({ founder: 'F', head: { seq: 3, hash: 'h3' } });
    expect(await persist.loadPlatformHead()).toEqual({ seq: 1, hash: 'p1' });
    persist.close();
    const db = await openDatabase(factory);
    expect([...db.objectStoreNames].sort()).toEqual(['cursors', 'heads', 'platform', 'session']);
    expect(db.name).toBe(DB_NAME);
    db.close();
  });

  it('treats malformed stored values as absent', async () => {
    const factory = new IDBFactory();
    await raw(factory, 'session', 'current', { keyId: 'x' });
    await raw(factory, 'heads', 'o1', { founder: 'F', head: { seq: -1, hash: 'h' } });
    await raw(factory, 'heads', 'o2', 'text');
    await raw(factory, 'platform', 'current', { seq: 1 });
    await raw(factory, 'cursors', 'o1', { s: 'x' });
    await raw(factory, 'cursors', 'o2', [1]);
    const persist = await Persist.open(factory);
    expect(await persist.loadSession()).toBeNull();
    expect(await persist.loadPinned('o1')).toBeNull();
    expect(await persist.loadPinned('o2')).toBeNull();
    expect(await persist.loadPlatformHead()).toBeNull();
    expect(await persist.loadCursors('o1')).toEqual({});
    expect(await persist.loadCursors('o2')).toEqual({});
  });

  it('opens on the global indexedDB by default', async () => {
    const original = globalThis.indexedDB;
    globalThis.indexedDB = new IDBFactory();
    try {
      const persist = await Persist.open();
      expect(await persist.loadSession()).toBeNull();
      persist.close();
    } finally {
      globalThis.indexedDB = original;
    }
  });

  it('rejects failed requests and aborted transactions', async () => {
    const request = {
      onsuccess: null,
      onerror: null,
      result: 1,
      error: new Error('request'),
    } as unknown as IDBRequest<number>;
    const pending = done(request);
    (request.onerror as () => void)();
    await expect(pending).rejects.toThrow('request');
    const transaction = { oncomplete: null, onabort: null, error: new Error('abort') } as unknown as IDBTransaction;
    const waiting = committed(transaction);
    (transaction.onabort as () => void)();
    await expect(waiting).rejects.toThrow('abort');
  });
});
