// The signed-in browser (06-web.md sections 2.3, 2.6, 3.4, 11): one `AppSession` owns the certified session key, the
// signed client, the background-job runner, the event reader and the live channel of the active org.
//
// - `restore()` at start-up: a stored, unexpired session key makes the browser `locked` (signed requests work, but
//   identity and scope keys live only in memory, so a reload needs the passkey again, section 2.3).
// - `begin(signIn)` stores a new session key (non-extractable, IndexedDB) – first sign-in and renewal alike.
// - `unlock(identity)` checks the identity against the registered public keys (`GET /v1/me`), loads the profile,
//   verifies the logs and opens the wraps of every org (jobs `login`), then `unlocked`.
// - `signOut()` ends the session at the hub (`POST /v1/session/end`), stops jobs and channel, deletes the IndexedDB
//   session entries and clears memory (`store.reset()`).
import { b64uDecode } from '../encoding/b64u';
import type { PlatformConfig } from '../config/check';
import { fail } from '../crypto/errors';
import type { Identity } from '../crypto/keys';
import { isLocale, type Locale } from '../i18n/locale';
import { backgroundJobs, JobRunner, type JobRegistration } from '../jobs';
import { INTERVAL_MS } from '../jobs/catch-up';
import { applyFrame } from '../net/frames';
import { connectLive, type LiveConnection, type LiveWiring } from '../net/live';
import { hostOf, type Session, type SessionNotice, SignedClient } from '../net/signedFetch';
import type { Persist } from '../persist';
import { isRecord } from '../registry';
import { EventReader } from '../store/reader';
import { noteSession, sessionRenewed } from '../store/slices/connection';
import { setIdentity, setProfile, setSession } from '../store/slices/me';
import { addKeys, ensureOrg, setLog } from '../store/slices/orgs';
import { setActiveOrg, setLocale, setPhase, setPrfMissing } from '../store/slices/session';
import type { Store } from '../store/store';
import type { TasksContext } from '../tasks/pull';
import type { TerminalContext } from '../terminal/watch';
import { anonClient, type AnonRequest } from './anon';
import type { PreparedOrg } from './genesis';
import { passkeyBundle, sameIdentity, type SignIn, slotOf } from './login';
import type { Prefs } from './prefs';

export type SessionPersist = Pick<
  Persist,
  | 'saveSession'
  | 'loadSession'
  | 'clearSession'
  | 'loadPinned'
  | 'savePinned'
  | 'loadPlatformHead'
  | 'savePlatformHead'
  | 'loadCursors'
  | 'saveCursors'
>;

export interface AppSessionDeps {
  readonly store: Store;
  readonly fetch: typeof globalThis.fetch;
  readonly config: PlatformConfig;
  /** The platform root key compiled into the build (b64u). */
  readonly rootKey: string;
  readonly persist: SessionPersist;
  readonly prefs: Prefs;
  readonly now?: () => number;
  /** Registered background jobs (default: the discovered ones). */
  readonly jobs?: readonly JobRegistration[];
  /** Starts the live channel (default `connectLive`). */
  readonly live?: (wiring: LiveWiring) => Promise<LiveConnection>;
  /** Timer of the `interval` trigger (default `setInterval`/`clearInterval`). */
  readonly timer?: {
    readonly start: (tick: () => void, ms: number) => unknown;
    readonly stop: (handle: unknown) => void;
  };
}

/** What `GET /v1/me` lists per org (03-api.md section 4.1). */
export interface MeOrg {
  readonly org_id: string;
  readonly role: string;
  readonly private: boolean;
  readonly default_wg: string;
}

const isMeOrg = (value: unknown): value is MeOrg =>
  isRecord(value) &&
  typeof value.org_id === 'string' &&
  typeof value.default_wg === 'string' &&
  typeof value.private === 'boolean';

/** The orgs of a `GET /v1/me` body (malformed items are left out). */
export function meOrgs(profile: Readonly<Record<string, unknown>> | null): MeOrg[] {
  const orgs = profile?.orgs;
  return Array.isArray(orgs) ? orgs.filter(isMeOrg) : [];
}

/** The locale of `user_settings` in a `GET /v1/me` body, if valid. */
export function settingsLocale(profile: Readonly<Record<string, unknown>> | null): Locale | null {
  const settings = profile?.settings;
  return isRecord(settings) && isLocale(settings.locale) ? settings.locale : null;
}

export class AppSession {
  readonly store: Store;
  readonly anon: AnonRequest;
  readonly client: SignedClient;
  readonly #deps: AppSessionDeps;
  readonly #now: () => number;
  #session: Session | null = null;
  /** b64u ID of the passkey that certified the current session. */
  #credentialId: string | null = null;
  #runner: JobRunner | null = null;
  #reader: EventReader | null = null;
  #live: { readonly org: string; readonly connection: Promise<LiveConnection | null> } | null = null;
  /** The handle of the `interval` timer while unlocked. */
  #interval: unknown = null;
  /** `setInterval`/`clearInterval` are called as functions (a browser refuses them as methods of another object). */
  readonly #timer: NonNullable<AppSessionDeps['timer']>;
  /** Watched terminal sessions in watch order: session → org and number of views (06-web.md section 5.5). */
  readonly #watched = new Map<string, { readonly org: string; count: number }>();
  /** Open monitoring views per org (06-web.md section 9.1: `metrics` frames only while one is open). */
  readonly #monitoring = new Map<string, number>();

  constructor(deps: AppSessionDeps) {
    this.#deps = deps;
    this.store = deps.store;
    this.#now = deps.now ?? Date.now;
    this.#timer = deps.timer ?? {
      start: (tick, ms) => setInterval(tick, ms),
      stop: (handle) => clearInterval(handle as ReturnType<typeof setInterval>),
    };
    this.anon = anonClient(deps.fetch);
    this.client = new SignedClient({
      fetch: deps.fetch,
      host: hostOf(deps.config.origin),
      hubKey: { id: deps.config.hub_key.id, publicKey: b64uDecode(deps.config.hub_key.public_key) },
      session: () => this.#session,
      now: this.#now,
      onSession: (notice: SessionNotice) => noteSession(this.store, notice),
    });
  }

  get config(): PlatformConfig {
    return this.#deps.config;
  }

  /** The platform root key compiled into the build (b64u; equal to the hub's, checked at start-up). */
  get rootKey(): string {
    return this.#deps.rootKey;
  }

  get prefs(): Prefs {
    return this.#deps.prefs;
  }

  /** `fetch` of the page (requests that need response headers, e.g. `Retry-After` of the recovery). */
  get fetch(): typeof globalThis.fetch {
    return this.#deps.fetch;
  }

  /** b64u ID of the passkey that certified the current session, null without a session. */
  get credentialId(): string | null {
    return this.#credentialId;
  }

  /** The signed-in user, null when anonymous. */
  get userId(): string | null {
    return this.store.getState().me.userId;
  }

  /** Start-up: a stored, unexpired session key makes the browser `locked`; anything else `anonymous`. */
  async restore(): Promise<void> {
    const stored = await this.#deps.persist.loadSession().catch(() => null);
    if (stored === null || stored.expiresAt <= this.#now()) {
      if (stored !== null) await this.#deps.persist.clearSession().catch(() => undefined);
      setPhase(this.store, 'anonymous');
      return;
    }
    this.#session = { keyId: stored.keyId, privateKey: stored.privateKey, expiresAt: stored.expiresAt };
    this.#credentialId = stored.credentialId;
    setSession(this.store, stored.userId, { keyId: stored.keyId, expiresAt: stored.expiresAt });
    this.#userPrefs(stored.userId);
    setPhase(this.store, 'locked');
  }

  /**
   * A certified session key from a sign-in: stored (non-extractable), used for every further request. A sign-in of
   * another user than the one in memory first signs the old one out locally (nothing of it stays in memory).
   */
  async begin(signIn: SignIn): Promise<void> {
    const current = this.userId;
    if (current !== null && current !== signIn.userId) await this.#clear();
    const session = { keyId: signIn.keyId, privateKey: signIn.privateKey, expiresAt: signIn.expiresAt };
    await this.#deps.persist.saveSession({
      ...session,
      publicKey: signIn.publicKey,
      userId: signIn.userId,
      credentialId: signIn.credentialId,
    });
    this.#session = session;
    this.#credentialId = signIn.credentialId;
    setSession(this.store, signIn.userId, { keyId: signIn.keyId, expiresAt: signIn.expiresAt });
    sessionRenewed(this.store);
    this.#userPrefs(signIn.userId);
    if (this.store.getState().me.identity === null) setPhase(this.store, 'locked');
  }

  #userPrefs(userId: string): void {
    this.prefs.forgetRecoveryReminder(userId);
  }

  /** Records whether the last sign-in could unlock with PRF (the `prf_missing` event, 06-web.md section 3.3). */
  notePrf(signIn: SignIn, unlocked: boolean): void {
    setPrfMissing(this.store, !unlocked && signIn.prf === null);
  }

  /** `GET /v1/me` into the store (profile, locale, orgs); returns the body. */
  async loadMe(): Promise<Readonly<Record<string, unknown>>> {
    const me = await this.client.request('GET', '/v1/me');
    if (!isRecord(me) || me.user_id !== this.userId) fail('header_invalid');
    setProfile(this.store, me);
    setLocale(this.store, settingsLocale(me));
    return me;
  }

  /**
   * Unlocks with `identity`: it must carry the public keys the hub registered for this user (`decrypt_failed`
   * otherwise), then logs and wraps of every org are verified (awaited, 06-web.md section 3.4) and the phase is
   * `unlocked`.
   */
  async unlock(identity: Identity): Promise<void> {
    const me = await this.loadMe();
    if (!sameIdentity(identity, me.identity)) fail('decrypt_failed');
    setIdentity(this.store, identity);
    const runner = this.#jobs();
    const orgs = meOrgs(me);
    for (const org of orgs) ensureOrg(this.store, org.org_id);
    await Promise.all(orgs.map((org) => runner.trigger('login', org.org_id)));
    setPhase(this.store, 'unlocked');
    this.#startInterval();
  }

  /**
   * The `interval` trigger (06-web.md section 11: catch-up wrapping and pending rotations every 10 minutes) for every
   * org of the profile, while unlocked.
   */
  #startInterval(): void {
    if (this.#interval !== null) return;
    this.#interval = this.#timer.start(() => {
      for (const org of meOrgs(this.store.getState().me.profile)) void this.#jobs().trigger('interval', org.org_id);
    }, INTERVAL_MS);
  }

  #jobs(): JobRunner {
    if (this.#runner === null) {
      const runner = new JobRunner(
        {
          store: this.store,
          client: this.client,
          persist: this.#deps.persist,
          rootKey: this.#deps.rootKey,
          now: this.#now,
          changed: (org) => this.#reader?.retry(org),
        },
        this.#deps.jobs ?? backgroundJobs(),
      );
      this.#runner = runner;
      this.#reader = new EventReader(this.store, runner);
    }
    return this.#runner;
  }

  /** Uploads the passkey slot of `identity` for the PRF output of `signIn` (`PUT /v1/me/bundles/{slot}`). */
  async uploadPasskeySlot(signIn: SignIn, identity: Identity): Promise<boolean> {
    const bundle = await passkeyBundle(signIn, identity);
    if (bundle === null) return false;
    await this.client.request('PUT', `/v1/me/bundles/${slotOf(signIn)}`, { body: { bundle_ct: bundle } });
    return true;
  }

  /** The recovery slot for an unlock without PRF (`GET /v1/me/bundles/recovery`, no rotation). */
  async recoveryBundle(): Promise<string> {
    const body = await this.client.request('GET', '/v1/me/bundles/recovery');
    if (!isRecord(body) || typeof body.bundle_ct !== 'string') fail('header_invalid');
    return body.bundle_ct;
  }

  /**
   * After `POST /v1/orgs` accepted a prepared org, or `POST /v1/orgs/{org}/log` an `accept` (WEB-08): its log, pinned
   * founder (the verified log's, L3) and head (L9), its keys in memory, the profile reloaded, and it becomes the
   * user's last org.
   */
  async installOrg(prepared: Pick<PreparedOrg, 'org' | 'log' | 'keys'>): Promise<void> {
    if (this.store.getState().me.identity === null) fail('decrypt_failed');
    ensureOrg(this.store, prepared.org);
    setLog(this.store, prepared.org, prepared.log);
    addKeys(this.store, prepared.org, prepared.keys);
    await this.#deps.persist.savePinned(prepared.org, { founder: prepared.log.state.founder, head: prepared.log.head });
    this.rememberOrg(prepared.org);
    await this.loadMe();
  }

  /** The org a signed-in user lands in: the last one used on this browser if still a member, else the first. */
  landingOrg(): string | null {
    const orgs = meOrgs(this.store.getState().me.profile);
    const userId = this.userId;
    const last = userId === null ? null : this.prefs.lastOrg(userId);
    return orgs.find((org) => org.org_id === last)?.org_id ?? orgs[0]?.org_id ?? null;
  }

  rememberOrg(org: string): void {
    const userId = this.userId;
    if (userId !== null) this.prefs.setLastOrg(userId, org);
  }

  /** Makes `org` the active org: its live channel runs (one org at a time, 03-api.md section 5). */
  activate(org: string): void {
    setActiveOrg(this.store, org);
    if (this.#live?.org === org || this.store.getState().session.phase !== 'unlocked') return;
    this.#stopLive();
    const start = this.#deps.live ?? connectLive;
    const connection = start({
      store: this.store,
      client: this.client,
      runner: this.#jobs(),
      reader: this.#reader!,
      persist: this.#deps.persist,
      org,
      terminals: this.#terminals(org),
      monitoring: this.#monitoring.has(org),
      onFrame: (type, data) => {
        if (type === 'terminals_refused') this.#refused(data);
        applyFrame(this.store, type, data, this.#now());
      },
    }).catch(() => null);
    this.#live = { org, connection };
    this.rememberOrg(org);
  }

  /** The watched sessions of `org`, oldest first. */
  #terminals(org: string): string[] {
    return [...this.#watched].filter(([, item]) => item.org === org).map(([session]) => session);
  }

  #syncTerminals(): void {
    const live = this.#live;
    if (live === null) return;
    const sessions = this.#terminals(live.org);
    void live.connection.then((connection) => connection?.channel.setTerminals(sessions));
  }

  /** The hub refused to watch these sessions: they leave the list (the channel already dropped them). */
  #refused(data: unknown): void {
    const sessions = isRecord(data) && Array.isArray(data.sessions) ? data.sessions : [];
    for (const session of sessions) if (typeof session === 'string') this.#watched.delete(session);
  }

  /**
   * Watches the terminal of `session` (live `terminal` frames of the active org's channel, at most three; the newest
   * win). The returned function ends this view's watch.
   */
  watchTerminal(org: string, session: string): () => void {
    const item = this.#watched.get(session) ?? { org, count: 0 };
    item.count += 1;
    this.#watched.delete(session);
    this.#watched.set(session, item);
    this.#syncTerminals();
    let released = false;
    return () => {
      if (released) return;
      released = true;
      item.count -= 1;
      if (item.count > 0 || this.#watched.get(session) !== item) return;
      this.#watched.delete(session);
      this.#syncTerminals();
    };
  }

  /**
   * Opens a monitoring view of `org`: the live channel of the active org requests `metrics` frames while at least one
   * view of it is open. The returned function closes this view.
   */
  watchMonitoring(org: string): () => void {
    this.#monitoring.set(org, (this.#monitoring.get(org) ?? 0) + 1);
    this.#syncMonitoring();
    let released = false;
    return () => {
      if (released) return;
      released = true;
      const left = this.#monitoring.get(org)! - 1;
      if (left > 0) this.#monitoring.set(org, left);
      else this.#monitoring.delete(org);
      this.#syncMonitoring();
    };
  }

  #syncMonitoring(): void {
    const live = this.#live;
    if (live === null) return;
    const on = this.#monitoring.has(live.org);
    void live.connection.then((connection) => connection?.channel.setMonitoring(on));
  }

  /** What the terminal view needs (`core/terminal/watch.ts`): store, client, reader without storage, the watch. */
  get terminal(): TerminalContext {
    return {
      store: this.store,
      client: this.client,
      now: this.#now,
      read: async (org, scopeId, items) => {
        this.#jobs();
        const reader = this.#reader!;
        const entries = [];
        for (const item of items) {
          entries.push(
            await reader.read({
              org,
              scopeId,
              scopeSeq: item.scope_seq,
              receivedAt: item.received_at,
              event: item.event,
              status: 'pending',
              code: null,
              header: null,
              body: null,
            }),
          );
        }
        return entries;
      },
      watch: (org, session) => this.watchTerminal(org, session),
    };
  }

  #stopLive(): void {
    const live = this.#live;
    this.#live = null;
    void live?.connection.then((connection) => connection?.stop());
  }

  /** What the task flows need (`core/tasks`): store, client, the log job and the event reader of this session. */
  get tasks(): TasksContext {
    return {
      store: this.store,
      client: this.client,
      now: this.#now,
      refreshLog: (org) => this.#jobs().trigger('log', org),
      ingest: (org, scopeId, items) => {
        this.#jobs();
        return this.#reader!.ingest(org, scopeId, items);
      },
    };
  }

  /** `PUT /v1/me/settings` with a new locale (model settings unchanged). */
  async saveLocale(locale: Locale): Promise<void> {
    const profile = this.store.getState().me.profile;
    const settings = isRecord(profile?.settings) ? profile.settings : {};
    const body = {
      model_access: typeof settings.model_access === 'string' ? settings.model_access : 'auto',
      model_id: typeof settings.model_id === 'string' ? settings.model_id : null,
      locale,
    };
    const saved = await this.client.request('PUT', '/v1/me/settings', { body });
    setProfile(this.store, { ...profile, settings: isRecord(saved) ? saved : body });
    setLocale(this.store, locale);
  }

  async #clear(): Promise<void> {
    if (this.#interval !== null) this.#timer.stop(this.#interval);
    this.#interval = null;
    this.#stopLive();
    this.#watched.clear();
    this.#runner?.stop();
    this.#runner = null;
    this.#reader = null;
    this.#session = null;
    this.#credentialId = null;
    await this.#deps.persist.clearSession().catch(() => undefined);
    this.store.reset();
  }

  /** Sign-out (06-web.md section 2.6): the hub ends the session (best effort), then everything local is cleared. */
  async signOut(): Promise<void> {
    if (this.#session !== null) await this.client.request('POST', '/v1/session/end', { body: {} }).catch(() => null);
    await this.#clear();
    setPhase(this.store, 'anonymous');
  }
}
