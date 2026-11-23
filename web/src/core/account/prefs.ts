// Non-secret preferences of this browser in `localStorage`: the last org a user worked in (route `/`, 06-web.md
// section 2.4) and, for the community (12-community.md section 6), the display name the user chose there and the
// identity keys of authors the user has seen (pinned on first sight). Only IDs, public keys and the public community
// name – no secret keys, no other names, no content (those stay in memory or in the IndexedDB entries of `persist/`).
// A storage that refuses access (private mode, quota) behaves as empty.
export interface PrefStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

const PREFIX = 'agent-platform.';

export class Prefs {
  readonly #storage: PrefStorage | null;

  constructor(storage: PrefStorage | null) {
    this.#storage = storage;
  }

  #get(key: string): string | null {
    try {
      return this.#storage?.getItem(PREFIX + key) ?? null;
    } catch {
      return null;
    }
  }

  #set(key: string, value: string | null): void {
    try {
      if (value === null) this.#storage?.removeItem(PREFIX + key);
      else this.#storage?.setItem(PREFIX + key, value);
    } catch {
      // a refusing storage only loses the preference
    }
  }

  lastOrg(userId: string): string | null {
    return this.#get(`last-org.${userId}`);
  }

  setLastOrg(userId: string, org: string): void {
    this.#set(`last-org.${userId}`, org);
  }

  /** Removes the reminder flag of a skipped recovery code that browsers stored before KOR-22 (no reminder any more). */
  forgetRecoveryReminder(userId: string): void {
    this.#set(`recovery-unconfirmed.${userId}`, null);
  }

  /** The display name the user chose for the community in this browser. */
  communityName(userId: string): string | null {
    return this.#get(`community-name.${userId}`);
  }

  setCommunityName(userId: string, name: string): void {
    this.#set(`community-name.${userId}`, name === '' ? null : name);
  }

  /** The pinned community author keys of a viewer: author ID → b64u Ed25519 key (malformed data is empty). */
  communityKeys(userId: string): Record<string, string> {
    try {
      const value: unknown = JSON.parse(this.#get(`community-keys.${userId}`) ?? '{}');
      if (typeof value !== 'object' || value === null || Array.isArray(value)) return {};
      return Object.fromEntries(
        Object.entries(value).filter((entry): entry is [string, string] => typeof entry[1] === 'string'),
      );
    } catch {
      return {};
    }
  }

  setCommunityKeys(userId: string, keys: Readonly<Record<string, string>>): void {
    this.#set(`community-keys.${userId}`, JSON.stringify(keys));
  }
}
