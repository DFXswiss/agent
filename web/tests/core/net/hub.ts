// A fake hub for the signed-fetch tests: checks `agent-request-v1` exactly like `agent_hub.auth.gate` (text from what
// was received, P-256 raw signature under the session's public key) and signs answers with `agent-response-v1`
// under the fixture hub key (tests/fixtures/net/signing.json).
import { b64uDecode, b64uEncode } from '../../../src/core/encoding/b64u';
import { utf8 } from '../../../src/core/encoding/bytes';
import { formatSeconds } from '../../../src/core/encoding/time';
import {
  ed25519KeyPair,
  ed25519Sign,
  generateSessionKey,
  type KeyPair,
  p256VerifyRaw,
} from '../../../src/core/crypto/keys';
import { sha256 } from '../../../src/core/crypto/subtle';
import { requestText, responseText, sha256Hex } from '../../../src/core/net/responseCheck';
import type { Session } from '../../../src/core/net/signedFetch';
import fixture from '../../fixtures/net/signing.json';

export const HOST = 'hub.example:8443';
export const T0 = Date.UTC(2026, 10, 21, 12, 0, 0);

export const hubKeyPair = async (): Promise<KeyPair> =>
  ed25519KeyPair(await sha256(utf8('agent.dfx.swiss web-02 test hub key')));

export const HUB_KEY = { id: fixture.hub_key.id, publicKey: b64uDecode(fixture.hub_key.public_key) };

export interface Received {
  readonly method: string;
  readonly target: string;
  readonly headers: Record<string, string>;
  readonly body: Uint8Array;
}

export interface Answer {
  readonly status?: number;
  readonly body?: unknown;
  readonly raw?: Uint8Array;
  /** `false`: no signature headers; a function: change the headers after signing. */
  readonly sign?: boolean | ((headers: Headers) => void);
  readonly hubTime?: number;
}

export async function makeSession(expiresAt = T0 + 3600_000): Promise<Session & { publicKey: Uint8Array }> {
  const pair = await generateSessionKey();
  return {
    keyId: 'session.0123456789abcdef0123456789abcdef',
    privateKey: pair.privateKey,
    publicKey: pair.publicKey,
    expiresAt,
  };
}

/** A `fetch` that verifies the request and returns `answer(received)`. */
export async function fakeHub(
  sessionPublic: Uint8Array,
  answer: (received: Received) => Answer | Promise<Answer>,
  now: () => number = () => T0,
): Promise<{ fetch: typeof globalThis.fetch; requests: Received[] }> {
  const hub = await hubKeyPair();
  const requests: Received[] = [];
  const fetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const headers = Object.fromEntries(new Headers(init?.headers).entries());
    const body = init?.body === undefined ? new Uint8Array(0) : new Uint8Array(init.body as Uint8Array);
    const received = { method: init!.method!, target: String(input), headers, body };
    requests.push(received);
    const text = requestText(
      headers['agent-key']!,
      received.method,
      HOST,
      received.target,
      headers['agent-time']!,
      headers['agent-nonce']!,
      await sha256Hex(body),
    );
    await p256VerifyRaw(sessionPublic, b64uDecode(headers['agent-signature']), text);
    const result = await answer(received);
    const status = result.status ?? 200;
    const raw = result.raw ?? utf8(JSON.stringify(result.body ?? {}));
    const out = new Headers({ 'content-type': 'application/json' });
    if (result.sign !== false) {
      const hubTime = formatSeconds(Math.floor((result.hubTime ?? now()) / 1000));
      const sent = { keyId: headers['agent-key']!, nonce: headers['agent-nonce']!, agentTime: headers['agent-time']! };
      const signature = await ed25519Sign(hub.privateKey, responseText(sent, status, hubTime, await sha256Hex(raw)));
      out.set('agent-hub-key', HUB_KEY.id);
      out.set('agent-hub-time', hubTime);
      out.set('agent-hub-signature', b64uEncode(signature));
      if (typeof result.sign === 'function') result.sign(out);
    }
    return new Response(status === 204 ? null : (raw as Uint8Array<ArrayBuffer>), { status, headers: out });
  };
  return { fetch, requests };
}
