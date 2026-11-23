// The community client (12-community.md section 4.2) against a simulated hub: bootstrap of epoch 1, key requests
// served by a holder and opened by the requester (wrong keys and forged wraps rejected), self-wraps after a new
// sign-in, reading with pinned author keys and every check that makes a message unverifiable, live frames, posting
// (429, mute, retry with the same bytes), read position, deletion, moderation, the display name, a hub without the
// community, failures and sign-outs in flight.
import { describe, expect, it } from 'vitest';
import { Prefs, type PrefStorage } from '../../../src/core/account/prefs';
import {
  Community,
  community,
  communityOf,
  codeOf,
  POLL_MS,
  RETRY_FALLBACK_S,
  SERVE_ROUNDS,
  TICK_MS,
} from '../../../src/core/community/engine';
import * as C from '../../../src/core/crypto/community';
import { CryptoError } from '../../../src/core/crypto/errors';
import { Secret } from '../../../src/core/crypto/secret';
import { random } from '../../../src/core/crypto/subtle';
import { b64uDecode } from '../../../src/core/encoding/b64u';
import { uuid7 } from '../../../src/core/encoding/ids';
import { bodyJson } from '../../../src/core/encoding/json';
import { HubError } from '../../../src/core/net/signedFetch';
import { frames } from '../../../src/core/store/slices/community';
import { putEvents } from '../../../src/core/store/slices/events';
import { setIdentity } from '../../../src/core/store/slices/me';
import { setActiveOrg } from '../../../src/core/store/slices/session';
import { ManualTimers, settle } from '../net/stream';
import { CommunityHub, seedKey, T0, until, type User } from './hub';

class MemoryStorage implements PrefStorage {
  readonly items = new Map<string, string>();
  getItem(key: string) {
    return this.items.get(key) ?? null;
  }
  setItem(key: string, value: string) {
    this.items.set(key, value);
  }
  removeItem(key: string) {
    this.items.delete(key);
  }
}

interface Client {
  readonly engine: Community;
  readonly timers: ManualTimers;
  readonly prefs: Prefs;
  readonly user: User;
}

function connect(hub: CommunityHub, user: User, withPrefs = true): Client {
  const timers = new ManualTimers();
  const prefs = new Prefs(new MemoryStorage());
  const engine = new Community({
    store: user.store,
    client: user.client,
    now: () => hub.now,
    timers,
    random: () => 0.5,
    ...(withPrefs && { prefs }),
  });
  return { engine, timers, prefs, user };
}

const state = (client: Client | User) => ('engine' in client ? client.user : client).store.getState().community;
const frame = (client: Client, change: string, seq = 0) => frames.community(client.user.store, { change, seq });
const body = (name: string, text: string) => bodyJson({ name, text, v: 1 });

async function failure(run: () => Promise<unknown>): Promise<string> {
  try {
    await run();
  } catch (error) {
    return codeOf(error);
  }
  return 'none';
}

/** A holder `a` (with the known key `raw`, started) and a hub. */
async function withHolder(raw = random(32)) {
  const hub = new CommunityHub();
  const a = await hub.user();
  await seedKey(hub, a, raw);
  const A = connect(hub, a);
  await A.engine.start();
  return { hub, a, A, raw };
}

/** `b` started without the key (its request open), plus the holder of `withHolder`. */
async function withRequester() {
  const setup = await withHolder();
  const b = await setup.hub.user();
  const B = connect(setup.hub, b);
  await B.engine.start();
  return { ...setup, b, B };
}

/** Wraps `key` to the open request of `user` as `wrapper` would, with any wrapper key and epoch. */
async function forgeWrap(hub: CommunityHub, user: User, wrapper: User, key: Secret, overrides: object = {}) {
  const request = hub.requests.get(user.id)!;
  const wrap = await C.wrapCommunityKey(b64uDecode(request.kem_pub), 1, request.id, key);
  request.wrapped = {
    epoch: 1,
    wrap,
    sig: await C.signCommunityWrap(wrapper.identity.sig.privateKey, 1, request.id, wrap),
    wrapped_by: wrapper.id,
    wrapper_sig: hub.keys.get(wrapper.id)!,
    ...overrides,
  };
}

describe('key distribution', () => {
  it('creates epoch 1 on an empty hub', async () => {
    const hub = new CommunityHub();
    const a = await hub.user();
    const A = connect(hub, a);
    const started = A.engine.start();
    expect(A.engine.start()).toBe(started);
    expect(state(A).phase).toBe('loading');
    await started;
    expect(state(A)).toMatchObject({
      phase: 'ready',
      key: 'ready',
      loaded: true,
      older: false,
      me: a.id,
      messages: [],
    });
    expect(hub.epoch?.epoch).toBe(1);
    expect(hub.wraps.has(a.id)).toBe(true);
    expect(hub.calls).toEqual([
      'POST /v1/community/join',
      'GET /v1/community',
      'POST /v1/community/epochs',
      'GET /v1/community/messages?limit=50',
      'GET /v1/community/requests?limit=20',
    ]);
  });

  it('requests the key, gets it from a holder and reads what was written before', async () => {
    const { hub, a, A, b, B } = await withRequester();
    expect(state(B)).toMatchObject({ phase: 'ready', key: 'waiting', waitingSince: T0 });
    expect(hub.requests.has(b.id)).toBe(true);
    await A.engine.post('Anna', 'before');
    frame(B, 'message', 1);
    await until(() => state(B).messages.length === 1);
    expect(state(B).messages.map((m) => m.status)).toEqual(['pending']);

    frame(A, 'request', 1);
    expect(A.timers.pending).toEqual([1500]);
    A.timers.advance(1500);
    await until(() => hub.requests.get(b.id)?.wrapped != null);
    expect(hub.requests.get(b.id)?.wrapped?.wrapped_by).toBe(a.id);

    frame(B, 'key', 1);
    await until(() => state(B).key === 'ready' && hub.calls.at(-1) === 'GET /v1/community/requests?limit=20');
    expect(state(B)).toMatchObject({ key: 'ready', keyCode: null, waitingSince: null });
    expect(state(B).messages).toMatchObject([{ status: 'verified', name: 'Anna', text: 'before', author: a.id }]);
    expect(hub.wraps.has(b.id)).toBe(true);
    expect(hub.requests.has(b.id)).toBe(false);
    expect(B.timers.pending).toEqual([]);
    expect(hub.calls.at(-1)).toBe('GET /v1/community/requests?limit=20');
  });

  it('opens its self-wrap after a new sign-in', async () => {
    const { hub, a, raw } = await withHolder();
    hub.insert(a.id, null);
    const again = connect(hub, hub.signIn(a));
    hub.calls.length = 0;
    await again.engine.start();
    expect(state(again).key).toBe('ready');
    expect(hub.calls).not.toContain('PUT /v1/community/request');
    // the key is the one of the epoch: a message sealed under it opens
    await hub.sealed(a, raw, body('Anna', 'x'));
    await again.engine.pullNew();
    expect(state(again).messages.at(-1)).toMatchObject({ status: 'verified', text: 'x' });
  });

  it('marks a broken self-wrap as failed', async () => {
    const { hub, a } = await withHolder();
    const sw = hub.wraps.get(a.id)!;
    hub.wraps.set(a.id, { ...sw, sig: sw.sig.slice(0, -4) + 'AAAA' });
    const again = connect(hub, hub.signIn(a));
    await again.engine.start();
    expect(state(again)).toMatchObject({ key: 'failed', keyCode: 'signature_invalid' });

    const wrap = await C.wrapCommunityKey(a.identity.kemPublic(), 1, a.id, Secret.random());
    const sig = await C.signCommunityWrap(a.identity.sig.privateKey, 1, a.id, wrap);
    hub.wraps.set(a.id, { epoch: 1, wrap, sig });
    const third = connect(hub, hub.signIn(a));
    await third.engine.start();
    expect(state(third)).toMatchObject({ key: 'failed', keyCode: 'decrypt_failed' });
  });

  it('rejects a wrong key, a forged wrap and a wrap of another epoch', async () => {
    const { hub, a, A, b, B } = await withRequester();
    const m = await hub.user();
    const cases: [object, Secret][] = [
      [{}, Secret.random()],
      [{ wrapper_sig: hub.keys.get(a.id) }, Secret.random()],
      [{ epoch: 2 }, Secret.random()],
    ];
    for (const [index, [overrides, key]] of cases.entries()) {
      await forgeWrap(hub, b, m, key, overrides);
      frame(B, 'key');
      await until(() => hub.rejects.length === index + 1);
      expect(state(B).key).toBe('waiting');
      expect(hub.requests.get(b.id)?.wrapped).toBeNull();
    }
    expect(hub.rejects).toEqual([
      [m.id, b.id],
      [m.id, b.id],
      [m.id, b.id],
    ]);
    await A.engine.serve();
    frame(B, 'key');
    await until(() => state(B).key === 'ready');
  });

  it('tolerates a wrap that is gone at the reject, and fails on other reject errors', async () => {
    const { hub, b, B } = await withRequester();
    const m = await hub.user();
    await forgeWrap(hub, b, m, Secret.random());
    hub.failWith('POST /v1/community/request/reject', new HubError('not_found', 404));
    await B.engine.refresh();
    expect(state(B).key).toBe('waiting');
    hub.failWith('POST /v1/community/request/reject', new HubError('unavailable', 503));
    expect(await failure(() => B.engine.refresh())).toBe('unavailable');
  });

  it('reads the state again after losing an epoch race', async () => {
    const hub = new CommunityHub();
    const a = await hub.user();
    const b = await hub.user();
    hub.failWith('POST /v1/community/epochs', () =>
      seedKey(hub, a, random(32)).then(() => {
        throw new HubError('community_epoch_exists', 409);
      }),
    );
    const B = connect(hub, b);
    await B.engine.start();
    expect(state(B)).toMatchObject({ phase: 'ready', key: 'waiting' });
    expect(hub.requests.has(b.id)).toBe(true);
  });

  it('gives a wrapped request of another browser a minute, then replaces it', async () => {
    const { hub, a, b } = await withRequester();
    const request = hub.requests.get(b.id)!;
    await forgeWrap(hub, b, a, Secret.random());
    const other = connect(hub, hub.signIn(b));
    hub.calls.length = 0;
    await other.engine.start();
    expect(hub.calls).not.toContain('PUT /v1/community/request');
    expect(state(other).key).toBe('waiting');
    other.timers.advance(POLL_MS);
    await until(() => hub.calls.includes('PUT /v1/community/request'));
    // the call is recorded before the double stores it: wait for the replacement itself
    await until(() => hub.requests.get(b.id)?.id !== request.id);
    expect(hub.requests.get(b.id)?.id).not.toBe(request.id);
  });

  it('replaces an unwrapped request it does not know, and keeps its own', async () => {
    const { hub, b, B } = await withRequester();
    const first = hub.requests.get(b.id)!.id;
    hub.calls.length = 0;
    frame(B, 'mute');
    await until(() => hub.calls.length === 1);
    await settle();
    expect(hub.requests.get(b.id)!.id).toBe(first);
    const other = connect(hub, hub.signIn(b));
    await other.engine.start();
    expect(hub.requests.get(b.id)!.id).not.toBe(first);
  });

  it('polls every minute while waiting, also after a failed poll', async () => {
    const { hub, B } = await withRequester();
    expect(B.timers.pending).toEqual([POLL_MS]);
    hub.failWith('GET /v1/community', new HubError('unavailable', 503));
    hub.calls.length = 0;
    B.timers.advance(POLL_MS);
    await until(() => hub.calls.length === 1 && B.timers.pending.length === 1);
    expect(B.timers.pending).toEqual([POLL_MS]);
    hub.calls.length = 0;
    B.timers.advance(POLL_MS);
    await until(() => hub.calls.length > 0);
    expect(hub.calls[0]).toBe('GET /v1/community');
  });

  it('runs no key flow without an identity', async () => {
    const hub = new CommunityHub();
    const a = await hub.user();
    setIdentity(a.store, null);
    const A = connect(hub, a);
    await A.engine.start();
    expect(state(A)).toMatchObject({ phase: 'ready', key: 'none' });
    expect(hub.epoch).toBeNull();
    expect(await failure(() => A.engine.post('A', 'x'))).toBe('community_no_key');
  });
});

describe('serving requests', () => {
  it('skips entries that cannot be sealed', async () => {
    const { hub, a, A, b } = await withRequester();
    const real = hub.requests.get(b.id)!;
    hub.answer.set('GET /v1/community/requests', () => ({
      requests: [null, { id: 'x', kem_pub: 'AAAA' }, { id: real.id, kem_pub: real.kem_pub }],
    }));
    await A.engine.serve();
    expect(hub.requests.get(b.id)?.wrapped?.wrapped_by).toBe(a.id);
    expect(hub.sent.at(-1)).toMatchObject({ wraps: [{ request: real.id, epoch: 1 }] });
  });

  it('serves full pages up to the round limit, once at a time, and quietly on failures', async () => {
    const { hub, A } = await withHolder();
    const full = { requests: Array.from({ length: 20 }, () => ({ id: uuid7(), kem_pub: 'AAAA' })) };
    hub.answer.set('GET /v1/community/requests', () => full);
    hub.calls.length = 0;
    const first = A.engine.serve();
    expect(A.engine.serve()).toBe(first);
    await first;
    expect(hub.calls).toEqual(Array(SERVE_ROUNDS).fill('GET /v1/community/requests?limit=20'));
    hub.answer.set('GET /v1/community/requests', () => ({}));
    hub.calls.length = 0;
    await A.engine.serve();
    expect(hub.calls).toEqual(['GET /v1/community/requests?limit=20']);
    hub.failWith('GET /v1/community/requests', new HubError('unavailable', 503));
    await A.engine.serve();
  });

  it('does not serve without the key or an identity', async () => {
    const { hub, B } = await withRequester();
    hub.calls.length = 0;
    await B.engine.serve();
    frame(B, 'request');
    expect(B.timers.pending).toEqual([POLL_MS]);
    const { hub: hub2, a, A } = await withHolder();
    setIdentity(a.store, null);
    hub2.calls.length = 0;
    await A.engine.serve();
    expect([...hub.calls, ...hub2.calls]).toEqual([]);
  });
});

describe('reading', () => {
  it('loads older pages and pulls new messages page by page', async () => {
    const hub = new CommunityHub();
    const a = await hub.user();
    await seedKey(hub, a, random(32));
    for (let i = 0; i < 60; i += 1) hub.insert(a.id, null);
    const A = connect(hub, a);
    expect(await A.engine.loadOlder()).toBeUndefined();
    await A.engine.start();
    expect(state(A).messages).toHaveLength(50);
    expect(state(A)).toMatchObject({ older: true, loaded: true });
    expect(state(A).messages[0]).toMatchObject({ seq: 11, removed: true, status: 'verified', text: null });
    await A.engine.loadOlder();
    expect(state(A).messages).toHaveLength(60);
    expect(state(A).older).toBe(false);
    for (let i = 0; i < 150; i += 1) hub.insert(a.id, null);
    hub.calls.length = 0;
    frame(A, 'message');
    await until(() => state(A).messages.length === 210);
    expect(hub.calls).toEqual([
      'GET /v1/community/messages?after=60&limit=100',
      'GET /v1/community/messages?after=160&limit=100',
    ]);
    expect(state(A).messages).toHaveLength(210);
  });

  it('pulls from the hub head before the first page was loaded', async () => {
    const hub = new CommunityHub();
    const a = await hub.user();
    const A = connect(hub, a);
    await A.engine.pullNew();
    expect(hub.calls).toEqual(['GET /v1/community/messages?after=0&limit=100']);
  });

  it('pins author keys on first sight and refuses a changed key', async () => {
    const { hub, a, A, b, B, raw } = await withRequester();
    await A.engine.serve();
    frame(B, 'key');
    await until(() => state(B).key === 'ready');
    await hub.sealed(a, raw, body('Anna', 'one'));
    await B.engine.pullNew();
    expect(B.prefs.communityKeys(b.id)).toEqual({ [a.id]: hub.keys.get(a.id) });
    const again = connect(hub, hub.signIn(b));
    again.prefs.setCommunityKeys(b.id, { [a.id]: hub.keys.get(b.id)! });
    await again.engine.start();
    expect(state(again).messages.at(-1)).toMatchObject({ status: 'unverifiable', name: null, text: null });
  });

  it('shows no content of a message that fails any check', async () => {
    const { hub, a, raw } = await withHolder();
    const c = await hub.user();
    const good = await hub.sealed(a, raw, body('Anna', 'ok'));
    hub.insert(a.id, good.message); // another row ID than the header's
    hub.insert(c.id, good.message, good.id); // another author than the header's
    hub.servedKeys.set(c.id, hub.keys.get(a.id)!);
    await hub.sealed(a, raw, body('Anna', 'x'), { epoch: 2 });
    await hub.sealed(a, raw, body('Anna', ''));
    await hub.sealed(a, random(32), body('Anna', 'x'));
    const d = await hub.user();
    hub.servedKeys.set(d.id, null);
    await hub.sealed(d, raw, body('Dora', 'x'));
    const e = await hub.user();
    hub.servedKeys.set(e.id, 'not a key');
    await hub.sealed(e, raw, body('Emil', 'x'));
    const tampered = await hub.sealed(a, raw, body('Anna', 'x'));
    tampered.message = { ...(tampered.message as object), sig: (good.message as { sig: string }).sig };
    const late = hub.insert(a.id, null);
    late.created_at = 'yesterday';
    const reader = connect(hub, hub.signIn(a), false);
    await reader.engine.start();
    expect(state(reader).messages.map((m) => m.status)).toEqual([
      'verified',
      ...Array(8).fill('unverifiable'),
      'verified',
    ]);
    expect(state(reader).messages.at(-1)).toMatchObject({ removed: true, at: 0 });
    expect(state(reader).messages[0]!.at).toBe(T0);
  });

  it('opens pending messages when the key arrives, unless another epoch', async () => {
    const { hub, a, A, b, B, raw } = await withRequester();
    await hub.sealed(a, raw, body('Anna', 'x'), { epoch: 2 });
    await hub.sealed(a, raw, body('Anna', 'y'));
    await B.engine.pullNew();
    expect(state(B).messages.map((m) => m.status)).toEqual(['pending', 'pending']);
    await A.engine.serve();
    frame(B, 'key');
    await until(() => state(B).messages.every((m) => m.status !== 'pending'));
    expect(state(B).messages.map((m) => m.status)).toEqual(['unverifiable', 'verified']);
    expect(b.id).not.toBe(a.id);
  });

  it('refuses malformed pages', async () => {
    const { hub, A } = await withHolder();
    hub.insert(hub.keys.keys().next().value!, null);
    await A.engine.pullNew();
    hub.answer.set('GET /v1/community/messages', () => ({ messages: [{ seq: 'x' }], authors: {} }));
    expect(await failure(() => A.engine.loadOlder())).toBe('request_invalid');
    hub.answer.set('GET /v1/community/messages', () => null);
    expect(await failure(() => A.engine.pullNew())).toBe('request_invalid');
    hub.answer.set('GET /v1/community/messages', () => ({
      messages: [],
      authors: { x: null, y: { sig_pub: 1 } },
      next_before: 5,
    }));
    await A.engine.pullNew();
  });

  it('marks moderators and drops removed messages', async () => {
    const { hub, a, A, raw } = await withHolder();
    hub.moderators.add(a.id);
    const row = await hub.sealed(a, raw, body('Anna', 'x'));
    await A.engine.pullNew();
    expect(state(A).messages[0]).toMatchObject({ moderator: true, status: 'verified' });
    frame(A, 'delete', row.seq);
    expect(state(A).messages[0]).toMatchObject({ removed: true, text: null, name: null });
  });
});

describe('live frames', () => {
  it('ignores frames before the start and unknown changes', async () => {
    const hub = new CommunityHub();
    const a = await hub.user();
    const A = connect(hub, a);
    frame(A, 'message');
    await settle();
    expect(hub.calls).toEqual([]);
    await A.engine.start();
    hub.calls.length = 0;
    frame(A, 'other');
    frame(A, 'delete', 99);
    await settle();
    expect(hub.calls).toEqual([]);
  });

  it('reads state and new messages again after a reconnect or resync', async () => {
    const { hub, A } = await withHolder();
    hub.calls.length = 0;
    frames.hello(A.user.store);
    await until(() => hub.calls.length === 2);
    expect(hub.calls).toEqual(['GET /v1/community', 'GET /v1/community/messages?after=0&limit=100']);
    hub.calls.length = 0;
    frames.resync(A.user.store);
    await until(() => hub.calls.length === 2);
    expect(hub.calls).toEqual(['GET /v1/community', 'GET /v1/community/messages?after=0&limit=100']);
  });

  it('swallows failures of frame-driven work and of the pull after a post', async () => {
    const { hub, A } = await withHolder();
    const down = new HubError('unavailable', 503);
    for (const [change, route] of [
      ['message', 'GET /v1/community/messages'],
      ['key', 'GET /v1/community'],
      ['hello', 'GET /v1/community'],
    ] as const) {
      hub.failWith(route, down);
      hub.calls.length = 0;
      frame(A, change);
      await until(() => hub.fail.get(route)!.times === 0);
      await settle();
    }
    hub.failWith('GET /v1/community/messages', down);
    await A.engine.post('Anna', 'x');
    expect(hub.rows).toHaveLength(1);
    expect(hub.fail.get('GET /v1/community/messages')!.times).toBe(0);
  });

  it('follows a mute', async () => {
    const { hub, a, A } = await withHolder();
    hub.members.get(a.id)!.muted = '2026-11-23T12:00:00.000Z';
    frame(A, 'mute');
    await until(() => state(A).muted);
  });
});

describe('posting', () => {
  it('cleans name and text, remembers the name and shows the message', async () => {
    const { hub, a, A } = await withHolder();
    await A.engine.post('  Anna​ ', '\r\nHallo\r\nWelt ‮');
    expect(state(A)).toMatchObject({ posted: true, retryAt: null });
    expect(state(A).messages).toMatchObject([{ status: 'verified', name: 'Anna', text: 'Hallo\nWelt', author: a.id }]);
    expect(A.prefs.communityName(a.id)).toBe('Anna');
    expect(A.engine.defaultName()).toBe('Anna');
    expect(hub.rows).toHaveLength(1);
    expect(await failure(() => A.engine.post('Anna', ' \n '))).toBe('header_invalid');
  });

  it('keeps the sealed message for a retry after 429 or an unreachable hub', async () => {
    const { hub, A } = await withHolder();
    hub.failWith('POST /v1/community/messages', new HubError('rate_limited', 429, null, 4));
    expect(await failure(() => A.engine.post('Anna', 'x'))).toBe('rate_limited');
    expect(state(A).retryAt).toBe(T0 + 4000);
    hub.failWith('POST /v1/community/messages', new HubError('rate_limited', 429));
    expect(await failure(() => A.engine.post('Anna', 'x'))).toBe('rate_limited');
    expect(state(A).retryAt).toBe(T0 + RETRY_FALLBACK_S * 1000);
    hub.failWith('POST /v1/community/messages', new HubError('unavailable', 0));
    expect(await failure(() => A.engine.post('Anna', 'x'))).toBe('unavailable');
    await A.engine.post('Anna', 'x');
    const posts = hub.sent.filter((_, i) => hub.calls[i] === 'POST /v1/community/messages');
    expect(new Set(posts.map((p) => JSON.stringify(p))).size).toBe(1);
    expect(state(A).retryAt).toBeNull();
    // a changed text is a new message
    hub.failWith('POST /v1/community/messages', new HubError('unavailable', 0));
    await failure(() => A.engine.post('Anna', 'y'));
    await A.engine.post('Anna', 'z');
    expect(hub.rows.map((r) => r.seq)).toEqual([1, 2]);
  });

  it('notes a mute and drops the sealed message after a refusal', async () => {
    const { hub, a, A } = await withHolder();
    hub.members.get(a.id)!.muted = '2026-11-23T12:00:00.000Z';
    expect(await failure(() => A.engine.post('Anna', 'x'))).toBe('community_muted');
    expect(state(A).muted).toBe(true);
    hub.members.get(a.id)!.muted = null;
    hub.failWith('POST /v1/community/messages', () => {
      throw new Error('boom');
    });
    expect(await failure(() => A.engine.post('Anna', 'x'))).toBe('unavailable');
    await A.engine.post('Anna', 'x');
    const posts = hub.sent.filter((_, i) => hub.calls[i] === 'POST /v1/community/messages');
    expect(new Set(posts.map((p) => JSON.stringify(p))).size).toBe(2);
  });
});

describe('read position, deletion and moderation', () => {
  it('moves the read position forward only', async () => {
    const { hub, a, raw } = await withHolder();
    const b = await hub.user();
    await hub.sealed(a, raw, body('Anna', '1'));
    await hub.sealed(a, raw, body('Anna', '2'));
    const B = connect(hub, b);
    await B.engine.start();
    expect(state(B).unread).toBe(2);
    await B.engine.markRead(1);
    expect(state(B)).toMatchObject({ lastRead: 1, unread: 1 });
    hub.calls.length = 0;
    await B.engine.markRead(1);
    expect(hub.calls).toEqual([]);
    await B.engine.markRead(9);
    expect(state(B)).toMatchObject({ lastRead: 2, unread: 0 });
    hub.answer.set('PUT /v1/community/read', () => ({}));
    await B.engine.markRead(3);
    expect(state(B).lastRead).toBe(3);
    hub.answer.set('PUT /v1/community/read', () => ({ last_read_seq: 1 }));
    await B.engine.markRead(4);
    expect(state(B).lastRead).toBe(3);
  });

  it('deletes own messages and lets moderators remove and mute', async () => {
    const { hub, a, A, raw } = await withHolder();
    const b = await hub.user();
    hub.members.set(b.id, { last_read: 0, muted: null });
    const own = await hub.sealed(a, raw, body('Anna', 'x'));
    const other = await hub.sealed(b, raw, body('Bea', 'y'));
    await A.engine.pullNew();
    await A.engine.remove(own.id);
    expect(state(A).messages[0]!.removed).toBe(true);
    expect(await failure(() => A.engine.remove(other.id))).toBe('forbidden');
    hub.moderators.add(a.id);
    await A.engine.remove(other.id);
    expect(hub.rows[1]!.deleted).toBe('moderator');
    await A.engine.setMuted(b.id, true);
    expect(await A.engine.mutes()).toEqual([{ user: b.id, mutedAt: T0 }]);
    await A.engine.setMuted(b.id, false);
    expect(await A.engine.mutes()).toEqual([]);
    expect(hub.calls).toContain(`DELETE /v1/community/mutes/${b.id}`);
    hub.answer.set('GET /v1/community/mutes', () => ({ mutes: [null, { user: 1 }, { user: 'x', muted_at: 5 }] }));
    expect(await A.engine.mutes()).toEqual([{ user: 'x', mutedAt: 0 }]);
    hub.answer.set('GET /v1/community/mutes', () => ({ mutes: [{ user: 'y', muted_at: 'bad' }] }));
    expect(await A.engine.mutes()).toEqual([{ user: 'y', mutedAt: 0 }]);
    hub.answer.set('GET /v1/community/mutes', () => null);
    expect(await A.engine.mutes()).toEqual([]);
  });
});

describe('display name', () => {
  it('offers the remembered name, the newest own message, the member profile, else nothing', async () => {
    const { hub, a, raw } = await withHolder();
    const reader = connect(hub, hub.signIn(a), false);
    await reader.engine.start();
    expect(reader.engine.defaultName()).toBe('');
    const org = uuid7();
    setActiveOrg(reader.user.store, org);
    expect(reader.engine.defaultName()).toBe('');
    const entry = (displayName: string) => ({
      org,
      scopeId: org,
      scopeSeq: 1,
      receivedAt: null,
      event: null,
      status: 'verified' as const,
      code: null,
      header: { kind: 'member.profile', ref: a.id, author: a.id },
      body: { display_name: displayName },
    });
    putEvents(reader.user.store, org, [entry(' Zoë​ Müller ')]);
    expect(reader.engine.defaultName()).toBe('Zoë Müller');
    putEvents(reader.user.store, org, [entry(`${'x'.repeat(63)} y`)]);
    expect(reader.engine.defaultName()).toBe('x'.repeat(63));
    await hub.sealed(a, raw, body('Anna', 'x'));
    await hub.sealed(a, raw, body('Anja', 'x'));
    await reader.engine.pullNew();
    expect(reader.engine.defaultName()).toBe('Anja');
    reader.engine.rememberName('Bea');

    const A = connect(hub, hub.signIn(a));
    A.engine.rememberName(' Bea ');
    expect(A.engine.defaultName()).toBe('Bea');
    A.engine.rememberName('');
    expect(A.prefs.communityName(a.id)).toBeNull();
  });
});

describe('start, tick and failures', () => {
  it('stops for good on a hub without the community', async () => {
    const hub = new CommunityHub();
    hub.absent = true;
    const a = await hub.user();
    const A = connect(hub, a);
    await A.engine.tick();
    expect(state(A).phase).toBe('absent');
    hub.now += TICK_MS;
    await A.engine.tick();
    frame(A, 'message');
    await settle();
    expect(hub.calls).toEqual(['POST /v1/community/join']);
  });

  it('stops when a route is missing after a wait was armed', async () => {
    const { hub } = await withHolder();
    const b = await hub.user();
    const B = connect(hub, b);
    hub.failWith('GET /v1/community/messages', new HubError('not_found', 404));
    await B.engine.start();
    expect(state(B)).toMatchObject({ phase: 'absent', key: 'waiting' });
    expect(B.timers.pending).toEqual([]);
  });

  it('shows a failed start and starts again on the next tick', async () => {
    const hub = new CommunityHub();
    const a = await hub.user();
    const A = connect(hub, a);
    hub.failWith('POST /v1/community/epochs', new HubError('unavailable', 503));
    const started = A.engine.start();
    const ticked = A.engine.tick();
    await Promise.all([started, ticked]);
    expect(state(A)).toMatchObject({ phase: 'error', code: 'unavailable' });
    await A.engine.tick();
    hub.now += TICK_MS;
    await A.engine.tick();
    expect(state(A)).toMatchObject({ phase: 'ready', key: 'ready' });
    hub.calls.length = 0;
    hub.now += TICK_MS;
    await A.engine.tick();
    expect(hub.calls).toEqual(['GET /v1/community', 'GET /v1/community/requests?limit=20']);
    hub.failWith('GET /v1/community', new HubError('unavailable', 503));
    hub.now += TICK_MS;
    await A.engine.tick();
  });

  it('maps unknown failures and malformed state', async () => {
    const hub = new CommunityHub();
    const a = await hub.user();
    const A = connect(hub, a);
    hub.failWith('POST /v1/community/join', () => {
      throw new Error('boom');
    });
    await A.engine.start();
    expect(state(A)).toMatchObject({ phase: 'error', code: 'unavailable' });
    await A.engine.start();
    expect(state(A)).toMatchObject({ phase: 'ready', key: 'ready' });
    const patched = (patch: object) => (b: unknown) => ({ ...(b as object), ...patch });
    const variants = [
      () => null,
      patched({ member: null }),
      patched({ limits: null }),
      patched({ epoch: { epoch: '1', key_check: 'x' } }),
      patched({ epoch: {} }),
      patched({ wrap: { wrap: 1 } }),
      patched({ wrap: 5 }),
      patched({ request: { id: 1 } }),
      patched({ request: 'x' }),
    ];
    for (const variant of variants) {
      hub.answer.set('GET /v1/community', variant);
      expect(await failure(() => A.engine.refresh())).toBe('request_invalid');
    }
    hub.answer.set(
      'GET /v1/community',
      patched({ member: { last_read_seq: -1 }, unread: 'x', head_seq: 1.5, limits: {} }),
    );
    await A.engine.refresh();
    expect(state(A)).toMatchObject({ lastRead: 0, unreadBase: 0, baseHead: 0, textChars: 2000, nameChars: 64 });
    expect(codeOf(new CryptoError('decrypt_failed'))).toBe('decrypt_failed');
  });

  it('writes nothing after a sign-out', async () => {
    const hub = new CommunityHub();
    const a = await hub.user();
    const A = connect(hub, a);
    hub.answer.set('GET /v1/community', (b) => {
      a.store.reset();
      return b;
    });
    await A.engine.start();
    expect(state(A).phase).toBe('idle');
    expect(communityOf(a.store)).toBeNull();

    const b = await hub.user();
    hub.answer.clear();
    const B = connect(hub, b);
    hub.answer.set('GET /v1/community/messages', () => {
      b.store.reset();
      throw new HubError('unavailable', 503);
    });
    await B.engine.start();
    expect(state(B).phase).toBe('idle');
    hub.answer.clear();
    const row = hub.insert(b.id, null);
    await B.engine.pullNew();
    await B.engine.markRead(5);
    await B.engine.remove(row.id);
    await B.engine.loadOlder();
    expect(state(B)).toMatchObject({ phase: 'idle', lastRead: 0 });
    frame(B, 'message');
    B.timers.advance(POLL_MS);
    await settle();
  });

  it('stops serving and polling after a sign-out', async () => {
    const { hub, b, B } = await withRequester();
    b.store.reset();
    hub.calls.length = 0;
    B.timers.advance(POLL_MS);
    await settle();
    expect(hub.calls).toEqual([]);
  });
});

describe('the client of a store', () => {
  it('is one per sign-in', async () => {
    const hub = new CommunityHub();
    const a = await hub.user();
    expect(communityOf(a.store)).toBeNull();
    const first = community({ store: a.store, client: a.client });
    expect(communityOf(a.store)).toBe(first);
    expect(community({ store: a.store, client: a.client })).toBe(first);
    a.store.reset();
    expect(communityOf(a.store)).toBeNull();
    expect(community({ store: a.store, client: a.client })).not.toBe(first);
  });
});
