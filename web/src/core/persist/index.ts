// IndexedDB persistence of the web client (06-web.md section 2.2): exactly the session key (a non-extractable
// `CryptoKey`, stored by structured clone, its private bytes never leave WebCrypto), the session certificate metadata
// (with the time of the last renewal, rule E-40; the identity of a browser that stays signed in is in `device-key.ts`),
// the pinned founder key and verified head per org (L3, L9), the verified platform-log head and the last cursors of
// the live channel. Nothing else – no scope keys, no identity keys, no event content. Values read back are checked
// and anything malformed is treated as absent.
import type { Head } from '../log/entries';
import type { Pinned } from '../log/verify';
import type { CursorMap } from '../net/cursors';

export const DB_NAME = 'agent-platform';
export const DB_VERSION = 1;
const SESSION = 'session';
const HEADS = 'heads';
const PLATFORM = 'platform';
const CURSORS = 'cursors';
const STORES = [SESSION, HEADS, PLATFORM, CURSORS] as const;
const CURRENT = 'current';

/** The certified session of this browser. */
export interface StoredSession {
  readonly keyId: string;
  readonly privateKey: CryptoKey;
  /** The 65-byte uncompressed P-256 point. */
  readonly publicKey: Uint8Array;
  readonly userId: string;
  readonly credentialId: string;
  /** `expires_at` (Unix ms). */
  readonly expiresAt: number;
  /** When a tab of this browser last renewed the session (Unix ms, rule E-40); absent before the first renewal. */
  readonly renewedAt?: number;
}

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);
const isText = (value: unknown): value is string => typeof value === 'string' && value !== '';
const isSeq = (value: unknown): value is number => Number.isSafeInteger(value) && (value as number) >= 0;

export function isHead(value: unknown): value is Head {
  return isRecord(value) && isSeq(value.seq) && isText(value.hash);
}

function isSession(value: unknown): value is StoredSession {
  return (
    isRecord(value) &&
    isText(value.keyId) &&
    value.privateKey instanceof CryptoKey &&
    !value.privateKey.extractable &&
    value.privateKey.type === 'private' &&
    value.privateKey.algorithm.name === 'ECDSA' &&
    (value.privateKey.algorithm as EcKeyAlgorithm).namedCurve === 'P-256' &&
    value.privateKey.usages.length === 1 &&
    value.privateKey.usages[0] === 'sign' &&
    value.publicKey instanceof Uint8Array &&
    value.publicKey.length === 65 &&
    isText(value.userId) &&
    isText(value.credentialId) &&
    isSeq(value.expiresAt) &&
    (value.renewedAt === undefined || isSeq(value.renewedAt))
  );
}

function isPinned(value: unknown): value is Pinned {
  return isRecord(value) && isText(value.founder) && (value.head === null || isHead(value.head));
}

function isCursors(value: unknown): value is CursorMap {
  return isRecord(value) && Object.values(value).every(isSeq);
}

/** The result of a request. */
export function done<T>(request: Pick<IDBRequest<T>, 'onsuccess' | 'onerror' | 'result' | 'error'>): Promise<T> {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

/** Resolves when the transaction committed; rejects when it aborted (an error aborts it). */
export function committed(transaction: Pick<IDBTransaction, 'oncomplete' | 'onabort' | 'error'>): Promise<void> {
  return new Promise((resolve, reject) => {
    transaction.oncomplete = () => resolve();
    transaction.onabort = () => reject(transaction.error);
  });
}

/** Opens (and creates) the database. */
export function openDatabase(factory: IDBFactory = globalThis.indexedDB): Promise<IDBDatabase> {
  const request = factory.open(DB_NAME, DB_VERSION);
  // version 1 creates every store; a later version adds its own upgrade step here
  request.onupgradeneeded = () => {
    for (const name of STORES) request.result.createObjectStore(name);
  };
  return done(request);
}

/** The persistence API on one open database. */
export class Persist {
  readonly #db: IDBDatabase;

  constructor(db: IDBDatabase) {
    this.#db = db;
  }

  static async open(factory?: IDBFactory): Promise<Persist> {
    return new Persist(await openDatabase(factory));
  }

  close(): void {
    this.#db.close();
  }

  async #get(store: string, key: string): Promise<unknown> {
    return done(this.#db.transaction(store, 'readonly').objectStore(store).get(key));
  }

  async #put(store: string, key: string, value: unknown): Promise<void> {
    const transaction = this.#db.transaction(store, 'readwrite');
    transaction.objectStore(store).put(value, key);
    await committed(transaction);
  }

  /** Stores the session; an extractable private key is refused (it must never be persisted). */
  async saveSession(session: StoredSession): Promise<void> {
    if (!isSession(session)) throw new Error('refusing to store an invalid or extractable session key');
    await this.#put(SESSION, CURRENT, { ...session });
  }

  async loadSession(): Promise<StoredSession | null> {
    const value = await this.#get(SESSION, CURRENT);
    return isSession(value) ? value : null;
  }

  /** Sign-out (06-web.md section 2.6): the session entries (session key, certificate metadata, cursors) are deleted. */
  async clearSession(): Promise<void> {
    const transaction = this.#db.transaction([SESSION, CURSORS], 'readwrite');
    transaction.objectStore(SESSION).clear();
    transaction.objectStore(CURSORS).clear();
    await committed(transaction);
  }

  async savePinned(org: string, pinned: Pinned): Promise<void> {
    await this.#put(HEADS, org, {
      founder: pinned.founder,
      head: pinned.head && { seq: pinned.head.seq, hash: pinned.head.hash },
    });
  }

  async loadPinned(org: string): Promise<Pinned | null> {
    const value = await this.#get(HEADS, org);
    return isPinned(value) ? value : null;
  }

  async savePlatformHead(head: Head): Promise<void> {
    await this.#put(PLATFORM, CURRENT, { seq: head.seq, hash: head.hash });
  }

  async loadPlatformHead(): Promise<Head | null> {
    const value = await this.#get(PLATFORM, CURRENT);
    return isHead(value) ? value : null;
  }

  async saveCursors(org: string, cursors: CursorMap): Promise<void> {
    await this.#put(CURSORS, org, { ...cursors });
  }

  async loadCursors(org: string): Promise<CursorMap> {
    const value = await this.#get(CURSORS, org);
    return isCursors(value) ? value : {};
  }
}
