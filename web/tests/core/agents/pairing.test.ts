// Confirming a pairing (11-agent-access.md section 5.1): the fingerprint check against the hub's keys, the state, the
// confirmation body (identity for the agent with `r`, label for the user, claim hash) and the loopback form.
import { describe, expect, it } from 'vitest';
import {
  loopbackForm,
  lookupPairing,
  PAIRING_TTL_MS,
  preparePairing,
  submitPairing,
} from '../../../src/core/agents/pairing';
import { claim, claimHash, fingerprint, openIdentity, openLabel } from '../../../src/core/crypto/agent';
import { Identity } from '../../../src/core/crypto/keys';
import { b64uDecode, b64uEncode } from '../../../src/core/encoding/b64u';
import { HubError } from '../../../src/core/net/signedFetch';
import { codeOf } from '../crypto/helpers';
import { AGENT, USER } from './world';

const EXPIRES = '2026-11-23T12:10:00.000Z';

async function setup(state = 'open') {
  const agent = await Identity.generate();
  const pub = agent.public();
  const link = {
    pairingId: AGENT,
    fingerprint: await fingerprint(agent.kemPublic(), agent.sigPublic()),
    port: 40123,
    name: 'Claude Code',
    host: 'Mac',
  };
  const body = { pairing_id: AGENT, pub, state, expires_at: EXPIRES };
  return { agent, link, body };
}

const errorOf = async (run: () => Promise<unknown>) => {
  try {
    await run();
  } catch (error) {
    return error instanceof HubError ? error.code : (error as Error).message;
  }
  return 'none';
};

describe('lookupPairing', () => {
  it('returns the keys and times of an open pairing whose keys match the link', async () => {
    const { agent, link, body } = await setup();
    const targets: string[] = [];
    const pairing = await lookupPairing(async (_m, target) => (targets.push(target), body), link);
    expect(targets).toEqual([`/v1/agent-pairings/${AGENT}`]);
    expect(b64uEncode(pairing.kemPub)).toBe(agent.public().kem);
    expect(pairing.expiresAt - pairing.requestedAt).toBe(PAIRING_TTL_MS);
  });

  it('refuses swapped keys, a pairing that is not open and malformed answers', async () => {
    const { link, body } = await setup();
    const other = (await Identity.generate()).public();
    expect(await errorOf(() => lookupPairing(async () => ({ ...body, pub: other }), link))).toBe('pairing_mismatch');
    expect(await errorOf(() => lookupPairing(async () => ({ ...body, state: 'confirmed' }), link))).toBe(
      'pairing_unknown',
    );
    expect(await codeOf(() => lookupPairing(async () => null, link))).toBe('header_invalid');
    expect(await codeOf(() => lookupPairing(async () => ({ ...body, pub: 'x' }), link))).toBe('header_invalid');
    expect(await codeOf(() => lookupPairing(async () => ({ ...body, pairing_id: USER }), link))).toBe('header_invalid');
  });
});

describe('preparePairing and submitPairing', () => {
  it('seals the identity for the agent with r, the label for the user and the claim hash', async () => {
    const { agent, link, body } = await setup();
    const user = await Identity.generate();
    const pairing = await lookupPairing(async () => body, link);
    const prepared = await preparePairing(pairing, link, USER, user);
    expect(prepared.body.pairing_id).toBe(AGENT);
    const opened = await openIdentity(agent.kem, prepared.secret, AGENT, USER, prepared.body.bundle_ct);
    expect(opened.public()).toEqual(user.public());
    expect(await openLabel(user.kem, AGENT, USER, prepared.body.label_ct)).toEqual({
      host: 'Mac',
      name: 'Claude Code',
    });
    const expected = b64uEncode(await claimHash(await claim(prepared.secret, AGENT, USER)));
    expect(prepared.body.claim_hash).toBe(expected);
    const sent: unknown[] = [];
    await submitPairing(
      { request: async (_m, _t, options) => (sent.push(options?.body), { agent_id: AGENT }) },
      prepared,
    );
    expect(sent).toEqual([prepared.body]);
    expect(await codeOf(() => submitPairing({ request: async () => ({ agent_id: USER }) }, prepared))).toBe(
      'header_invalid',
    );
    expect(await codeOf(() => submitPairing({ request: async () => null }, prepared))).toBe('header_invalid');
    const form = loopbackForm(40123, prepared, 'ROOT');
    expect(form.action).toBe('http://127.0.0.1:40123/agent-pairing');
    expect(form.fields.map(([name]) => name)).toEqual(['p', 'u', 'r', 'k']);
    expect(form.fields[0]![1]).toBe(AGENT);
    expect(form.fields[1]![1]).toBe(USER);
    expect(form.fields[3]![1]).toBe('ROOT');
    expect(b64uDecode(form.fields[2]![1])).toEqual(prepared.secret.reveal((raw) => raw));
  });
});
