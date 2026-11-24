// Confirming a pairing (11-agent-access.md section 5.1): the fingerprint check against the hub's keys, the state, the
// confirmation body (identity for the agent with `r`, label for the user, claim hash) and the loopback form.
import { describe, expect, it } from 'vitest';
import {
  loopbackForm,
  lookupPairing,
  PAIRING_TTL_MS,
  pairingOutcome,
  preparePairing,
  REMOTE_PAIRING_TTL_MS,
  submitPairing,
} from '../../../src/core/agents/pairing';
import {
  agentCode,
  claim,
  claimHash,
  fingerprint,
  openHandover,
  openIdentity,
  openLabel,
  returnCode,
} from '../../../src/core/crypto/agent';
import type { AgentEntry } from '../../../src/core/store/slices/agents';
import { Identity } from '../../../src/core/crypto/keys';
import { b64uDecode, b64uEncode } from '../../../src/core/encoding/b64u';
import { HubError } from '../../../src/core/net/signedFetch';
import { codeOf } from '../crypto/helpers';
import { AGENT, USER } from './world';

const EXPIRES = '2026-11-23T12:10:00.000Z';
const ORIGIN = 'https://agent.example';
const ROOT = b64uEncode(new Uint8Array(32).fill(7));
const NONCE = 'yw1CyUW_rzOg-hZzWeorVQ';

async function setup(state = 'open') {
  const agent = await Identity.generate();
  const pub = agent.public();
  const link = {
    mode: 'loopback' as 'loopback' | 'remote',
    pairingId: AGENT,
    fingerprint: await fingerprint(agent.kemPublic(), agent.sigPublic()),
    port: 40123,
    name: 'Claude Code',
    host: 'Mac',
    message: null as string | null,
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
    const pairing = await lookupPairing(async (_m, target) => (targets.push(target), body), link, ORIGIN);
    expect(targets).toEqual([`/v1/agent-pairings/${AGENT}`]);
    expect(b64uEncode(pairing.kemPub)).toBe(agent.public().kem);
    expect(pairing.expiresAt - pairing.requestedAt).toBe(PAIRING_TTL_MS);
  });

  it('refuses swapped keys, a pairing that is not open and malformed answers', async () => {
    const { link, body } = await setup();
    const other = (await Identity.generate()).public();
    expect(await errorOf(() => lookupPairing(async () => ({ ...body, pub: other }), link, ORIGIN))).toBe(
      'pairing_mismatch',
    );
    expect(await errorOf(() => lookupPairing(async () => ({ ...body, state: 'confirmed' }), link, ORIGIN))).toBe(
      'pairing_unknown',
    );
    expect(await codeOf(() => lookupPairing(async () => null, link, ORIGIN))).toBe('header_invalid');
    expect(await codeOf(() => lookupPairing(async () => ({ ...body, pub: 'x' }), link, ORIGIN))).toBe('header_invalid');
    expect(await codeOf(() => lookupPairing(async () => ({ ...body, pairing_id: USER }), link, ORIGIN))).toBe(
      'header_invalid',
    );
  });
});

describe('preparePairing and submitPairing', () => {
  it('seals the identity for the agent with r, the label for the user and the claim hash', async () => {
    const { agent, link, body } = await setup();
    const user = await Identity.generate();
    const pairing = await lookupPairing(async () => body, link, ORIGIN);
    const prepared = await preparePairing(pairing, link, USER, user, ROOT);
    expect(prepared.returnCode).toBeNull();
    expect(prepared.body).not.toHaveProperty('handover_ct');
    expect(pairing.remote).toBe(false);
    expect(pairing.agentCode).toBeNull();
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

describe('a pairing from another computer (ADR-067)', () => {
  async function remote() {
    const world = await setup();
    const link = { ...world.link, mode: 'remote' as const, port: null };
    const body = { ...world.body, remote: true, nonce: NONCE };
    return { ...world, link, body };
  }

  it('computes the agent code from the link, the hub nonce and the page origin', async () => {
    const { link, body } = await remote();
    const pairing = await lookupPairing(async () => body, link, ORIGIN);
    expect(pairing.remote).toBe(true);
    expect(pairing.expiresAt - pairing.requestedAt).toBe(REMOTE_PAIRING_TTL_MS);
    expect(pairing.agentCode).toBe(await agentCode(AGENT, link.fingerprint, b64uDecode(NONCE), ORIGIN));
    const other = await lookupPairing(async () => body, link, 'https://other.example');
    expect(other.agentCode).not.toBe(pairing.agentCode);
  });

  it('refuses a mode that the link and the hub do not agree on, both ways, and a malformed nonce', async () => {
    const { link, body } = await remote();
    const loopback = await setup();
    expect(await errorOf(() => lookupPairing(async () => loopback.body, link, ORIGIN))).toBe('pairing_mismatch');
    expect(await errorOf(() => lookupPairing(async () => body, loopback.link, ORIGIN))).toBe('pairing_mismatch');
    expect(await codeOf(() => lookupPairing(async () => ({ ...body, remote: false }), link, ORIGIN))).toBe(
      'header_invalid',
    );
    expect(await codeOf(() => lookupPairing(async () => ({ ...body, nonce: 'AAAA' }), link, ORIGIN))).toBe(
      'header_invalid',
    );
    expect(await codeOf(() => lookupPairing(async () => ({ ...body, nonce: undefined }), link, ORIGIN))).toBe(
      'header_invalid',
    );
    expect(await errorOf(() => lookupPairing(async () => ({ ...body, state: 'confirmed' }), link, ORIGIN))).toBe(
      'pairing_unknown',
    );
  });

  it('seals the handover to the agent and derives the return code from the same r', async () => {
    const { agent, link, body } = await remote();
    const user = await Identity.generate();
    const pairing = await lookupPairing(async () => body, link, ORIGIN);
    const prepared = await preparePairing(pairing, link, USER, user, ROOT);
    const handover = await openHandover(agent.kem, AGENT, USER, prepared.body.handover_ct!);
    expect(handover.rootKey).toBe(ROOT);
    expect(b64uEncode(handover.secret)).toBe(prepared.secret.reveal((raw) => b64uEncode(raw)));
    expect(prepared.returnCode).toBe(await returnCode(handover.secret, AGENT, USER));
    expect(prepared.returnCode).toMatch(/^[0-9A-HJKMNP-TV-Z]{8}$/);
    const opened = await openIdentity(agent.kem, handover.secret, AGENT, USER, prepared.body.bundle_ct);
    expect(opened.public()).toEqual(user.public());
  });
});

describe('pairingOutcome', () => {
  const entry = (state: AgentEntry['state'], agentId = AGENT): AgentEntry => ({
    agentId,
    state,
    label: null,
    createdAt: 'x',
    confirmedAt: 'x',
    claimedAt: null,
    lastUsedAt: null,
    revokedAt: null,
    replaces: null,
    remote: true,
  });

  it('waits while confirmed, connects when active, fails otherwise', () => {
    expect(pairingOutcome(null, AGENT)).toBe('waiting');
    expect(pairingOutcome([entry('confirmed')], AGENT)).toBe('waiting');
    expect(pairingOutcome([entry('active')], AGENT)).toBe('connected');
    expect(pairingOutcome([entry('expired')], AGENT)).toBe('failed');
    expect(pairingOutcome([entry('revoked')], AGENT)).toBe('failed');
    expect(pairingOutcome([entry('active', USER)], AGENT)).toBe('failed');
  });
});
