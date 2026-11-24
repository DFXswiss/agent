// Confirming a pairing (11-agent-access.md section 5.1, ADR-082 sections 2 and 9): the fingerprint check against the
// hub's keys, the state, the generation of the signed lookup, the confirmation body (certificate, the user's public
// keys sealed for the agent with `r`, label for the user, claim hash; never the identity), the batches of every org
// (the agent added with every held epoch, a replaced orchestrator removed with its rotations), the `CK` wraps, the
// retry after `seq_conflict`, the credential-key rotation after a replacement, and the loopback form.
import { describe, expect, it } from 'vitest';
import {
  confirmationBatches,
  confirmPairing,
  loopbackForm,
  lookupPairing,
  PAIRING_TTL_MS,
  pairingGen,
  pairingOutcome,
  preparePairing,
  profileOrgs,
  REMOTE_PAIRING_TTL_MS,
} from '../../../src/core/agents/pairing';
import {
  agentCode,
  claim,
  claimHash,
  fingerprint,
  openHandover,
  openLabel,
  openUser,
  returnCode,
  verifyCert,
} from '../../../src/core/crypto/agent';
import { BATCH_ATTEMPTS } from '../../../src/core/org/batch';
import { Prefs } from '../../../src/core/account/prefs';
import { AGENT as AGENT_KIND, epochKey } from '../../../src/core/log/state';
import { setProfile } from '../../../src/core/store/slices/me';
import { blockOrg, orgEntry } from '../../../src/core/store/slices/orgs';
import { actor, type Actor } from '../log/world';
import type { AgentEntry } from '../../../src/core/store/slices/agents';
import { Identity } from '../../../src/core/crypto/keys';
import { b64uDecode, b64uEncode } from '../../../src/core/encoding/b64u';
import { HubError } from '../../../src/core/net/signedFetch';
import { codeOf } from '../crypto/helpers';
import { AGENT, agentWorld, confirmWorld, linkOf, pairingOf, USER } from './world';

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

describe('pairingGen', () => {
  it('reads the generation the signed lookup names and refuses an answer without one', async () => {
    const targets: string[] = [];
    const client = (answer: unknown) => ({
      request: async (_method: string, target: string) => (targets.push(target), answer),
    });
    expect(await pairingGen(client({ pairing_id: AGENT, gen: 3 }), AGENT)).toBe(3);
    expect(targets).toEqual([`/v1/agent-pairings/${AGENT}`]);
    expect(await codeOf(() => pairingGen(client({ pairing_id: AGENT }), AGENT))).toBe('header_invalid');
    expect(await codeOf(() => pairingGen(client({ pairing_id: AGENT, gen: 0 }), AGENT))).toBe('header_invalid');
    expect(await codeOf(() => pairingGen(client({ pairing_id: USER, gen: 1 }), AGENT))).toBe('header_invalid');
    expect(await codeOf(() => pairingGen(client(null), AGENT))).toBe('header_invalid');
  });
});

describe('preparePairing', () => {
  it('signs the certificate, seals the user anchor for the agent with r, the label for the user and the claim hash', async () => {
    const { agent, link, body } = await setup();
    const user = await Identity.generate();
    const pairing = await lookupPairing(async () => body, link, ORIGIN);
    const prepared = await preparePairing(pairing, link, USER, user, ROOT, 2);
    expect(prepared.returnCode).toBeNull();
    expect(prepared.body).not.toHaveProperty('handover_ct');
    expect(prepared.body).not.toHaveProperty('bundle_ct');
    expect(pairing.remote).toBe(false);
    expect(pairing.agentCode).toBeNull();
    expect(prepared.body.pairing_id).toBe(AGENT);
    expect(prepared.body.gen).toBe(2);
    expect(prepared.agent).toEqual({ id: AGENT, pub: agent.public(), gen: 2, cert: prepared.body.cert });
    await verifyCert(user.sigPublic(), prepared.body.cert, { agent: AGENT, pub: agent.public(), user: USER, gen: 2 });
    const anchor = await openUser(agent.kem, prepared.secret, AGENT, USER, prepared.body.user_ct);
    expect(anchor).toEqual({ ...user.public(), cert: prepared.body.cert });
    expect(await openLabel(user.kem, AGENT, USER, prepared.body.label_ct)).toEqual({
      host: 'Mac',
      name: 'Claude Code',
    });
    const expected = b64uEncode(await claimHash(await claim(prepared.secret, AGENT, USER)));
    expect(prepared.body.claim_hash).toBe(expected);
    const form = loopbackForm(40123, prepared, 'ROOT');
    expect(form.action).toBe('http://127.0.0.1:40123/agent-pairing');
    expect(form.fields.map(([name]) => name)).toEqual(['p', 'u', 'r', 'k']);
    expect(form.fields[0]![1]).toBe(AGENT);
    expect(form.fields[1]![1]).toBe(USER);
    expect(form.fields[3]![1]).toBe('ROOT');
    expect(b64uDecode(form.fields[2]![1])).toEqual(prepared.secret.reveal((raw) => raw));
  });
});

describe('confirmPairing', () => {
  async function prepare(t: Awaited<ReturnType<typeof confirmWorld>>, agent: Actor, gen: number) {
    return preparePairing(pairingOf(agent), linkOf(agent), t.me.id, t.me.identity, ROOT, gen);
  }

  it('adds the agent to every org with every held epoch and wraps the credential key for it', async () => {
    const t = await confirmWorld();
    const bot = await actor('first bot');
    t.world.add(bot);
    expect(profileOrgs(t.store)).toEqual([t.org]);
    const confirmed: [string, string, string][] = [];
    const prefs = {
      addConfirmedAgent: (...args: [string, string, string]) => void confirmed.push(args),
      handledLosses: () => [],
      setHandledLosses: () => undefined,
    };
    const prepared = await prepare(t, bot, 1);
    await confirmPairing({ ...t.context, prefs }, prepared);
    expect(confirmed).toEqual([[t.me.id, bot.id, prepared.body.cert]]);
    const body = t.confirm.bodies[0]!;
    expect(Object.keys(body).sort()).toEqual(
      ['batches', 'cert', 'claim_hash', 'cred_wraps', 'gen', 'label_ct', 'pairing_id', 'user_ct'].sort(),
    );
    const batches = body.batches as { org: string; entries: { body: Record<string, unknown> }[] }[];
    expect(batches.map((batch) => [batch.org, batch.entries.map((entry) => entry.body.type)])).toEqual([
      [t.org, ['agent_add']],
    ]);
    const state = orgEntry(t.store.getState().orgs, t.org).log!.state;
    expect(state.member(bot.id)).toMatchObject({ kind: AGENT_KIND, parent: t.me.id, gen: 1 });
    expect(t.world.currentKey(bot.id, 'org', t.org)).toBe(t.world.currentKey(t.me.id, 'org', t.org));
    expect(
      (body.cred_wraps as { recipient_id: string; epoch: number }[]).map((w) => [w.recipient_id, w.epoch]),
    ).toEqual([[bot.id, 1]]);
    // nothing was lost: no credential-key rotation
    expect(t.hub.rotations).toEqual([]);
  });

  it('replaces the previous orchestrator in the same batch with its rotations, keeps the new keys, rotates CK', async () => {
    const t = await confirmWorld();
    const [first, second] = [await actor('first bot'), await actor('second bot')];
    t.world.add(first, second);
    const items = new Map<string, string>();
    const prefs = new Prefs({
      getItem: (key) => items.get(key) ?? null,
      setItem: (key, value) => void items.set(key, value),
      removeItem: (key) => void items.delete(key),
    });
    await confirmPairing({ ...t.context, prefs }, await prepare(t, first, 1));
    await confirmPairing({ ...t.context, prefs }, await prepare(t, second, 2));
    const batches = t.confirm.bodies[1]!.batches as { entries: { body: Record<string, unknown> }[] }[];
    expect(batches[0]!.entries.map((entry) => entry.body.type)).toEqual(['agent_add', 'rotate', 'rotate']);
    // the CK rotation for the replaced orchestrator is remembered: a reload does not rotate again
    expect(prefs.handledLosses(t.me.id)).toEqual([expect.stringMatching(new RegExp(`^${first.id} `))]);
    expect(Object.keys(prefs.confirmedAgents(t.me.id)).sort()).toEqual([first.id, second.id].sort());
    const entry = orgEntry(t.store.getState().orgs, t.org);
    const state = entry.log!.state;
    expect(state.member(first.id)).toBeNull();
    expect(state.activeAgent(t.me.id)!.id).toBe(second.id);
    expect(entry.keys.has(epochKey('org', t.org, state.epoch('org', t.org)))).toBe(true);
    expect(state.epoch('org', t.org)).toBe(2);
    // the replaced orchestrator lost the credential key: CK_2 for the user, the device and the new agent only
    expect(t.hub.rotations.map((rotation) => rotation.epoch)).toEqual([2]);
    const recipients = t.hub.rotations[0]!.wraps.map((wrap) => wrap.recipient_id).sort();
    expect(recipients).toEqual([t.me.id, t.device.id, second.id].sort());
  });

  it('lets a member without admin right replace its orchestrator with the rotations it owes', async () => {
    const t = await confirmWorld({ member: true });
    expect(t.world.state.isAdmin(t.me.id)).toBe(false);
    const [first, second] = [await actor('first bot'), await actor('second bot')];
    t.world.add(first, second);
    await confirmPairing(t.context, await prepare(t, first, 1));
    await confirmPairing(t.context, await prepare(t, second, 2));
    const batches = t.confirm.bodies[1]!.batches as { entries: { body: Record<string, unknown> }[] }[];
    expect(batches[0]!.entries.map((entry) => [entry.body.type, entry.body.by])).toEqual([
      ['agent_add', t.me.id],
      ['rotate', t.me.id],
      ['rotate', t.me.id],
    ]);
    const state = orgEntry(t.store.getState().orgs, t.org).log!.state;
    expect(state.member(first.id)).toBeNull();
    expect(state.agentOwed.size).toBe(0);
  });

  it('rebuilds after seq_conflict, gives up after the last attempt and passes other refusals on', async () => {
    const t = await confirmWorld();
    const bot = await actor('first bot');
    t.world.add(bot);
    const prepared = await prepare(t, bot, 1);
    t.confirm.before.push(new HubError('seq_conflict', 409));
    await confirmPairing(t.context, prepared);
    expect(t.confirm.bodies).toHaveLength(2);
    expect(t.context.refreshLog).toHaveBeenCalledWith(t.org);
    // the orgs or the CK epoch changed meanwhile, or the hub no longer takes the CK epoch: read again, rebuild
    for (const code of ['agent_batches_incomplete', 'epoch_stale']) {
      const changed = await confirmWorld();
      changed.world.add(bot);
      changed.confirm.before.push(new HubError(code, 409));
      await confirmPairing(changed.context, prepared);
      expect(changed.confirm.bodies).toHaveLength(2);
      // the log job of every org ran before each attempt and once more after the confirmation
      expect(changed.context.refreshLog).toHaveBeenCalledTimes(3);
    }
    const other = await confirmWorld();
    for (let i = 0; i < BATCH_ATTEMPTS; i += 1) other.confirm.before.push(new HubError('seq_conflict', 409));
    expect(await errorOf(() => confirmPairing(other.context, prepared))).toBe('seq_conflict');
    expect(other.confirm.bodies).toHaveLength(BATCH_ATTEMPTS);
    other.confirm.before.push(new HubError('client_outdated', 409));
    expect(await errorOf(() => confirmPairing(other.context, prepared))).toBe('client_outdated');
    other.confirm.before.push(() => ({ agent_id: USER }));
    expect(await codeOf(() => confirmPairing(other.context, prepared))).toBe('header_invalid');
    other.confirm.before.push(() => null);
    expect(await codeOf(() => confirmPairing(other.context, prepared))).toBe('header_invalid');
  });

  it('keeps no keys of a batch the hub did not apply (a repeat answered alike)', async () => {
    const t = await confirmWorld();
    const [first, second] = [await actor('first bot'), await actor('second bot')];
    t.world.add(first, second);
    await confirmPairing(t.context, await prepare(t, first, 1));
    const prepared = await prepare(t, second, 2);
    t.confirm.before.push(() => ({ agent_id: second.id }));
    await confirmPairing(t.context, prepared);
    const entry = orgEntry(t.store.getState().orgs, t.org);
    expect(entry.log!.state.activeAgent(t.me.id)!.id).toBe(first.id);
    expect(entry.keys.has(epochKey('org', t.org, 2))).toBe(false);
  });

  it('sends no batch and no CK wrap without an org, and refuses an org without a verified log', async () => {
    const t = await confirmWorld();
    const bot = await actor('first bot');
    const prepared = await prepare(t, bot, 1);
    setProfile(t.store, { orgs: [{ org_id: 7 }, 'x'] });
    expect(profileOrgs(t.store)).toEqual([]);
    expect(await confirmationBatches(t.context, prepared)).toMatchObject({ batches: [], cred_wraps: [] });
    expect(t.client.of('GET /v1/wraps')).toHaveLength(0);
    expect(profileOrgs((await agentWorld()).store)).toEqual([]);
    setProfile(t.store, { orgs: [{ org_id: t.org }] });
    blockOrg(t.store, t.org, { code: 'log_chain', stored: null, served: null, lh: null });
    expect(await errorOf(() => confirmationBatches(t.context, prepared))).toBe('log_chain');
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
    // the same keys, only the mode differs
    const { remote: _r, nonce: _n, ...plain } = body;
    expect(await errorOf(() => lookupPairing(async () => plain, link, ORIGIN))).toBe('pairing_mismatch');
    const asLoopback = { ...link, mode: 'loopback' as const, port: 40123 };
    expect(await errorOf(() => lookupPairing(async () => body, asLoopback, ORIGIN))).toBe('pairing_mismatch');
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
    const prepared = await preparePairing(pairing, link, USER, user, ROOT, 1);
    const handover = await openHandover(agent.kem, AGENT, USER, prepared.body.handover_ct!);
    expect(handover.rootKey).toBe(ROOT);
    expect(b64uEncode(handover.secret)).toBe(prepared.secret.reveal((raw) => b64uEncode(raw)));
    expect(prepared.returnCode).toBe(await returnCode(handover.secret, AGENT, USER));
    expect(prepared.returnCode).toMatch(/^[0-9A-HJKMNP-TV-Z]{8}$/);
    const anchor = await openUser(agent.kem, handover.secret, AGENT, USER, prepared.body.user_ct);
    expect(anchor.cert).toBe(prepared.body.cert);
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
    ownIdentity: true,
    heldIdentity: false,
    gen: 1,
    revokedReason: null,
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
