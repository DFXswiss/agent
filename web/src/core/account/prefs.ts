// Non-secret preferences of this browser in `localStorage`: the last org a user worked in (route `/`, 06-web.md
// section 2.4) and the reminder that the recovery code was skipped (section 3.2). Only IDs and a flag – no keys, no
// names, no content (those stay in memory or in the IndexedDB entries of `persist/`). A storage that refuses access
// (private mode, quota) behaves as empty.
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

  recoveryUnconfirmed(userId: string): boolean {
    return this.#get(`recovery-unconfirmed.${userId}`) === '1';
  }

  setRecoveryUnconfirmed(userId: string, unconfirmed: boolean): void {
    this.#set(`recovery-unconfirmed.${userId}`, unconfirmed ? '1' : null);
  }
}
