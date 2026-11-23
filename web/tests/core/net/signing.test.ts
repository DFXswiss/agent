// agent-request-v1 / agent-response-v1 against texts and a signature produced by `agent_core.signing`
// (tests/fixtures/net/signing.json) and the legacy passkey vectors (tests/vectors/passkey.json of the repository).
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import { b64uDecode } from '../../../src/core/encoding/b64u';
import { fromUtf8, utf8 } from '../../../src/core/encoding/bytes';
import { p256VerifyRaw } from '../../../src/core/crypto/keys';
import {
  checkResponse,
  requestText,
  responseText,
  sha256Hex,
  WINDOW_SECONDS,
} from '../../../src/core/net/responseCheck';
import { WEB_DIR } from '../../helpers';
import fixture from '../../fixtures/net/signing.json';
import { HUB_KEY } from './hub';

const passkey = JSON.parse(readFileSync(join(WEB_DIR, '..', 'tests', 'vectors', 'passkey.json'), 'utf8')) as {
  request_texts: {
    key_id: string;
    method: string;
    host: string;
    target: string;
    time: string;
    nonce: string;
    body_hash: string;
    text: string;
  }[];
  session_signatures: { name: string; public_key: string; text: string; signature: string; result: string }[];
};

const req = fixture.request;
const sent = { keyId: req.key_id, nonce: req.nonce, agentTime: req.agent_time };
const T = Date.UTC(2026, 10, 21, 12, 0, 1);

function headers(values: Record<string, string>): Headers {
  return new Headers(values);
}

const signed = (response: typeof fixture.response, extra: Record<string, string> = {}) =>
  headers({
    'agent-hub-key': fixture.hub_key.id,
    'agent-hub-time': response.hub_time,
    'agent-hub-signature': response.signature,
    ...extra,
  });

describe('agent-request-v1', () => {
  it('matches agent_core.signing.request_text', async () => {
    expect(await sha256Hex(utf8(req.body))).toBe(req.body_hash);
    const text = requestText(req.key_id, req.method, req.host, req.target, req.agent_time, req.nonce, req.body_hash);
    expect(fromUtf8(text)).toBe(req.text);
  });

  it('matches the passkey request-text vectors', () => {
    for (const v of passkey.request_texts) {
      expect(fromUtf8(requestText(v.key_id, v.method, v.host, v.target, v.time, v.nonce, v.body_hash))).toBe(v.text);
    }
  });

  it('verifies the session signatures of the passkey vectors (P-256 raw r||s)', async () => {
    for (const v of passkey.session_signatures) {
      let result = 'ok';
      try {
        await p256VerifyRaw(b64uDecode(v.public_key), b64uDecode(v.signature), utf8(v.text));
      } catch {
        result = 'signature_invalid';
      }
      // the hub refuses a wrongly sized signature earlier (`signature_missing`); here only accepted vs. refused counts
      expect([v.name, result === 'ok']).toEqual([v.name, v.result === 'ok']);
    }
  });
});

describe('agent-response-v1', () => {
  it('matches agent_core.signing.response_text', async () => {
    for (const response of [fixture.response, fixture.error_response]) {
      const text = responseText(sent, response.status, response.hub_time, await sha256Hex(utf8(response.body)));
      expect(fromUtf8(text)).toBe(response.text);
    }
  });

  it('accepts the hub signature of agent_core.signing.HubSigner', async () => {
    for (const response of [fixture.response, fixture.error_response]) {
      expect(await checkResponse(HUB_KEY, sent, response.status, signed(response), utf8(response.body), T)).toBeNull();
    }
  });

  it('refuses forged answers', async () => {
    const r = fixture.response;
    const body = utf8(r.body);
    const check = (h: Headers, status = r.status, raw = body) => checkResponse(HUB_KEY, sent, status, h, raw, T);
    expect(await check(signed(r), 201)).toBe('signature_invalid');
    expect(await check(signed(r), r.status, utf8('{"results":[1]}'))).toBe('signature_invalid');
    expect(await check(signed(r, { 'agent-hub-key': 'other-key' }))).toBe('signature_invalid');
    expect(await check(signed(r, { 'agent-hub-signature': fixture.error_response.signature }))).toBe(
      'signature_invalid',
    );
    expect(await check(signed(r, { 'agent-hub-signature': 'not b64u!' }))).toBe('signature_invalid');
    expect(await check(signed(r, { 'agent-hub-time': '2026-11-21T12:00:01.000Z' }))).toBe('signature_invalid');
    expect(await check(signed(r, { 'agent-hub-time': '2026-11-21T12:00:02Z' }))).toBe('signature_invalid');
    expect(
      await checkResponse(HUB_KEY, { ...sent, nonce: 'AAECAwQFBgcICQoLDA0OEA' }, r.status, signed(r), body, T),
    ).toBe('signature_invalid');
    expect(await check(headers({ 'agent-hub-key': HUB_KEY.id }))).toBe('signature_invalid');
    expect(await check(headers({ 'agent-hub-key': HUB_KEY.id, 'agent-hub-time': r.hub_time }))).toBe(
      'signature_invalid',
    );
    expect(await check(headers({ 'agent-hub-signature': r.signature }))).toBe('signature_invalid');
    expect(await check(headers({}))).toBe('unsigned');
  });

  it('refuses an answer whose hub time is outside the window (replayed or expired)', async () => {
    const r = fixture.response;
    const at = (ms: number) => checkResponse(HUB_KEY, sent, r.status, signed(r), utf8(r.body), ms);
    expect(await at(T + WINDOW_SECONDS * 1000)).toBeNull();
    expect(await at(T - WINDOW_SECONDS * 1000)).toBeNull();
    expect(await at(T + (WINDOW_SECONDS + 1) * 1000)).toBe('time_skew');
    expect(await at(T - (WINDOW_SECONDS + 1) * 1000)).toBe('time_skew');
  });
});
