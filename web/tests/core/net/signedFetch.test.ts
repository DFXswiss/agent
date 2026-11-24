// The signed client against the fake hub: request signatures, answer verification (MUST), error mapping onto the
// catalog, the session notices and the stream request.
import { describe, expect, it } from 'vitest';
import { fromUtf8, utf8 } from '../../../src/core/encoding/bytes';
import {
  NONCE_BYTES,
  RENEW_BEFORE_MS,
  errorOf,
  hostOf,
  HubError,
  parseBody,
  SignedClient,
  type SessionNotice,
  unsignedAllowed,
} from '../../../src/core/net/signedFetch';
import { type Answer, fakeHub, HOST, HUB_KEY, makeSession, type Received, T0 } from './hub';
import { b64uDecode } from '../../../src/core/encoding/b64u';

async function setup(answer: (received: Received) => Answer | Promise<Answer>, expiresAt = T0 + 3600_000) {
  const session = await makeSession(expiresAt);
  const hub = await fakeHub(session.publicKey, answer);
  const notices: SessionNotice[] = [];
  let current: typeof session | null = session;
  const client = new SignedClient({
    fetch: hub.fetch,
    host: HOST,
    hubKey: HUB_KEY,
    session: () => current,
    now: () => T0,
    onSession: (notice) => notices.push(notice),
  });
  return { client, hub, notices, signOut: () => (current = null) };
}

async function failure(run: () => Promise<unknown>): Promise<HubError> {
  try {
    await run();
  } catch (error) {
    if (error instanceof HubError) return error;
    throw error;
  }
  throw new Error('no failure');
}

describe('signed requests', () => {
  it('signs method, host, target, time, nonce and body hash; returns the verified body', async () => {
    const { client, hub, notices } = await setup((r) => ({ body: { echo: fromUtf8(r.body), target: r.target } }));
    const result = await client.request('POST', '/v1/events?x=1', {
      body: { events: [] },
      headers: { 'x-extra': '1' },
    });
    expect(result).toEqual({ echo: '{"events":[]}', target: '/v1/events?x=1' });
    const sent = hub.requests[0]!;
    expect(sent.headers['agent-key']).toBe('session.0123456789abcdef0123456789abcdef');
    expect(sent.headers['agent-time']).toBe('2026-11-21T12:00:00Z');
    expect(b64uDecode(sent.headers['agent-nonce'])).toHaveLength(NONCE_BYTES);
    expect(sent.headers['content-type']).toBe('application/json');
    expect(sent.headers.accept).toBe('application/json');
    expect(sent.headers['x-extra']).toBe('1');
    expect(notices).toEqual([]);
  });

  it('signs an empty body for GET and uses a fresh nonce per request', async () => {
    const { client, hub } = await setup(() => ({ body: { ok: true } }));
    await client.request('GET', '/v1/me');
    await client.request('GET', '/v1/me');
    expect(hub.requests[0]!.body).toHaveLength(0);
    expect(hub.requests[0]!.headers['content-type']).toBeUndefined();
    expect(hub.requests[0]!.headers['agent-nonce']).not.toBe(hub.requests[1]!.headers['agent-nonce']);
  });

  it('derives the host line from the origin', () => {
    expect(hostOf('https://hub.example')).toBe('hub.example');
    expect(hostOf('https://hub.example:443')).toBe('hub.example');
    expect(hostOf('http://agent.localhost:20700')).toBe('agent.localhost:20700');
  });
});

describe('answer verification', () => {
  it('refuses an unsigned success', async () => {
    const { client } = await setup(() => ({ body: { ok: true }, sign: false }));
    expect((await failure(() => client.request('GET', '/v1/me'))).code).toBe('signature_invalid');
  });

  it('refuses unsigned errors the hub always signs (403, 404, 409)', async () => {
    for (const status of [403, 404, 409]) {
      const { client } = await setup(() => ({ status, body: { error: 'not_found' }, sign: false }));
      expect((await failure(() => client.request('GET', '/v1/scopes/x/events'))).code).toBe('signature_invalid');
    }
    expect([400, 401, 413, 429, 500, 502, 200, 404].map(unsignedAllowed)).toEqual([
      true,
      true,
      true,
      true,
      true,
      true,
      false,
      false,
    ]);
  });

  it('refuses a forged or tampered answer', async () => {
    const { client } = await setup(() => ({
      body: { ok: true },
      sign: (h) => h.set('agent-hub-signature', 'A'.repeat(86)),
    }));
    const error = await failure(() => client.request('GET', '/v1/me'));
    expect([error.code, error.status]).toEqual(['signature_invalid', 200]);
  });

  it('refuses a signed error whose signature does not verify', async () => {
    const { client } = await setup(() => ({
      status: 403,
      body: { error: 'forbidden' },
      sign: (h) => h.set('agent-hub-key', 'x'),
    }));
    expect((await failure(() => client.request('GET', '/v1/me'))).code).toBe('signature_invalid');
  });

  it('refuses an answer with a hub time outside the window (expired)', async () => {
    const { client } = await setup(() => ({ body: {}, hubTime: T0 - 10 * 60_000 }));
    expect((await failure(() => client.request('GET', '/v1/me'))).code).toBe('time_skew');
  });

  it('returns null for a signed success without a JSON body', async () => {
    const { client } = await setup(() => ({ raw: utf8('') }));
    expect(await client.request('POST', '/v1/session/end', { body: {} })).toBeNull();
  });
});

describe('error mapping', () => {
  it('maps signed and unsigned errors onto the catalog', async () => {
    const answers: Answer[] = [
      { status: 403, body: { error: 'last_admin' } },
      { status: 409, body: { error: 'seq_conflict', detail: { expected_seq: 17 } } },
      { status: 401, body: { error: 'nonce_replayed' }, sign: false },
      { status: 429, body: { error: 'rate_limited' }, sign: false },
      { status: 400, body: { error: 'no_such_code' } },
      { status: 502, raw: utf8('<html>'), sign: false },
    ];
    let index = 0;
    const { client } = await setup(() => answers[index++]!);
    const codes = [];
    for (let i = 0; i < answers.length; i += 1) {
      const error = await failure(() => client.request('GET', '/v1/x'));
      codes.push([error.code, error.status, error.detail]);
    }
    expect(codes).toEqual([
      ['last_admin', 403, null],
      ['seq_conflict', 409, { expected_seq: 17 }],
      ['nonce_replayed', 401, null],
      ['rate_limited', 429, null],
      ['request_invalid', 400, null],
      ['unavailable', 502, null],
    ]);
  });

  it('maps statuses without a known code', () => {
    const code = (status: number, body: unknown = null) => errorOf(status, body).code;
    expect([401, 403, 404, 409, 413, 429, 500, 503, 0, 400, 418].map((s) => code(s))).toEqual([
      'unauthorized',
      'forbidden',
      'not_found',
      'id_conflict',
      'size_exceeded',
      'rate_limited',
      'unavailable',
      'unavailable',
      'unavailable',
      'request_invalid',
      'request_invalid',
    ]);
    expect(code(404, { error: 7 })).toBe('not_found');
    expect(code(404, 'text')).toBe('not_found');
    expect(parseBody(utf8('{"a":1}'))).toEqual({ a: 1 });
    expect(parseBody(utf8('{'))).toBeNull();
  });

  it('maps a network failure to unavailable and passes an abort through', async () => {
    const session = await makeSession();
    const failing = new SignedClient({
      fetch: () => Promise.reject(new TypeError('network')),
      host: HOST,
      hubKey: HUB_KEY,
      session: () => session,
      now: () => T0,
    });
    expect((await failure(() => failing.request('GET', '/v1/me'))).code).toBe('unavailable');
    const abort = new AbortController();
    abort.abort();
    const aborting = new SignedClient({
      fetch: () => Promise.reject(new DOMException('aborted', 'AbortError')),
      host: HOST,
      hubKey: HUB_KEY,
      session: () => session,
    });
    await expect(aborting.request('GET', '/v1/me', { signal: abort.signal })).rejects.toThrow('aborted');
  });

  it('maps a failed body read to unavailable and passes an abort through', async () => {
    const session = await makeSession();
    const broken = () =>
      Promise.resolve({
        ok: true,
        status: 200,
        headers: new Headers(),
        arrayBuffer: () => Promise.reject(new DOMException('aborted', 'AbortError')),
      } as unknown as Response);
    const client = new SignedClient({
      fetch: broken,
      host: HOST,
      hubKey: HUB_KEY,
      session: () => session,
      now: () => T0,
    });
    expect((await failure(() => client.request('GET', '/v1/me'))).code).toBe('unavailable');
    const abort = new AbortController();
    abort.abort();
    await expect(client.request('GET', '/v1/me', { signal: abort.signal })).rejects.toThrow('aborted');
  });
});

describe('session notices', () => {
  it('reports expiring less than five minutes before expiry', async () => {
    const early = await setup(() => ({ body: {} }), T0 + RENEW_BEFORE_MS);
    await early.client.request('GET', '/v1/me');
    expect(early.notices).toEqual([]);
    const late = await setup(() => ({ body: {} }), T0 + RENEW_BEFORE_MS - 1);
    await late.client.request('GET', '/v1/me');
    expect(late.notices).toEqual(['expiring']);
  });

  it('reports expired on 401 session_expired (signed or not)', async () => {
    const { client, notices } = await setup(() => ({ status: 401, body: { error: 'session_expired' }, sign: false }));
    expect((await failure(() => client.request('GET', '/v1/me'))).code).toBe('session_expired');
    expect(notices).toEqual(['expired']);
  });

  it('refuses to send without a session', async () => {
    const { client, hub, signOut } = await setup(() => ({ body: {} }));
    signOut();
    expect((await failure(() => client.request('GET', '/v1/me'))).code).toBe('unauthorized');
    expect(hub.requests).toEqual([]);
  });

  it('works without a notice listener', async () => {
    const session = await makeSession(T0);
    const hub = await fakeHub(session.publicKey, () => ({ status: 401, body: { error: 'session_expired' } }));
    const client = new SignedClient({
      fetch: hub.fetch,
      host: HOST,
      hubKey: HUB_KEY,
      session: () => session,
      now: () => T0,
    });
    expect((await failure(() => client.request('GET', '/v1/me'))).code).toBe('session_expired');
  });
});

describe('stream requests', () => {
  it('signs the request and returns the unverified streaming response', async () => {
    const { client, hub } = await setup(() => ({ raw: utf8(': hi\n\n'), sign: false }));
    const response = await client.stream('/v1/stream?org=o', new AbortController().signal);
    expect(await response.text()).toBe(': hi\n\n');
    expect(hub.requests[0]!.headers.accept).toBe('text/event-stream');
  });

  it('maps a refused stream onto the catalog', async () => {
    const { client, notices } = await setup(() => ({ status: 401, body: { error: 'session_expired' }, sign: false }));
    expect((await failure(() => client.stream('/v1/stream?org=o', new AbortController().signal))).code).toBe(
      'session_expired',
    );
    expect(notices).toEqual(['expired']);
    const session = await makeSession();
    const broken = () =>
      Promise.resolve({
        ok: false,
        status: 503,
        arrayBuffer: () => Promise.reject(new Error('x')),
      } as unknown as Response);
    const other = new SignedClient({
      fetch: broken,
      host: HOST,
      hubKey: HUB_KEY,
      session: () => session,
      now: () => T0,
    });
    expect((await failure(() => other.stream('/v1/stream', new AbortController().signal))).code).toBe('unavailable');
  });
});
