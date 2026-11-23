// A simulated community hub for the engine tests (12-community.md section 4): the routes of `/v1/community*` in
// memory, with the hub's signature checks (self-wraps, request wraps, messages) done by the web twin of the format, and
// per-user clients (`Pick<SignedClient, 'request'>`). `fail` replaces the answer of a route (`METHOD /path` without the
// query) once or always; `answer` replaces a body; `calls` records every request.
import * as C from '../../../src/core/crypto/community';
import { Identity } from '../../../src/core/crypto/keys';
import { Secret } from '../../../src/core/crypto/secret';
import { b64uDecode, b64uEncode } from '../../../src/core/encoding/b64u';
import { uuid7 } from '../../../src/core/encoding/ids';
import { formatTime } from '../../../src/core/encoding/time';
import { HubError, type SignedClient } from '../../../src/core/net/signedFetch';
import { createStore, type Store } from '../../../src/core/store';
import { setIdentity, setSession } from '../../../src/core/store/slices/me';

export interface Wrapped {
  epoch: number;
  wrap: string;
  sig: string;
  wrapped_by: string;
  wrapper_sig: string;
}

export interface Row {
  seq: number;
  id: string;
  author: string;
  created_at: string;
  deleted: null | 'author' | 'moderator';
  message: unknown;
}

export interface User {
  readonly id: string;
  readonly identity: Identity;
  readonly store: Store;
  readonly client: Pick<SignedClient, 'request'>;
}

type Failure = HubError | (() => unknown);

export const T0 = Date.parse('2026-11-23T12:00:00.000Z');

export class CommunityHub {
  absent = false;
  epoch: { epoch: number; key_check: string } | null = null;
  readonly wraps = new Map<string, { epoch: number; wrap: string; sig: string }>();
  readonly requests = new Map<string, { id: string; kem_pub: string; sig: string; wrapped: Wrapped | null }>();
  readonly rejects: [wrapper: string, user: string][] = [];
  readonly members = new Map<string, { last_read: number; muted: string | null }>();
  readonly moderators = new Set<string>();
  readonly rows: Row[] = [];
  readonly keys = new Map<string, string>();
  /** Served instead of the real author keys (`null` drops the author). */
  readonly servedKeys = new Map<string, string | null>();
  readonly calls: string[] = [];
  /** The bodies of the requests, in the order of `calls`. */
  readonly sent: unknown[] = [];
  readonly fail = new Map<string, { error: Failure; times: number }>();
  /** Replaces the body of a route (`METHOD /path`) once it was computed. */
  readonly answer = new Map<string, (body: unknown) => unknown>();
  now = T0;

  /** A signed-in user with an unlocked identity and its own store and client. */
  async user(): Promise<User> {
    const identity = await Identity.generate();
    const id = uuid7(this.now + this.keys.size);
    this.keys.set(id, b64uEncode(identity.sigPublic()));
    const store = createStore();
    setSession(store, id, { keyId: 'k', expiresAt: this.now + 3_600_000 });
    setIdentity(store, identity);
    return { id, identity, store, client: { request: (m, t, o) => this.request(id, m, t, o?.body) } };
  }

  /** A new sign-in of `user` (same ID and identity, a fresh store). */
  signIn(user: User): User {
    const store = createStore();
    setSession(store, user.id, { keyId: 'k2', expiresAt: this.now + 3_600_000 });
    setIdentity(store, user.identity);
    return { ...user, store };
  }

  /** `error` for the next `times` calls of `route` (Infinity: always). */
  failWith(route: string, error: Failure, times = 1): void {
    this.fail.set(route, { error, times });
  }

  head(): number {
    return this.rows.length === 0 ? 0 : this.rows[this.rows.length - 1]!.seq;
  }

  /** Appends a row as stored (no checks): a tombstone when `message` is null. */
  insert(author: string, message: unknown, id = uuid7(this.now + this.rows.length)): Row {
    const row: Row = {
      seq: this.head() + 1,
      id,
      author,
      created_at: formatTime(this.now),
      deleted: message === null ? 'author' : null,
      message,
    };
    this.rows.push(row);
    return row;
  }

  /** Seals and inserts a message of `user` under `key` with any body (header fields overridable). */
  async sealed(user: User, key: Uint8Array, body: Uint8Array, fields: Partial<C.HeaderInput> = {}): Promise<Row> {
    const header = C.newHeader({ id: uuid7(this.now), author: user.id, epoch: 1, ts: formatTime(this.now), ...fields });
    const message = await C.sealMessage(Secret.of(key), header, body, user.identity.sig.privateKey);
    return this.insert(user.id, message, header.id as string);
  }

  async request(user: string, method: string, target: string, body: unknown): Promise<unknown> {
    const path = target.split('?')[0]!;
    const route = `${method} ${path}`;
    this.calls.push(`${method} ${target}`);
    this.sent.push(body);
    const failure = this.fail.get(route);
    if (failure !== undefined && failure.times > 0) {
      failure.times -= 1;
      if (failure.error instanceof HubError) throw failure.error;
      return failure.error();
    }
    if (this.absent) throw new HubError('not_found', 404);
    const result = await this.#route(user, method, path, new URLSearchParams(target.split('?')[1] ?? ''), body);
    const replace = this.answer.get(route);
    return replace === undefined ? result : replace(result);
  }

  #member(user: string) {
    const member = this.members.get(user);
    if (member === undefined) throw new HubError('community_not_member', 409);
    return member;
  }

  #holder(user: string): number {
    const epoch = this.epoch;
    if (epoch === null || !this.wraps.has(user)) throw new HubError('community_no_key', 409);
    return epoch.epoch;
  }

  #moderator(user: string): void {
    if (!this.moderators.has(user)) throw new HubError('forbidden', 403);
  }

  // eslint-disable-next-line @typescript-eslint/no-explicit-any -- request bodies of the engine, untyped JSON
  async #route(user: string, method: string, path: string, query: URLSearchParams, body: any): Promise<unknown> {
    const sigPub = b64uDecode(this.keys.get(user)!);
    if (method === 'POST' && path === '/v1/community/join') {
      if (!this.members.has(user)) this.members.set(user, { last_read: 0, muted: null });
      return { joined: true };
    }
    if (method === 'GET' && path === '/v1/community') {
      const member = this.members.get(user);
      const request = this.requests.get(user);
      const lastRead = member?.last_read ?? 0;
      return {
        epoch: this.epoch,
        wrap: this.epoch === null ? null : (this.wraps.get(user) ?? null),
        request: request === undefined ? null : { id: request.id, wrapped: request.wrapped },
        member: {
          joined: member !== undefined,
          muted: member?.muted != null,
          posted: this.rows.some((row) => row.author === user),
          last_read_seq: lastRead,
        },
        moderator: this.moderators.has(user),
        unread: this.rows.filter((row) => row.seq > lastRead && row.author !== user && row.deleted === null).length,
        head_seq: this.head(),
        limits: { text_chars: 2000, name_chars: 64, burst: 5, refill_seconds: 6 },
      };
    }
    if (method === 'POST' && path === '/v1/community/epochs') {
      this.#member(user);
      if (this.epoch !== null) throw new HubError('community_epoch_exists', 409);
      await C.verifyCommunityWrap(sigPub, body.epoch, user, body.wrap, body.sig);
      this.epoch = { epoch: body.epoch, key_check: body.key_check };
      this.wraps.set(user, { epoch: body.epoch, wrap: body.wrap, sig: body.sig });
      this.requests.delete(user);
      return { epoch: body.epoch };
    }
    if (method === 'PUT' && path === '/v1/community/request') {
      this.#member(user);
      if (this.epoch !== null && this.wraps.has(user)) throw new HubError('forbidden', 403);
      await C.verifyRequest(sigPub, { id: body.id, kem_pub: body.kem_pub, user }, body.sig);
      this.requests.set(user, { id: body.id, kem_pub: body.kem_pub, sig: body.sig, wrapped: null });
      return { id: body.id };
    }
    if (method === 'GET' && path === '/v1/community/requests') {
      this.#holder(user);
      const limit = Number(query.get('limit') ?? 20);
      const open = [...this.requests]
        .filter(([owner, r]) => owner !== user && r.wrapped === null)
        .filter(([owner]) => !this.rejects.some(([w, u]) => w === user && u === owner))
        .slice(0, limit);
      return { requests: open.map(([, r]) => ({ id: r.id, kem_pub: r.kem_pub })) };
    }
    if (method === 'POST' && path === '/v1/community/requests/wraps') {
      const epoch = this.#holder(user);
      let stored = 0;
      for (const item of body.wraps) {
        await C.verifyCommunityWrap(sigPub, epoch, item.request, item.wrap, item.sig);
        const entry = [...this.requests].find(([, r]) => r.id === item.request && r.wrapped === null);
        if (entry === undefined) continue;
        entry[1].wrapped = {
          epoch,
          wrap: item.wrap,
          sig: item.sig,
          wrapped_by: user,
          wrapper_sig: this.keys.get(user)!,
        };
        stored += 1;
      }
      return { stored, skipped: body.wraps.length - stored };
    }
    if (method === 'POST' && path === '/v1/community/request/reject') {
      const request = this.requests.get(user);
      if (request?.wrapped == null) throw new HubError('not_found', 404);
      this.rejects.push([request.wrapped.wrapped_by, user]);
      request.wrapped = null;
      return { rejected: true };
    }
    if (method === 'POST' && path === '/v1/community/wraps') {
      this.#member(user);
      if (this.epoch === null) throw new HubError('community_no_key', 409);
      await C.verifyCommunityWrap(sigPub, body.epoch, user, body.wrap, body.sig);
      this.wraps.set(user, { epoch: body.epoch, wrap: body.wrap, sig: body.sig });
      this.requests.delete(user);
      return { epoch: body.epoch };
    }
    if (method === 'GET' && path === '/v1/community/messages') {
      const limit = Number(query.get('limit') ?? 50);
      const after = query.get('after');
      let rows: Row[];
      let nextBefore: string | null = null;
      if (after !== null) {
        rows = this.rows.filter((row) => row.seq > Number(after)).slice(0, limit);
      } else {
        const before = Number(query.get('before') ?? Number.MAX_SAFE_INTEGER);
        const older = this.rows.filter((row) => row.seq < before).reverse();
        rows = older.slice(0, limit);
        if (older.length > limit) nextBefore = String(rows[rows.length - 1]!.seq);
      }
      const authors: Record<string, { sig_pub: string; moderator: boolean }> = {};
      for (const row of rows) {
        if (row.deleted !== null) continue;
        const served = this.servedKeys.has(row.author) ? this.servedKeys.get(row.author)! : this.keys.get(row.author)!;
        if (served !== null) authors[row.author] = { sig_pub: served, moderator: this.moderators.has(row.author) };
      }
      return { messages: rows.map((row) => ({ ...row })), authors, next_before: nextBefore };
    }
    if (method === 'POST' && path === '/v1/community/messages') {
      const member = this.#member(user);
      if (member.muted !== null) throw new HubError('community_muted', 403);
      const epoch = this.#holder(user);
      const header = await C.verifyMessage(body.message, sigPub);
      if (header.author !== user) throw new HubError('forbidden', 403);
      if (header.epoch !== epoch) throw new HubError('epoch_stale', 400);
      const same = this.rows.find((row) => row.id === header.id);
      if (same !== undefined) {
        if (JSON.stringify(same.message) !== JSON.stringify(body.message)) throw new HubError('id_conflict', 409);
        return { seq: same.seq, replayed: true };
      }
      return { seq: this.insert(user, body.message, header.id as string).seq };
    }
    if (method === 'DELETE' && path.startsWith('/v1/community/messages/')) {
      const id = decodeURIComponent(path.slice('/v1/community/messages/'.length));
      const row = this.rows.find((r) => r.id === id);
      if (row === undefined) throw new HubError('not_found', 404);
      const role = row.author === user ? 'author' : 'moderator';
      if (role === 'moderator') this.#moderator(user);
      row.deleted ??= role;
      row.message = null;
      return { deleted: row.deleted };
    }
    if (method === 'PUT' && path === '/v1/community/read') {
      const member = this.#member(user);
      member.last_read = Math.max(member.last_read, Math.min(body.seq, this.head()));
      return { last_read_seq: member.last_read };
    }
    if (method === 'GET' && path === '/v1/community/mutes') {
      this.#moderator(user);
      const mutes = [...this.members].filter(([, m]) => m.muted !== null);
      return { mutes: mutes.map(([id, m]) => ({ user: id, muted_at: m.muted })) };
    }
    if (path.startsWith('/v1/community/mutes/')) {
      this.#moderator(user);
      const target = this.#member(decodeURIComponent(path.slice('/v1/community/mutes/'.length)));
      target.muted = method === 'PUT' ? formatTime(this.now) : null;
      return { muted: method === 'PUT' };
    }
    throw new HubError('not_found', 404);
  }
}

/** Gives `user` the community key `raw` of epoch 1 as if it had created it (key check and self-wrap at the hub). */
export async function seedKey(hub: CommunityHub, user: User, raw: Uint8Array): Promise<void> {
  const secret = Secret.of(raw);
  const wrap = await C.wrapCommunityKey(user.identity.kemPublic(), 1, user.id, secret);
  hub.epoch = { epoch: 1, key_check: b64uEncode(await C.keyCheck(secret, 1)) };
  hub.wraps.set(user.id, {
    epoch: 1,
    wrap,
    sig: await C.signCommunityWrap(user.identity.sig.privateKey, 1, user.id, wrap),
  });
  hub.members.set(user.id, { last_read: 0, muted: null });
}

/** Waits until `check` holds (the engine's work after a frame runs detached and includes WebCrypto calls). */
export async function until(check: () => boolean): Promise<void> {
  for (let i = 0; i < 5000 && !check(); i += 1) await new Promise((resolve) => setTimeout(resolve, 1));
  if (!check()) throw new Error('condition not reached');
}
