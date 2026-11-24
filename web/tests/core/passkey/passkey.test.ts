// U-WEB-crypto (passkey): WebAuthn options from the hub JSON (contract fixtures), result serialisation, PRF extraction
// and the `agent-session-v1` statement (expected values computed with `agent_core.passkey.certificate`).
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import { b64uDecode, b64uEncode } from '../../../src/core/encoding/b64u';
import { fromUtf8 } from '../../../src/core/encoding/bytes';
import { prfInput } from '../../../src/core/crypto/kdf';
import { generateSessionKey } from '../../../src/core/crypto/keys';
import { creationOptions, requestOptions, TIMEOUT_MS } from '../../../src/core/passkey/options';
import { prfEnabled, prfOutput, requirePrf } from '../../../src/core/passkey/prf';
import { attestationJson, certificateJson } from '../../../src/core/passkey/response';
import {
  buildStatement,
  HUB_VALUE_BYTES,
  SESSION_ISSUE_SECONDS,
  SESSION_MAX_SECONDS,
  sessionExpiry,
  sessionKeyId,
  statementChallenge,
} from '../../../src/core/passkey/statement';
import { WEB_DIR } from '../../helpers';
import { codeOf } from '../crypto/helpers';
import { VECTOR_FILES } from '../crypto/vector-suite';

const fixture = (name: string) =>
  JSON.parse(readFileSync(join(WEB_DIR, '..', 'contracts', 'fixtures', 'api', name, 'valid', 'basic.json'), 'utf8'));
const REGISTER = fixture('passkeys-register-begin.response').options;
const LOGIN = fixture('passkeys-login-begin.response').options;
const bytes = (value: unknown) => new Uint8Array(value as ArrayBuffer);

describe('creation options', () => {
  it('converts the hub JSON and keeps only the values the client allows', async () => {
    const options = await creationOptions({
      ...REGISTER,
      attestation: 'none',
      authenticatorSelection: { ...REGISTER.authenticatorSelection, requireResidentKey: true },
      excludeCredentials: [
        { type: 'public-key', id: 'AQID' },
        { type: 'public-key', id: 'BAU', transports: ['internal', 'hybrid'] },
      ],
    });
    expect(options.rp).toEqual(REGISTER.rp);
    expect(b64uEncode(bytes(options.user.id))).toBe(REGISTER.user.id);
    expect([options.user.name, options.user.displayName]).toEqual([REGISTER.user.name, REGISTER.user.name]);
    expect(b64uEncode(bytes(options.challenge))).toBe(REGISTER.challenge);
    expect(options.pubKeyCredParams).toEqual([{ type: 'public-key', alg: -7 }]);
    expect(options.authenticatorSelection).toEqual({
      residentKey: 'required',
      requireResidentKey: true,
      userVerification: 'required',
    });
    expect(bytes(options.extensions!.prf!.eval!.first)).toEqual(await prfInput());
    expect([options.timeout, options.attestation]).toEqual([TIMEOUT_MS, 'none']);
    expect(options.excludeCredentials!.map((c) => [b64uEncode(bytes(c.id)), c.transports])).toEqual([
      ['AQID', undefined],
      ['BAU', ['internal', 'hybrid']],
    ]);
    expect((await creationOptions(REGISTER)).excludeCredentials).toEqual([]);
  });

  it('refuses options that weaken the ceremony or are malformed', async () => {
    const selection = REGISTER.authenticatorSelection;
    const bad = [
      null,
      { ...REGISTER, extra: 1 },
      { ...REGISTER, timeout: undefined },
      { ...REGISTER, user: { ...REGISTER.user, displayName: 'Alice' } },
      { ...REGISTER, user: { ...REGISTER.user, name: 'alice', displayName: 'alice' } },
      { ...REGISTER, user: { ...REGISTER.user, id: 'AQID' } },
      { ...REGISTER, user: { ...REGISTER.user, id: b64uEncode(new Uint8Array(16)) } },
      { ...REGISTER, challenge: 'AQID' },
      { ...REGISTER, pubKeyCredParams: [{ type: 'public-key', alg: -257 }] },
      { ...REGISTER, pubKeyCredParams: [] },
      {
        ...REGISTER,
        pubKeyCredParams: [
          { type: 'public-key', alg: -7 },
          { type: 'public-key', alg: -8 },
        ],
      },
      { ...REGISTER, authenticatorSelection: { ...selection, userVerification: 'preferred' } },
      { ...REGISTER, authenticatorSelection: { ...selection, residentKey: 'preferred' } },
      { ...REGISTER, authenticatorSelection: { ...selection, requireResidentKey: false } },
      { ...REGISTER, authenticatorSelection: { ...selection, authenticatorAttachment: 'platform' } },
      { ...REGISTER, authenticatorSelection: [] },
      { ...REGISTER, extensions: { prf: { eval: { first: b64uEncode(new Uint8Array(32)) } } } },
      { ...REGISTER, extensions: { prf: { eval: { first: REGISTER.extensions.prf.eval.first, second: 'AA' } } } },
      { ...REGISTER, timeout: 60000 },
      { ...REGISTER, attestation: 'direct' },
      { ...REGISTER, rp: { ...REGISTER.rp, id: '' } },
      { ...REGISTER, excludeCredentials: {} },
      { ...REGISTER, excludeCredentials: Array(65).fill({ type: 'public-key', id: 'AQ' }) },
      { ...REGISTER, excludeCredentials: [null] },
      { ...REGISTER, excludeCredentials: [{ type: 'password', id: 'AQ' }] },
      { ...REGISTER, excludeCredentials: [{ type: 'public-key', id: 'AQ', name: 'x' }] },
      { ...REGISTER, excludeCredentials: [{ type: 'public-key', id: '' }] },
      { ...REGISTER, excludeCredentials: [{ type: 'public-key', id: b64uEncode(new Uint8Array(1024)) }] },
      { ...REGISTER, excludeCredentials: [{ type: 'public-key', id: 'AQ', transports: ['bluetooth'] }] },
      { ...REGISTER, excludeCredentials: [{ type: 'public-key', id: 'AQ', transports: 'usb' }] },
    ];
    for (const json of bad) expect(await codeOf(() => creationOptions(json))).toBe('header_invalid');
  });
});

describe('request options', () => {
  const challenge = new Uint8Array(32).fill(7);

  it('uses the statement challenge and optionally one credential', async () => {
    const options = await requestOptions(LOGIN, challenge);
    expect(options.rpId).toBe(LOGIN.rpId);
    expect(bytes(options.challenge)).toEqual(challenge);
    expect([options.allowCredentials, options.userVerification, options.timeout]).toEqual([[], 'required', TIMEOUT_MS]);
    expect(bytes(options.extensions!.prf!.eval!.first)).toEqual(await prfInput());
    const one = await requestOptions(LOGIN, challenge, new Uint8Array([1, 2]));
    expect(one.allowCredentials!.map((c) => [c.type, bytes(c.id)])).toEqual([['public-key', new Uint8Array([1, 2])]]);
  });

  it('refuses malformed options, challenges and credential IDs', async () => {
    const bad = [
      { ...LOGIN, allowCredentials: [{ type: 'public-key', id: 'AQ' }] },
      { ...LOGIN, userVerification: 'discouraged' },
      { ...LOGIN, timeout: 1 },
      { ...LOGIN, rpId: 7 },
      { ...LOGIN, extensions: {} },
    ];
    for (const json of bad) expect(await codeOf(() => requestOptions(json, challenge))).toBe('header_invalid');
    expect(await codeOf(() => requestOptions(LOGIN, challenge.slice(1)))).toBe('header_invalid');
    expect(await codeOf(() => requestOptions(LOGIN, challenge, new Uint8Array(0)))).toBe('header_invalid');
  });
});

describe('results', () => {
  const buffer = (...values: number[]) => new Uint8Array(values).buffer;

  it('serialises the registration credential', () => {
    const json = attestationJson({
      rawId: buffer(1, 2, 3),
      response: {
        clientDataJSON: new Uint8Array([4]),
        attestationObject: buffer(5),
        getTransports: () => ['internal', 'hybrid', 'internal', 'cable'],
      },
    });
    expect(json).toEqual({
      id: 'AQID',
      clientDataJSON: 'BA',
      attestationObject: 'BQ',
      transports: ['internal', 'hybrid'],
    });
    const plain = attestationJson({
      rawId: buffer(1),
      response: { clientDataJSON: buffer(), attestationObject: buffer() },
    });
    expect(plain.transports).toEqual([]);
  });

  it('serialises the session certificate and refuses empty credential IDs', async () => {
    const assertion = {
      rawId: buffer(9),
      response: { clientDataJSON: buffer(1), authenticatorData: buffer(2), signature: new DataView(buffer(3)) },
    };
    expect(certificateJson(new Uint8Array([0x61]), assertion)).toEqual({
      statement: 'YQ',
      credential_id: 'CQ',
      authenticator_data: 'Ag',
      client_data_json: 'AQ',
      signature: 'Aw',
    });
    expect(await codeOf(() => certificateJson(new Uint8Array(1), { ...assertion, rawId: buffer() }))).toBe(
      'header_invalid',
    );
  });
});

describe('PRF', () => {
  it('extracts a 32-byte output as secret and zeroes the source', () => {
    const raw = new Uint8Array(40).fill(5);
    const view = new Uint8Array(raw.buffer, 4, 32);
    const secret = prfOutput({ prf: { results: { first: view } } })!;
    expect(secret.reveal((bytes) => bytes)).toEqual(new Uint8Array(32).fill(5));
    expect(view.every((byte) => byte === 0)).toBe(true);
    expect([raw[0], raw[39]]).toEqual([5, 5]);
    expect(requirePrf({ prf: { results: { first: new Uint8Array(32).buffer } } }).length).toBe(32);
  });

  it('reports absent outputs and refuses wrong lengths', async () => {
    expect(prfOutput({})).toBeNull();
    expect(prfOutput({ prf: { enabled: true } })).toBeNull();
    expect(await codeOf(() => requirePrf({ prf: {} }))).toBe('prf_missing');
    expect(await codeOf(() => prfOutput({ prf: { results: { first: new Uint8Array(16) } } }))).toBe('prf_length');
    expect(await codeOf(() => prfOutput({ prf: { results: { first: new Uint8Array(64).buffer } } }))).toBe(
      'prf_length',
    );
    expect([prfEnabled({}), prfEnabled({ prf: { enabled: false } })]).toEqual([false, false]);
    expect(prfEnabled({ prf: { enabled: true } })).toBe(true);
    expect(prfEnabled({ prf: { results: { first: new Uint8Array(32) } } })).toBe(true);
  });
});

describe('agent-session-v1 statement', () => {
  const input = {
    origin: 'https://agent.example.org',
    sessionPublicKey: b64uDecode(VECTOR_FILES.keys.p256.public_key),
    hubValue: new Uint8Array(HUB_VALUE_BYTES).fill(0x3a),
    expires: 1795003200,
  };

  it('reproduces agent_core.passkey.certificate byte for byte', async () => {
    const statement = await buildStatement(input);
    expect(fromUtf8(statement)).toBe(
      'agent-session-v1\nhttps://agent.example.org\n' +
        'BGltWeV2uxCLw1yPL7p0IC_Bl403Yc1jtYF94mn8ktwUawfUqPkAkZi8xHUN72UNKGPbxzkTMPEdR7rKLwsbL44\n' +
        'Ojo6Ojo6Ojo6Ojo6Ojo6Ojo6Ojo6Ojo6Ojo6Ojo6Ojo\n2026-11-18T12:00:00Z',
    );
    expect(b64uEncode(await statementChallenge(statement))).toBe('EMe6R5oFCHiUGXNJF5bOhBAGDlcyiT-bP00iHQoHb60');
    expect(await sessionKeyId(statement)).toBe('session.10c7ba479a050878941973491796ce84');
  });

  it('accepts a generated session key and refuses malformed inputs', async () => {
    const session = await generateSessionKey();
    expect(fromUtf8(await buildStatement({ ...input, sessionPublicKey: session.publicKey })).split('\n')).toHaveLength(
      5,
    );
    const bad = [
      { ...input, origin: '' },
      { ...input, origin: 'https://a\nb' },
      { ...input, sessionPublicKey: input.sessionPublicKey.slice(1) },
      { ...input, sessionPublicKey: Uint8Array.of(2, ...input.sessionPublicKey.slice(1)) },
      { ...input, hubValue: new Uint8Array(31) },
      { ...input, expires: 1.5 },
      { ...input, sessionPublicKey: Uint8Array.of(4, ...new Uint8Array(64)) },
    ];
    for (const value of bad) expect(await codeOf(() => buildStatement(value))).toBe('header_invalid');
  });

  it('expires 43080 s after the browser clock, inside the 12 h bound', () => {
    expect(sessionExpiry(1_795_003_200_999)).toBe(1795003200 + SESSION_ISSUE_SECONDS);
    expect(SESSION_ISSUE_SECONDS).toBeLessThan(SESSION_MAX_SECONDS);
  });
});
