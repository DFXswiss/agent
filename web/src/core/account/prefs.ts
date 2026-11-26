// Non-secret preferences of this browser in `localStorage`: the last org a user worked in (route `/`, 06-web.md
// section 2.4) and, for the community (14-community.md section 6), the display name the user chose there and the
// identity keys of authors the user has seen (pinned on first sight), and the agents whose pairing this browser
// confirmed with their certificates (ADR-082: the job `agents` adds an agent from the hub's list only then), and the
// credential-key losses this browser already rotated for (a rotation is owed once, not at every sign-in). Only IDs, public keys and the public community
// name – no secret keys, no other names, no content (those stay in memory or in the IndexedDB entries of `persist/`).
// The bandwidth choice of the live browser picture (06-web.md section 5.4b) and the choice "Stay signed in" of the
// sign-in page (rule E-40, on unless switched off) are kept here as well (one value for every user of this browser).
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
    return this.#record(`community-keys.${userId}`);
  }

  setCommunityKeys(userId: string, keys: Readonly<Record<string, string>>): void {
    this.#set(`community-keys.${userId}`, JSON.stringify(keys));
  }

  /** The agents whose pairing this browser confirmed for the user: agent ID → certificate (malformed data is empty). */
  confirmedAgents(userId: string): Record<string, string> {
    return this.#record(`confirmed-agents.${userId}`);
  }

  /** Remembers that this browser confirmed the pairing of `agent` with certificate `cert`. */
  addConfirmedAgent(userId: string, agent: string, cert: string): void {
    this.#set(`confirmed-agents.${userId}`, JSON.stringify({ ...this.confirmedAgents(userId), [agent]: cert }));
  }

  /** The credential-key losses this browser handled for the user (`jobs/credential-rewrap.ts`; malformed: none). */
  handledLosses(userId: string): string[] {
    try {
      const value: unknown = JSON.parse(this.#get(`handled-losses.${userId}`) ?? '[]');
      return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : [];
    } catch {
      return [];
    }
  }

  setHandledLosses(userId: string, keys: readonly string[]): void {
    this.#set(`handled-losses.${userId}`, JSON.stringify(keys));
  }

  /** A stored JSON object of strings (malformed data is empty). */
  #record(key: string): Record<string, string> {
    try {
      const value: unknown = JSON.parse(this.#get(key) ?? '{}');
      if (typeof value !== 'object' || value === null || Array.isArray(value)) return {};
      return Object.fromEntries(
        Object.entries(value).filter((entry): entry is [string, string] => typeof entry[1] === 'string'),
      );
    } catch {
      return {};
    }
  }

  /** "Stay signed in" (rule E-40) for the next sign-in on this browser: on unless the user switched it off. */
  staySignedIn(): boolean {
    return this.#get('stay-signed-in') !== 'off';
  }

  setStaySignedIn(on: boolean): void {
    this.#set('stay-signed-in', on ? null : 'off');
  }

  /** The bandwidth choice of the live browser picture (`core/browser/bandwidth.ts`), the same for every user. */
  browserBandwidth(): string | null {
    return this.#get('browser-bandwidth');
  }

  setBrowserBandwidth(value: string): void {
    this.#set('browser-bandwidth', value);
  }
}
