// The community client of one signed-in browser (14-community.md section 4.2, ADR-069): one `Community` per store and
// sign-in (`community(deps)`; `communityOf(store)` finds the running one), driven by the background job `community`
// (start, every 10 minutes), the live frame `community` and the screen.
//
// - Start: `POST /v1/community/join`, `GET /v1/community`, the newest page of messages; a hub without the community
//   (`404`) makes the area `absent` and nothing further runs.
// - Key: no epoch → draw `CMK_1`, key check, self-wrap, `POST /v1/community/epochs` (`community_epoch_exists` → read
//   again); a self-wrap → check its signature, open it, compare the key check; neither → a one-off X25519 request key
//   (memory only), `PUT /v1/community/request`, wait for the frame `key` (and read again every minute); a wrapped
//   request → check the wrapper's signature, open, compare the key check, store the self-wrap, else reject it. A
//   wrapped request of another browser of the same user gets one minute to be stored before it is replaced.
// - Serve: a holder seals the key to open requests (`GET`/`POST /v1/community/requests…`) at start, on the frame
//   `request` after a random delay of up to 3 s, and every 10 minutes.
// - Read: pages before the oldest loaded message, pulls after the newest on the frame `message` (and after a
//   reconnect); every message is checked under its author's identity key, which is pinned on first sight per viewer
//   after a valid signature (a changed key makes the message unverifiable), and only a verified message shows content.
//   An author that is an own-identity agent (ADR-082) also needs a certificate that verifies under its user's signing
//   key, pinned like a user's key; its messages are marked `agent` ("Agent of <name>"), else they are unverifiable.
// - Post (a `429` notes `retryAt` from `Retry-After`), delete, read position and moderation are the screen's actions.
import type { Prefs } from '../account/prefs';
import { b64uDecode, b64uEncode } from '../encoding/b64u';
import { uuid7 } from '../encoding/ids';
import { formatTime, parseTime } from '../encoding/time';
import { verifyCert } from '../crypto/agent';
import * as C from '../crypto/community';
import { CryptoError } from '../crypto/errors';
import { type Identity, type KeyPair, x25519KeyPair } from '../crypto/keys';
import { Secret } from '../crypto/secret';
import { random } from '../crypto/subtle';
import { HubError, type SignedClient } from '../net/signedFetch';
import { SYSTEM_TIMERS, type Timers } from '../net/sse';
import { isRecord } from '../registry';
import {
  type CommunityMessage,
  type CommunityState,
  onCommunityFrame,
  patchCommunity,
  putMessages,
  removeMessage,
} from '../store/slices/community';
import type { Store } from '../store/store';
import { selectDirectory } from '../tasks/directory';

export const PAGE = 50;
export const PULL = 100;
export const REQUESTS = 20;
/** At most this many request pages are served in one run. */
export const SERVE_ROUNDS = 5;
export const POLL_MS = 60_000;
export const SERVE_DELAY_MS = 3000;
/** `tick` (the job's `interval` trigger, once per org) runs at most this often. */
export const TICK_MS = 60_000;
/** The wait after a `429` without a usable `Retry-After` (one refill of the hub's bucket, ADR-069 decision 7). */
export const RETRY_FALLBACK_S = 6;

export interface CommunityDeps {
  readonly store: Store;
  readonly client: Pick<SignedClient, 'request'>;
  readonly prefs?: Pick<Prefs, 'communityName' | 'setCommunityName' | 'communityKeys' | 'setCommunityKeys'>;
  readonly now?: () => number;
  readonly timers?: Timers;
  readonly random?: () => number;
}

interface Row {
  readonly seq: number;
  readonly id: string;
  readonly author: string;
  readonly created_at: string;
  readonly deleted: string | null;
  readonly message: unknown;
}

interface Author {
  readonly sig_pub: string;
  readonly moderator: boolean;
  /** An own-identity agent (ADR-082): its user, the user's signing key, its keys and certificate (unchecked here). */
  readonly agent?: unknown;
}

interface Page {
  readonly rows: Row[];
  readonly authors: ReadonlyMap<string, Author>;
  readonly nextBefore: string | null;
}

interface Wrapped {
  readonly epoch: unknown;
  readonly wrap: unknown;
  readonly sig: unknown;
  readonly wrapper_sig: unknown;
}

/** The parts of `GET /v1/community` the client acts on. */
interface Overview {
  readonly epoch: { readonly epoch: number; readonly check: string } | null;
  readonly wrap: { readonly wrap: string; readonly sig: unknown } | null;
  readonly request: { readonly id: string; readonly wrapped: Wrapped | null } | null;
  readonly patch: Partial<CommunityState>;
}

interface Kept {
  readonly name: string;
  readonly text: string;
  readonly message: unknown;
}

const isText = (value: unknown): value is string => typeof value === 'string';
const count = (value: unknown, fallback: number): number =>
  Number.isSafeInteger(value) && (value as number) >= 0 ? (value as number) : fallback;

function invalid(): never {
  throw new HubError('request_invalid', 200);
}

function parseRow(value: unknown): Row {
  if (
    !isRecord(value) ||
    !Number.isSafeInteger(value.seq) ||
    !isText(value.id) ||
    !isText(value.author) ||
    !isText(value.created_at) ||
    !(value.deleted === null || isText(value.deleted))
  ) {
    invalid();
  }
  return value as unknown as Row;
}

function parsePage(body: unknown): Page {
  if (!isRecord(body) || !Array.isArray(body.messages) || !isRecord(body.authors)) invalid();
  const authors = new Map<string, Author>();
  for (const [user, entry] of Object.entries(body.authors)) {
    if (isRecord(entry) && isText(entry.sig_pub)) {
      const agent = entry.agent === undefined ? {} : { agent: entry.agent };
      authors.set(user, { sig_pub: entry.sig_pub, moderator: entry.moderator === true, ...agent });
    }
  }
  const nextBefore = isText(body.next_before) ? body.next_before : null;
  return { rows: body.messages.map(parseRow), authors, nextBefore };
}

function parseOverview(body: unknown): Overview {
  if (!isRecord(body) || !isRecord(body.member) || !isRecord(body.limits)) invalid();
  const { epoch, wrap, request, member, limits } = body;
  if (epoch !== null && !(isRecord(epoch) && Number.isSafeInteger(epoch.epoch) && isText(epoch.key_check))) invalid();
  if (wrap !== null && !(isRecord(wrap) && isText(wrap.wrap))) invalid();
  if (request !== null && !(isRecord(request) && isText(request.id))) invalid();
  return {
    epoch: epoch === null ? null : { epoch: epoch.epoch as number, check: epoch.key_check as string },
    wrap: wrap === null ? null : { wrap: wrap.wrap as string, sig: wrap.sig },
    request:
      request === null
        ? null
        : {
            id: request.id as string,
            wrapped: isRecord(request.wrapped) ? (request.wrapped as unknown as Wrapped) : null,
          },
    patch: {
      muted: member.muted === true,
      posted: member.posted === true,
      moderator: body.moderator === true,
      lastRead: count(member.last_read_seq, 0),
      baseRead: count(member.last_read_seq, 0),
      unreadBase: count(body.unread, 0),
      baseHead: count(body.head_seq, 0),
      textChars: count(limits.text_chars, C.MAX_TEXT_CHARS),
      nameChars: count(limits.name_chars, C.MAX_NAME_CHARS),
    },
  };
}

function timeOf(text: string): number {
  try {
    return parseTime(text);
  } catch {
    return 0;
  }
}

/** The `HubError`/`CryptoError` code of a failure, `unavailable` for anything else. */
export function codeOf(error: unknown): string {
  return error instanceof HubError || error instanceof CryptoError ? error.code : 'unavailable';
}

/** A hub without the community answers its routes with `404 not_found`. */
const isAbsent = (error: unknown): boolean =>
  error instanceof HubError && error.status === 404 && error.code === 'not_found';

const engines = new WeakMap<Store, Community>();

/** The community client of `deps.store` for the current sign-in (created on first use, renewed after a sign-out). */
export function community(deps: CommunityDeps): Community {
  return communityOf(deps.store) ?? create(deps);
}

function create(deps: CommunityDeps): Community {
  const created = new Community(deps);
  engines.set(deps.store, created);
  return created;
}

/** The running community client of `store` for the current sign-in, null before the job created one. */
export function communityOf(store: Store): Community | null {
  const known = engines.get(store);
  return known !== undefined && known.generation === store.generation ? known : null;
}

export class Community {
  readonly generation: number;
  readonly #deps: CommunityDeps;
  readonly #store: Store;
  readonly #now: () => number;
  readonly #timers: Timers;
  readonly #random: () => number;
  #started: Promise<void> | null = null;
  #lastTick = -Infinity;
  #key: { readonly epoch: number; readonly secret: Secret } | null = null;
  #request: { readonly id: string; readonly pair: KeyPair } | null = null;
  /** A wrapped request of another browser of this user that was given time to store its self-wrap. */
  #foreign: string | null = null;
  #pins: Record<string, string> | null = null;
  /** Verified rows not opened yet (no key) by `seq`, with their author's key. */
  readonly #pending = new Map<number, { readonly row: Row; readonly author: Author }>();
  #poll: unknown = null;
  #pull: Promise<void> = Promise.resolve();
  #refreshing: Promise<void> = Promise.resolve();
  #serving: Promise<void> | null = null;
  #kept: Kept | null = null;

  constructor(deps: CommunityDeps) {
    this.#deps = deps;
    this.#store = deps.store;
    this.generation = deps.store.generation;
    this.#now = deps.now ?? Date.now;
    this.#timers = deps.timers ?? SYSTEM_TIMERS;
    this.#random = deps.random ?? Math.random;
    onCommunityFrame(this.#store, (frame) => this.#frame(frame.change));
  }

  /** No sign-out since this client was created. */
  get #current(): boolean {
    return this.#store.generation === this.generation;
  }

  get #alive(): boolean {
    return this.#current && this.#state.phase !== 'absent';
  }

  get #state(): CommunityState {
    return this.#store.getState().community;
  }

  get #me(): string {
    return this.#store.getState().me.userId!;
  }

  #patch(patch: Partial<CommunityState>): void {
    if (this.#current) patchCommunity(this.#store, patch);
  }

  #put(messages: readonly CommunityMessage[], patch?: Partial<CommunityState>): void {
    if (this.#current) putMessages(this.#store, messages, patch);
  }

  #call(method: string, target: string, body?: unknown): Promise<unknown> {
    return this.#deps.client.request(method, target, body === undefined ? {} : { body });
  }

  /** Join, state, newest page and key flow, once per sign-in (again after a failed start, never after `absent`). */
  start(): Promise<void> {
    this.#started ??= this.#start().catch((error: unknown) => {
      if (!this.#current) return;
      if (isAbsent(error)) {
        this.#stopPoll();
        this.#patch({ phase: 'absent', code: null });
        return;
      }
      this.#started = null;
      this.#patch({ phase: 'error', code: codeOf(error) });
    });
    return this.#started;
  }

  async #start(): Promise<void> {
    this.#patch({ phase: 'loading', code: null, me: this.#me });
    await this.#call('POST', '/v1/community/join', {});
    await this.refresh();
    await this.#loadLatest();
    await this.serve();
  }

  /** The job's `interval` trigger: start if needed, else read the state again and serve (at most once a minute). */
  async tick(): Promise<void> {
    const now = this.#now();
    if (now - this.#lastTick < TICK_MS) return;
    this.#lastTick = now;
    if (this.#started === null) return this.start();
    await this.#started;
    if (!this.#alive || this.#state.phase !== 'ready') return;
    await this.refresh().catch(() => undefined);
    await this.serve();
  }

  /**
   * `GET /v1/community` into the slice, then the key flow (one at a time: two flows would replace each other's
   * request).
   */
  refresh(): Promise<void> {
    this.#refreshing = this.#refreshing.catch(() => undefined).then(() => this.#refresh());
    return this.#refreshing;
  }

  async #refresh(): Promise<void> {
    const overview = parseOverview(await this.#call('GET', '/v1/community'));
    if (!this.#current) return;
    this.#patch({ ...overview.patch, phase: 'ready', code: null, me: this.#me });
    const identity = this.#store.getState().me.identity;
    if (identity !== null) await this.#ensureKey(identity, overview);
  }

  async #ensureKey(identity: Identity, overview: Overview): Promise<void> {
    if (overview.epoch === null) return this.#bootstrap(identity);
    const { epoch, check } = overview.epoch;
    if (this.#key?.epoch === epoch) return;
    const own = overview.wrap;
    if (own !== null) {
      try {
        await C.verifyCommunityWrap(identity.sigPublic(), epoch, this.#me, own.wrap, own.sig);
        await this.#accept(epoch, check, await C.unwrapCommunityKey(identity.kem, epoch, this.#me, own.wrap));
      } catch (error) {
        // a broken self-wrap cannot be repaired by this client (the hub refuses requests of holders)
        this.#patch({ key: 'failed', keyCode: codeOf(error), waitingSince: null });
      }
      return;
    }
    const request = overview.request;
    if (request !== null && request.id === this.#request?.id) {
      if (request.wrapped !== null) return this.#openWrapped(identity, epoch, check, request.wrapped);
      return this.#wait();
    }
    if (request !== null && request.wrapped !== null && request.id !== this.#foreign) {
      this.#foreign = request.id;
      return this.#wait();
    }
    await this.#newRequest(identity);
  }

  async #accept(epoch: number, check: string, secret: Secret): Promise<void> {
    if (!(await C.matchesCheck(secret, epoch, check))) throw new CryptoError('decrypt_failed');
    this.#key = { epoch, secret };
    this.#request = null;
    this.#stopPoll();
    this.#patch({ key: 'ready', keyCode: null, waitingSince: null });
    await this.#openPending();
  }

  async #bootstrap(identity: Identity): Promise<void> {
    const secret = Secret.random(32);
    const wrap = await C.wrapCommunityKey(identity.kemPublic(), 1, this.#me, secret);
    const check = b64uEncode(await C.keyCheck(secret, 1));
    const sig = await C.signCommunityWrap(identity.sig.privateKey, 1, this.#me, wrap);
    try {
      await this.#call('POST', '/v1/community/epochs', { epoch: 1, key_check: check, wrap, sig });
    } catch (error) {
      if (codeOf(error) !== 'community_epoch_exists') throw error;
      return this.#refresh();
    }
    await this.#accept(1, check, secret);
  }

  async #openWrapped(identity: Identity, epoch: number, check: string, wrapped: Wrapped): Promise<void> {
    const request = this.#request!;
    let secret: Secret;
    try {
      if (wrapped.epoch !== epoch) throw new CryptoError('epoch_stale');
      const wrap = wrapped.wrap as string;
      await C.verifyCommunityWrap(b64uDecode(wrapped.wrapper_sig, 32), epoch, request.id, wrap, wrapped.sig);
      secret = await C.unwrapCommunityKey(request.pair, epoch, request.id, wrap);
      if (!(await C.matchesCheck(secret, epoch, check))) throw new CryptoError('decrypt_failed');
    } catch {
      // a wrong key or a forged wrap: the hub records the wrapper, the request stays open for another holder
      await this.#reject();
      return this.#wait();
    }
    const self = await C.wrapCommunityKey(identity.kemPublic(), epoch, this.#me, secret);
    const sig = await C.signCommunityWrap(identity.sig.privateKey, epoch, this.#me, self);
    await this.#call('POST', '/v1/community/wraps', { epoch, wrap: self, sig });
    await this.#accept(epoch, check, secret);
    void this.serve();
  }

  async #reject(): Promise<void> {
    try {
      await this.#call('POST', '/v1/community/request/reject', {});
    } catch (error) {
      // `not_found`: the wrap is gone already (replaced or rejected by another browser of this user)
      if (codeOf(error) !== 'not_found') throw error;
    }
  }

  async #newRequest(identity: Identity): Promise<void> {
    const pair = await x25519KeyPair(random(32));
    const id = uuid7(this.#now());
    const kemPub = b64uEncode(pair.publicKey);
    const sig = await C.signRequest(identity.sig.privateKey, { id, kem_pub: kemPub, user: this.#me });
    await this.#call('PUT', '/v1/community/request', { id, kem_pub: kemPub, sig });
    this.#request = { id, pair };
    this.#wait();
  }

  #wait(): void {
    if (this.#state.key !== 'waiting') this.#patch({ key: 'waiting', keyCode: null, waitingSince: this.#now() });
    if (this.#poll !== null) return;
    this.#poll = this.#timers.setTimeout(() => {
      this.#poll = null;
      if (this.#alive) void this.refresh().catch(() => this.#wait());
    }, POLL_MS);
  }

  #stopPoll(): void {
    if (this.#poll !== null) this.#timers.clearTimeout(this.#poll);
    this.#poll = null;
  }

  /** Seals the key to open requests of others (holders only; one run at a time); a failure ends the run quietly. */
  serve(): Promise<void> {
    this.#serving ??= this.#serve().finally(() => {
      this.#serving = null;
    });
    return this.#serving;
  }

  async #serve(): Promise<void> {
    const key = this.#key;
    const identity = this.#store.getState().me.identity;
    if (key === null || identity === null || !this.#alive) return;
    try {
      for (let round = 0; round < SERVE_ROUNDS; round += 1) {
        const body = await this.#call('GET', `/v1/community/requests?limit=${REQUESTS}`);
        const requests: unknown[] = isRecord(body) && Array.isArray(body.requests) ? body.requests : [];
        const wraps = [];
        for (const request of requests) {
          try {
            if (!isRecord(request)) throw new CryptoError('header_invalid');
            const id = request.id as string;
            const wrap = await C.wrapCommunityKey(b64uDecode(request.kem_pub, 32), key.epoch, id, key.secret);
            const sig = await C.signCommunityWrap(identity.sig.privateKey, key.epoch, id, wrap);
            wraps.push({ request: id, epoch: key.epoch, wrap, sig });
          } catch {
            // an entry that cannot be sealed is skipped (14-community.md section 4.2)
          }
        }
        if (wraps.length > 0) await this.#call('POST', '/v1/community/requests/wraps', { wraps });
        if (requests.length < REQUESTS) return;
      }
    } catch {
      // serving is best effort: the next trigger tries again
    }
  }

  #frame(change: string): void {
    if (!this.#alive || this.#started === null) return;
    if (change === 'message') void this.pullNew().catch(() => undefined);
    else if (change === 'key' || change === 'mute') void this.refresh().catch(() => undefined);
    else if (change === 'request' && this.#key !== null) {
      this.#timers.setTimeout(() => void this.serve(), Math.floor(this.#random() * SERVE_DELAY_MS));
    } else if (change === 'hello') {
      void this.refresh()
        .then(() => this.pullNew())
        .catch(() => undefined);
    }
  }

  /** Whether `key` is the pinned key of `author` for this viewer (the first key seen is pinned). */
  #pinned(author: string, key: string): boolean {
    const prefs = this.#deps.prefs;
    this.#pins ??= prefs?.communityKeys(this.#me) ?? {};
    const known = this.#pins[author];
    if (known !== undefined) return known === key;
    this.#pins[author] = key;
    prefs?.setCommunityKeys(this.#me, this.#pins);
    return true;
  }

  /**
   * An agent author (ADR-082): its certificate must verify under its user's signing key, which is pinned like the key
   * of a user author (first sight, then it must not change); true when it does, null otherwise.
   */
  async #agentOk(agentId: string, author: Author): Promise<true | null> {
    const agent = author.agent;
    if (!isRecord(agent) || !isText(agent.user) || !isText(agent.user_sig_pub) || !isText(agent.kem_pub)) return null;
    try {
      const pub = { kem: agent.kem_pub, sig: author.sig_pub };
      await verifyCert(b64uDecode(agent.user_sig_pub, 32), agent.cert, {
        agent: agentId,
        pub,
        user: agent.user,
        gen: agent.gen,
      });
    } catch {
      return null;
    }
    return this.#pinned(agent.user, agent.user_sig_pub) ? true : null;
  }

  async #read(row: Row, author: Author | undefined): Promise<CommunityMessage> {
    const base = {
      seq: row.seq,
      id: row.id,
      author: row.author,
      at: timeOf(row.created_at),
      moderator: author?.moderator === true,
      name: null,
      text: null,
    };
    if (row.deleted !== null || row.message === null) return { ...base, removed: true, status: 'verified' };
    const failed = { ...base, removed: false, status: 'unverifiable' } as const;
    if (author === undefined) return failed;
    let header: Record<string, unknown>;
    let sigPublic: Uint8Array;
    try {
      sigPublic = b64uDecode(author.sig_pub, 32);
      header = await C.verifyMessage(row.message, sigPublic);
    } catch {
      return failed;
    }
    if (header.id !== row.id || header.author !== row.author || !this.#pinned(row.author, author.sig_pub)) {
      return failed;
    }
    const agent = author.agent === undefined ? false : await this.#agentOk(row.author, author);
    if (agent === null) return failed;
    const key = this.#key;
    if (key === null) {
      this.#pending.set(row.seq, { row, author });
      return { ...base, removed: false, status: 'pending' };
    }
    if (header.epoch !== key.epoch) return failed;
    try {
      const body = await C.openMessage(key.secret, row.message, sigPublic);
      return { ...base, removed: false, status: 'verified', name: body.name, text: body.text, agent };
    } catch {
      return failed;
    }
  }

  async #ingest(page: Page): Promise<CommunityMessage[]> {
    const out: CommunityMessage[] = [];
    for (const row of page.rows) out.push(await this.#read(row, page.authors.get(row.author)));
    return out;
  }

  async #openPending(): Promise<void> {
    const pending = [...this.#pending.values()];
    this.#pending.clear();
    const opened: CommunityMessage[] = [];
    for (const item of pending) opened.push(await this.#read(item.row, item.author));
    this.#put(opened);
  }

  async #page(query: string): Promise<{ messages: CommunityMessage[]; nextBefore: string | null }> {
    const page = parsePage(await this.#call('GET', `/v1/community/messages?${query}`));
    return { messages: await this.#ingest(page), nextBefore: page.nextBefore };
  }

  async #loadLatest(): Promise<void> {
    const { messages, nextBefore } = await this.#page(`limit=${PAGE}`);
    this.#put(messages, { loaded: true, older: nextBefore !== null });
  }

  /** The next older page (before the oldest loaded message). */
  async loadOlder(): Promise<void> {
    const oldest = this.#state.messages[0]?.seq;
    if (oldest === undefined) return;
    const { messages, nextBefore } = await this.#page(`before=${oldest}&limit=${PAGE}`);
    this.#put(messages, { older: nextBefore !== null });
  }

  /** Pulls every message after the newest one known (one pull at a time). */
  pullNew(): Promise<void> {
    this.#pull = this.#pull.catch(() => undefined).then(() => this.#pullNew());
    return this.#pull;
  }

  async #pullNew(): Promise<void> {
    while (this.#current) {
      const state = this.#state;
      const after = Math.max(state.messages[state.messages.length - 1]?.seq ?? 0, state.loaded ? 0 : state.baseHead);
      const { messages } = await this.#page(`after=${after}&limit=${PULL}`);
      this.#put(messages);
      if (messages.length < PULL) return;
    }
  }

  /**
   * The display name to offer: the one chosen in this browser, else the name of the user's newest own message, else
   * the user's `member.profile` in the active org (cleaned and cut to the limit), else empty.
   */
  defaultName(): string {
    const chosen = this.#deps.prefs?.communityName(this.#me) ?? null;
    if (chosen !== null) return chosen;
    const own = this.#state.messages.filter((m) => m.author === this.#me && m.name !== null);
    const newest = own[own.length - 1]?.name;
    if (newest !== undefined && newest !== null) return newest;
    const state = this.#store.getState();
    const org = state.session.activeOrg;
    const profile = org === null ? undefined : selectDirectory(org)(state).members.get(this.#me);
    return C.cleanName([...C.cleanName(profile ?? '')].slice(0, this.#state.nameChars).join(''));
  }

  /** Remembers the chosen display name in this browser (an empty name forgets it). */
  rememberName(name: string): void {
    this.#deps.prefs?.setCommunityName(this.#me, C.cleanName(name));
  }

  /**
   * Seals and posts a message (name and text cleaned by `cleanName`/`cleanText` first; a body that still breaks a rule
   * is `header_invalid`). A failed post that may succeed later keeps the sealed message: sending the same name and
   * text again repeats it byte for byte (the hub answers `replayed` if the first attempt was stored). A `429` sets
   * `retryAt`.
   */
  async post(name: string, text: string): Promise<void> {
    const key = this.#key;
    const identity = this.#store.getState().me.identity;
    if (key === null || identity === null) throw new HubError('community_no_key', 409);
    const cleanName = C.cleanName(name);
    const cleanText = C.cleanText(text);
    const kept = this.#kept;
    let message: unknown;
    if (kept !== null && kept.name === cleanName && kept.text === cleanText) {
      message = kept.message;
    } else {
      const body = C.bodyBytes(cleanName, cleanText);
      const now = this.#now();
      const header = C.newHeader({ id: uuid7(now), author: this.#me, epoch: key.epoch, ts: formatTime(now) });
      message = await C.sealMessage(key.secret, header, body, identity.sig.privateKey);
    }
    try {
      await this.#call('POST', '/v1/community/messages', { message });
    } catch (error) {
      const code = codeOf(error);
      this.#kept =
        code === 'unavailable' || code === 'rate_limited' ? { name: cleanName, text: cleanText, message } : null;
      if (code === 'community_muted') this.#patch({ muted: true });
      if (code === 'rate_limited') {
        const seconds = error instanceof HubError && error.retryAfter !== null ? error.retryAfter : RETRY_FALLBACK_S;
        this.#patch({ retryAt: this.#now() + seconds * 1000 });
      }
      throw error;
    }
    this.#kept = null;
    this.rememberName(cleanName);
    this.#patch({ posted: true, retryAt: null });
    await this.pullNew().catch(() => undefined);
  }

  /** Moves the read position to `seq` (never back). */
  async markRead(seq: number): Promise<void> {
    if (seq <= this.#state.lastRead) return;
    const body = await this.#call('PUT', '/v1/community/read', { seq });
    const read = isRecord(body) && Number.isSafeInteger(body.last_read_seq) ? (body.last_read_seq as number) : seq;
    this.#patch({ lastRead: Math.max(read, this.#state.lastRead) });
  }

  /** Deletes a message: the author's own, or any as a moderator. */
  async remove(id: string): Promise<void> {
    await this.#call('DELETE', `/v1/community/messages/${encodeURIComponent(id)}`);
    if (this.#current) removeMessage(this.#store, { id });
  }

  /** The muted users, newest mute first (moderators). */
  async mutes(): Promise<{ readonly user: string; readonly mutedAt: number }[]> {
    const body = await this.#call('GET', '/v1/community/mutes');
    const rows: unknown[] = isRecord(body) && Array.isArray(body.mutes) ? body.mutes : [];
    return rows
      .filter((row) => isRecord(row) && isText(row.user))
      .map((row) => {
        const { user, muted_at: mutedAt } = row as { user: string; muted_at: unknown };
        return { user, mutedAt: isText(mutedAt) ? timeOf(mutedAt) : 0 };
      });
  }

  /** Mutes or unmutes a user (moderators). */
  async setMuted(user: string, muted: boolean): Promise<void> {
    const target = `/v1/community/mutes/${encodeURIComponent(user)}`;
    await (muted ? this.#call('PUT', target, {}) : this.#call('DELETE', target));
  }
}
