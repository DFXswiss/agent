// The agent as a sub-principal of its user (ADR-082 sections 1, 3, 4 and 9): `agent_add` by the user or by the agent
// itself, the `agent` member of `genesis`/`accept`, rights through `acting`, generations and replacement, removal with
// and without its user, R in the batch or pending, events, credential readers, revoked agents and catch-up.
import { beforeAll, describe, expect, it } from 'vitest';
import { signCert } from '../../../src/core/crypto/agent';
import { CryptoError } from '../../../src/core/crypto/errors';
import { buildEntries, type LogFields, signLogEntry, logBody } from '../../../src/core/log/entries';
import { agentSlots, confirmedSlots, missingCredWraps, missingWraps } from '../../../src/core/log/catchup';
import { checkEvent } from '../../../src/core/log/events';
import { asJson, checkBatch, losers, lost } from '../../../src/core/log/rotation';
import { AGENT } from '../../../src/core/log/state';
import { appendEntries, apply, type VerifiedLog } from '../../../src/core/log/verify';
import { credReaders, revokedAgents, slotJson, slotOf, type WrapSlot } from '../../../src/core/log/wraps';
import { newHeader, sealEvent } from '../../../src/core/crypto/event';
import { utf8 } from '../../../src/core/encoding/bytes';
import { actors, type Actor, EXPIRES, idOf, inviteFor, TS, World } from './world';

const code = async (promise: Promise<unknown>) => {
  try {
    await promise;
  } catch (error) {
    if (error instanceof CryptoError) return error.code;
    throw error;
  }
  return null;
};

let alice: Actor, bob: Actor, carol: Actor, dev: Actor, aliceBot: Actor, bobBot: Actor, bobBot2: Actor, mallory: Actor;

beforeAll(async () => {
  [alice, bob, carol, dev, aliceBot, bobBot, bobBot2, mallory] = await actors(
    'alice',
    'bob',
    'carol',
    'dev',
    'alice bot',
    'bob bot',
    'bob bot 2',
    'mallory',
  );
});

/** The `agent_add` fields of `agent` for `user` with a certificate signed by `signer` (default: the user). */
async function agentAdd(user: Actor, agent: Actor, gen = 1, signer: Actor = user): Promise<LogFields> {
  const pub = agent.identity.public();
  const cert = await signCert(signer.identity.sig.privateKey, { agent: agent.id, pub, user: user.id, gen });
  return { type: 'agent_add', subject: agent.id, user: user.id, pub, gen, cert };
}

/** The `agent` member of a `genesis`/`accept` of `user`. */
async function member(user: Actor, agent: Actor, gen = 1) {
  const fields = (await agentAdd(user, agent, gen)) as { pub: { kem: string; sig: string }; cert: string };
  return { id: agent.id, pub: fields.pub, gen, cert: fields.cert };
}

/** Signs `list` as `signer` after the log head and appends it with the verifier alone (no batch rules). */
async function append(log: VerifiedLog, signer: Actor, list: LogFields[], key = signer): Promise<VerifiedLog> {
  const entries = await buildEntries(
    { org: log.state.org, head: log.head },
    { id: signer.id, key: key.identity.sig.privateKey },
    list,
    TS,
  );
  return appendEntries(log, entries, '');
}

/** alice (admin) with bob (member) and a device on the default workgroup. */
async function org(): Promise<World> {
  const world = await World.create(alice);
  await world.admitUser(alice, bob);
  await world.admitDevice(alice, dev, [world.defaultWg]);
  world.add(aliceBot, bobBot, bobBot2, carol, mallory);
  return world;
}

describe('agent_add', () => {
  it('adds an agent by its user with wraps of every held epoch, and by the agent itself', async () => {
    const world = await org();
    await world.batch(alice, [{ type: 'rotate', scope: 'org', scope_id: world.org, epoch: 2 }]);
    const built = await world.batch(alice, [await agentAdd(alice, aliceBot)]);
    const agent = world.state.member(aliceBot.id)!;
    expect([agent.kind, agent.role, agent.parent, agent.gen]).toEqual([AGENT, AGENT, alice.id, 1]);
    expect(world.state.acting(aliceBot.id)).toBe(alice.id);
    expect(world.state.acting(bob.id)).toBe(bob.id);
    expect(world.state.isAdmin(aliceBot.id)).toBe(true);
    expect(world.state.admins()).toEqual([alice.id]);
    expect(world.state.readers('org', world.org)).toContain(aliceBot.id);
    expect(built.body.wraps.map((w) => [w.scope, w.epoch, w.recipient_id])).toEqual([
      ['org', 1, aliceBot.id],
      ['org', 2, aliceBot.id],
      ['wg', 1, aliceBot.id],
    ]);
    expect(world.currentKey(aliceBot.id, 'org', world.org)).toBe(world.currentKey(alice.id, 'org', world.org));
    // bob's agent adds itself with bob's certificate
    await world.batch(bobBot, [await agentAdd(bob, bobBot)], { keys: world.ring(alice.id) });
    expect(world.state.member(bobBot.id)!.parent).toBe(bob.id);
    expect(world.state.isAdmin(bobBot.id)).toBe(false);
    expect(world.state.isAdmin(mallory.id)).toBe(false);
    expect(world.state.activeAgent(carol.id)).toBeNull();
  });

  it('refuses agents the user did not certify and every other agent_add rule (log_agent)', async () => {
    const world = await org();
    const log = world.log!;
    const refused = async (signer: Actor, fields: LogFields, key = signer) => code(append(log, signer, [fields], key));
    // by neither the user nor the agent
    expect(await refused(alice, await agentAdd(bob, bobBot))).toBe('log_agent');
    // the hub forges: certificate by another key, for another key, for another user
    expect(await refused(bobBot, await agentAdd(bob, bobBot, 1, mallory))).toBe('log_agent');
    const otherKey = { ...((await agentAdd(bob, bobBot)) as Record<string, unknown>), pub: mallory.identity.public() };
    expect(await refused(bobBot, otherKey as LogFields, mallory)).toBe('log_agent');
    const forAlice = { ...((await agentAdd(bob, bobBot)) as Record<string, unknown>), user: alice.id };
    expect(await refused(bobBot, forAlice as LogFields)).toBe('log_agent');
    // the user is not an active user member (a device, an unknown ID); the subject is a user or device
    expect(await refused(bobBot, await agentAdd(dev, bobBot))).toBe('log_agent');
    expect(await refused(bobBot, await agentAdd(carol, bobBot))).toBe('log_agent');
    expect(await refused(bob, await agentAdd(bob, alice))).toBe('log_agent');
    expect(await refused(bob, await agentAdd(bob, dev))).toBe('log_agent');
    // the agent signs with a key other than the one it brings; an unknown author
    expect(await refused(bobBot, await agentAdd(bob, bobBot), mallory)).toBe('log_signature');
    expect(await refused(mallory, await agentAdd(bob, bobBot))).toBe('log_signature');
  });

  it('lets an agent not add another agent', async () => {
    const world = await org();
    await world.batch(bob, [await agentAdd(bob, bobBot)]);
    expect(await code(append(world.log!, bobBot, [await agentAdd(bob, bobBot2, 2)]))).toBe('log_agent');
    expect(await code(append(world.log!, bobBot, [await agentAdd(bob, bobBot2, 2, bobBot)]))).toBe('log_agent');
  });

  it('replaces a lower generation (R of the old agent in the batch) and refuses an equal or lower one', async () => {
    const world = await org();
    await world.batch(alice, [await agentAdd(alice, aliceBot)]);
    const log = world.log!;
    expect(await code(append(log, alice, [await agentAdd(alice, bobBot, 1)]))).toBe('log_agent');
    expect(await code(append(log, alice, [await agentAdd(alice, aliceBot, 1)]))).toBe('log_agent');
    // without its rotations the replacement is refused
    const bare = await buildEntries(
      { org: world.org, head: log.head },
      { id: alice.id, key: alice.identity.sig.privateKey },
      [await agentAdd(alice, bobBot, 2)],
      TS,
    );
    expect(await code(checkBatch(log, bare, [], alice.id))).toBe('rotation_required');
    const built = await world.batch(alice, [await agentAdd(alice, bobBot, 2)]);
    expect(built.required).toEqual(
      asJson(world.batches[0]!.log.state.readableScopes(alice.id).map((s) => s.join(' '))),
    );
    const old = world.state.principals.get(aliceBot.id)!;
    expect([old.removedSeq, old.removedWithUser]).toEqual([log.head.seq + 1, false]);
    expect(world.state.activeAgent(alice.id)!.id).toBe(bobBot.id);
    expect(world.state.readers('org', world.org)).not.toContain(aliceBot.id);
    // the replaced agent cannot come back
    expect(await code(append(world.log!, alice, [await agentAdd(alice, aliceBot, 3)]))).toBe('log_agent');
    const before = world.log!.states[log.head.seq]!;
    const after = world.log!.states[log.head.seq + 1]!;
    const entry = world.log!.entries[log.head.seq + 1]!.body;
    expect(losers(before, entry)).toEqual(new Set([bobBot.id, aliceBot.id]));
    expect(lost(before, after, entry).size).toBe(2);
  });
});

describe('the agent member of genesis and accept', () => {
  it('creates an org signed by the agent for its user', async () => {
    const orgId = await idOf('agent org');
    const fields: LogFields = {
      type: 'genesis',
      subject: alice.id,
      pub: alice.identity.public(),
      default_wg: await idOf('agent org wg'),
      private: true,
      agent: await member(alice, aliceBot),
    };
    const body = logBody({ org: orgId, head: null }, alice.id, fields, TS);
    const founder = alice.identity.public().sig;
    const signed = await signLogEntry(body, aliceBot.identity.sig.privateKey);
    const state = await apply(null, signed, founder);
    expect(state.member(aliceBot.id)!.parent).toBe(alice.id);
    expect(state.member(alice.id)!.joinAgent).toBe(aliceBot.id);
    expect(state.readers('org', orgId)).toEqual([alice.id, aliceBot.id].sort());
    // signed by the user instead of the agent; a certificate by another key
    expect(await code(apply(null, await signLogEntry(body, alice.identity.sig.privateKey), founder))).toBe(
      'log_genesis',
    );
    const forged = {
      ...fields,
      agent: { ...(await member(alice, aliceBot)), cert: (await member(bob, aliceBot)).cert },
    };
    const forgedBody = logBody({ org: orgId, head: null }, alice.id, forged, TS);
    expect(await code(apply(null, await signLogEntry(forgedBody, aliceBot.identity.sig.privateKey), founder))).toBe(
      'log_agent',
    );
    // a batch of the agent: it wraps the new keys for its user and itself
    const genesis = await signLogEntry(body, aliceBot.identity.sig.privateKey);
    expect(await code(checkBatch(null, [genesis], [], aliceBot.id, { founder }))).toBe('rotation_required');
  });

  it('accepts an invitation signed by the agent, but not for a device', async () => {
    const world = await org();
    const secrets = await inviteFor('carol with agent');
    await world.batch(alice, [
      {
        type: 'invite',
        invite_id: secrets.inviteId,
        pk_inv: secrets.pkInv,
        kind: 'user',
        role: 'member',
        wgs: {},
        expires: EXPIRES,
      },
    ]);
    const fields: LogFields = {
      type: 'accept',
      invite_id: secrets.inviteId,
      subject: carol.id,
      kind: 'user',
      pub: carol.identity.public(),
      agent: await member(carol, bobBot2),
    };
    const head = world.log!.head;
    const body = logBody({ org: world.org, head }, carol.id, fields, TS);
    const signed = await signLogEntry(body, bobBot2.identity.sig.privateKey, secrets.signing.privateKey);
    const after = await appendEntries(world.log, [signed], '');
    expect(after.state.member(bobBot2.id)!.parent).toBe(carol.id);
    expect(after.state.member(carol.id)!.joinAgent).toBe(bobBot2.id);
    const byUser = await signLogEntry(body, carol.identity.sig.privateKey, secrets.signing.privateKey);
    expect(await code(appendEntries(world.log, [byUser], ''))).toBe('log_accept');
    const badGen = logBody({ org: world.org, head }, carol.id, { ...fields, agent: await member(bob, bobBot2) }, TS);
    const badCert = await signLogEntry(badGen, bobBot2.identity.sig.privateKey, secrets.signing.privateKey);
    expect(await code(appendEntries(world.log, [badCert], ''))).toBe('log_agent');
    // a device has no agent
    const devSecrets = await inviteFor('device with agent');
    await world.batch(alice, [
      {
        type: 'invite',
        invite_id: devSecrets.inviteId,
        pk_inv: devSecrets.pkInv,
        kind: 'device',
        wgs: {},
        expires: EXPIRES,
      },
    ]);
    const devFields: LogFields = {
      type: 'accept',
      invite_id: devSecrets.inviteId,
      subject: mallory.id,
      kind: 'device',
      pub: mallory.identity.public(),
      agent: await member(mallory, bobBot2),
    };
    const devBody = logBody({ org: world.org, head: world.log!.head }, mallory.id, devFields, TS);
    const devEntry = await signLogEntry(devBody, bobBot2.identity.sig.privateKey, devSecrets.signing.privateKey);
    expect(await code(appendEntries(world.log, [devEntry], ''))).toBe('log_agent');
  });
});

describe('rights through the user', () => {
  it('lets the agent of an admin author admin entries and refuses those of a member', async () => {
    const world = await org();
    await world.batch(alice, [await agentAdd(alice, aliceBot)]);
    await world.batch(bob, [await agentAdd(bob, bobBot)]);
    const wg = await idOf('agent wg');
    await world.batch(aliceBot, [
      { type: 'wg_create', wg, epoch: 1 },
      { type: 'grant', subject: bob.id, wg, access: 'read' },
    ]);
    // the creator's write access is the user's, never the agent's own
    expect(world.state.explicitAccess(alice.id, wg)).toBe('write');
    expect(world.state.access.has(`${aliceBot.id} ${wg}`)).toBe(false);
    expect(world.state.canWriteWg(aliceBot.id, wg)).toBe(true);
    expect(world.state.canRead(bobBot.id, 'wg', wg)).toBe(true);
    expect(world.state.inherited(bobBot.id, wg)).toBe(false);
    await world.batch(aliceBot, [{ type: 'rotate', scope: 'wg', scope_id: wg, epoch: 2 }]);
    const log = world.log!;
    expect(await code(append(log, bobBot, [{ type: 'rotate', scope: 'org', scope_id: world.org, epoch: 2 }]))).toBe(
      'log_rights',
    );
    expect(await code(append(log, bobBot, [{ type: 'wg_create', wg: await idOf('x'), epoch: 1 }]))).toBe('log_rights');
    // an agent is never the subject of grant, role or ungrant
    expect(await code(append(log, alice, [{ type: 'grant', subject: bobBot.id, wg, access: 'write' }]))).toBe(
      'log_rights',
    );
    expect(await code(append(log, alice, [{ type: 'role', subject: bobBot.id, role: 'admin' }]))).toBe('log_rights');
    expect(await code(append(log, alice, [{ type: 'ungrant', subject: bobBot.id, wg }]))).toBe('log_rights');
  });

  it('demotes its own admin user as a self-demotion (pending)', async () => {
    const world = await org();
    await world.batch(alice, [{ type: 'role', subject: bob.id, role: 'admin' }]);
    await world.batch(bob, [await agentAdd(bob, bobBot)]);
    const wg = await idOf('demote wg');
    await world.batch(alice, [{ type: 'wg_create', wg, epoch: 1 }]);
    const built = await world.batch(bobBot, [{ type: 'role', subject: bob.id, role: 'member' }]);
    expect(built.required).toEqual([]);
    expect(asJson(world.state.pending)).toEqual([{ scope: 'wg', scope_id: wg }]);
  });
});

describe('removal', () => {
  let world: World;

  beforeAll(async () => {
    world = await org();
    await world.batch(alice, [await agentAdd(alice, aliceBot)]);
    await world.batch(bob, [await agentAdd(bob, bobBot)]);
  });

  it('owes R in the batch when the agent of an admin removes an agent', async () => {
    const own = await org();
    await own.batch(alice, [await agentAdd(alice, aliceBot)]);
    await own.batch(bob, [await agentAdd(bob, bobBot)]);
    const built = await own.batch(aliceBot, [{ type: 'remove', subject: bobBot.id }]);
    expect(built.required).toEqual(asJson([`org ${own.org}`, `wg ${own.defaultWg}`]));
    expect(own.state.principals.get(bobBot.id)!.removedWithUser).toBe(false);
    expect(own.currentKey(bob.id, 'org', own.org)).toBe(own.currentKey(alice.id, 'org', own.org));
    // an admin's removal ends the membership in this org only: the agent is not revoked elsewhere
    const removed = own.state.principals.get(bobBot.id)!;
    expect(removed.removedByOwner).toBe(false);
    expect(revokedAgents([own.state])).toEqual(new Set());
    const shown = (own.state.projection().members as { principal_id: string; removed_by_owner?: boolean }[]).find(
      (m) => m.principal_id === bobBot.id,
    );
    expect(shown!.removed_by_owner).toBe(false);
  });

  it('lets a member remove or replace its agent and rotate what the agent read', async () => {
    const own = await org();
    const wg = await idOf('member agent wg');
    await own.batch(alice, [
      { type: 'wg_create', wg, epoch: 1 },
      { type: 'grant', subject: bob.id, wg, access: 'read' },
    ]);
    await own.batch(bob, [await agentAdd(bob, bobBot)]);
    const replaced = await own.batch(bob, [await agentAdd(bob, bobBot2, 2)]);
    expect(replaced.required).toEqual(asJson([`org ${own.org}`, `wg ${own.defaultWg}`, `wg ${wg}`]));
    expect(own.state.agentOwed.size).toBe(0);
    expect(own.currentKey(bobBot2.id, 'wg', wg)).toBe(own.currentKey(alice.id, 'wg', wg));
    // the owed scopes show in the projection until they are rotated
    const owing = await append(own.log!, bob, [{ type: 'remove', subject: bobBot2.id }]);
    expect(owing.state.projection().agent_owed).toHaveLength(3);
    expect(own.state.projection()).not.toHaveProperty('agent_owed');
    const built = await own.batch(bob, [{ type: 'remove', subject: bobBot2.id }]);
    expect(built.required).toHaveLength(3);
    // another member owes nothing and may not rotate
    expect(await code(append(owing, carol, [{ type: 'rotate', scope: 'org', scope_id: own.org, epoch: 4 }]))).toBe(
      'log_signature',
    );
    expect(await code(append(owing, dev, [{ type: 'rotate', scope: 'wg', scope_id: wg, epoch: 4 }]))).toBe(
      'log_rights',
    );
  });

  it('makes the R of an agent that leaves by itself pending', async () => {
    const after = await append(world.log!, bobBot, [{ type: 'remove', subject: bobBot.id }]);
    expect(asJson(after.state.pending)).toEqual(asJson([`org ${world.org}`, `wg ${world.defaultWg}`]));
    expect(revokedAgents([after.state])).toEqual(new Set([bobBot.id]));
  });

  it('lets the user leave through its agent (pending) and removes the agent with its user', async () => {
    const after = await append(world.log!, bobBot, [{ type: 'remove', subject: bob.id }]);
    expect(after.state.pending.size).toBe(2);
    const agent = after.state.principals.get(bobBot.id)!;
    expect([agent.removedSeq, agent.removedWithUser]).toEqual([after.state.seq, true]);
    expect(after.state.member(bobBot.id)).toBeNull();
    expect(revokedAgents([after.state]).size).toBe(0);
    const entries = await buildEntries(
      { org: world.org, head: world.log!.head },
      { id: bobBot.id, key: bobBot.identity.sig.privateKey },
      [{ type: 'remove', subject: bob.id }],
      TS,
    );
    expect((await checkBatch(world.log, entries, [], alice.id)).required.size).toBe(0);
  });

  it('refuses the removal of an agent by another member', async () => {
    expect(await code(append(world.log!, bob, [{ type: 'remove', subject: aliceBot.id }]))).toBe('log_rights');
    expect(await code(append(world.log!, bobBot, [{ type: 'remove', subject: alice.id }]))).toBe('log_rights');
  });

  it('owes R of the user and its agent when an admin removes the user', async () => {
    const entries = await buildEntries(
      { org: world.org, head: world.log!.head },
      { id: alice.id, key: alice.identity.sig.privateKey },
      [{ type: 'remove', subject: bob.id }],
      TS,
    );
    expect(await code(checkBatch(world.log, entries, [], alice.id))).toBe('rotation_required');
    const after = await appendEntries(world.log, entries, '');
    expect(losers(world.log!.state, entries[0]!.body)).toEqual(new Set([bob.id, bobBot.id]));
    expect(lost(world.log!.state, after.state, entries[0]!.body).size).toBe(2);
  });
});

describe('rejoin', () => {
  it('lets the agent removed with its user come back, but not with another generation', async () => {
    const world = await org();
    await world.batch(bob, [await agentAdd(bob, bobBot)]);
    await world.batch(alice, [{ type: 'remove', subject: bob.id }]);
    expect(world.state.activeAgent(bob.id)).toBeNull();
    await world.admitUser(alice, bob);
    expect(world.state.readers('org', world.org)).not.toContain(bobBot.id);
    const log = world.log!;
    expect(await code(append(log, bob, [await agentAdd(bob, bobBot, 2)]))).toBe('log_agent');
    const back = await append(log, bob, [await agentAdd(bob, bobBot, 1)]);
    expect(back.state.member(bobBot.id)!.parent).toBe(bob.id);
    // a different key under the same ID does not come back
    const otherKey = { ...((await agentAdd(bob, bobBot)) as Record<string, unknown>), pub: mallory.identity.public() };
    const cert = await signCert(bob.identity.sig.privateKey, {
      agent: bobBot.id,
      pub: mallory.identity.public(),
      user: bob.id,
      gen: 1,
    });
    expect(await code(append(log, bob, [{ ...otherKey, cert } as LogFields]))).toBe('log_agent');
    // a new agent needs a generation above the removed one
    expect(await code(append(log, bob, [await agentAdd(bob, aliceBot)]))).toBe('log_agent');
  });
});

describe('events by an agent', () => {
  it('writes prompts with its user rights and its user profile', async () => {
    const world = await org();
    await world.batch(bob, [await agentAdd(bob, bobBot)]);
    const log = world.log!;
    const event = async (kind: string, scope: string, scopeId: string, extra: Record<string, string>) => {
      const header = newHeader({
        id: await idOf(`agent event ${kind} ${JSON.stringify(extra)}`),
        org: world.org,
        scope,
        scope_id: scopeId,
        epoch: 1,
        lh: log.head.seq,
        author: bobBot.id,
        kind,
        ts: TS,
        ...extra,
      });
      const key = world.ring(bobBot.id)(scope, scopeId, 1)!;
      return sealEvent(key, header, utf8('{"x":1}'), bobBot.identity.sig.privateKey);
    };
    const task = await idOf('agent task');
    const prompt = await event('prompt', 'wg', world.defaultWg, { wg: world.defaultWg, task });
    expect((await checkEvent(prompt, log)).header.author).toBe(bobBot.id);
    expect((await checkEvent(await event('member.profile', 'org', world.org, { ref: bob.id }), log)).rule.kind).toBe(
      'member.profile',
    );
    expect(await code(checkEvent(await event('member.profile', 'org', world.org, { ref: bobBot.id }), log))).toBe(
      'header_invalid',
    );
    expect(await code(checkEvent(await event('org.profile', 'org', world.org, {}), log))).toBe('author_not_writer');
  });
});

describe('credential readers and revoked agents', () => {
  it('adds the active agent, drops revoked ones and the consent of an org a revoked agent joined', async () => {
    const one = await org();
    await one.batch(bob, [await agentAdd(bob, bobBot)]);
    expect(credReaders([one.state], bob.id)).toEqual(new Set([bob.id, bobBot.id, dev.id]));
    // another org of bob, joined through its agent, which a higher generation supersedes there
    const two = await World.create(carol);
    // an org the owner is not a member of adds nobody
    expect(credReaders([one.state, two.state], bob.id)).toEqual(new Set([bob.id, bobBot.id, dev.id]));
    two.add(bob, bobBot, bobBot2, dev);
    const secrets = await inviteFor('bob in two');
    await two.batch(carol, [
      {
        type: 'invite',
        invite_id: secrets.inviteId,
        pk_inv: secrets.pkInv,
        kind: 'user',
        role: 'member',
        wgs: {},
        expires: EXPIRES,
      },
    ]);
    const fields: LogFields = {
      type: 'accept',
      invite_id: secrets.inviteId,
      subject: bob.id,
      kind: 'user',
      pub: bob.identity.public(),
      agent: await member(bob, bobBot),
    };
    const body = logBody({ org: two.org, head: two.log!.head }, bob.id, fields, TS);
    two.log = await appendEntries(
      two.log,
      [await signLogEntry(body, bobBot.identity.sig.privateKey, secrets.signing.privateKey)],
      '',
    );
    await two.admitDevice(carol, dev, [two.defaultWg]);
    expect(credReaders([one.state, two.state], bob.id)).toEqual(new Set([bob.id, bobBot.id, dev.id]));
    const replaced = await append(two.log!, bob, [await agentAdd(bob, bobBot2, 2)]);
    const revoked = revokedAgents([one.state, replaced.state]);
    expect(revoked).toEqual(new Set([bobBot.id]));
    // bobBot is superseded everywhere; org two was joined by it, so its device gets no credential key from there
    expect(credReaders([one.state, replaced.state], bob.id)).toEqual(new Set([bob.id, bobBot2.id, dev.id]));
    expect(credReaders([replaced.state], bob.id)).toEqual(new Set([bob.id, bobBot2.id]));
  });
});

describe('the epochs a batch gives an agent', () => {
  it('gives an agent the range its user gets: history only with the grant, every held epoch when installed', async () => {
    const world = await org();
    const [held, plain, past] = [await idOf('range held'), await idOf('range plain'), await idOf('range past')];
    for (const wg of [held, plain, past]) {
      await world.batch(alice, [{ type: 'wg_create', wg, epoch: 1 }]);
      await world.batch(alice, [{ type: 'rotate', scope: 'wg', scope_id: wg, epoch: 2 }]);
    }
    await world.batch(alice, [{ type: 'grant', subject: bob.id, wg: held, access: 'read' }], { history: true });
    type Built = { body: { wraps: { scope_id: string; epoch: number; recipient_id: string }[] } };
    const epochsOf = (built: Built, wg: string) =>
      built.body.wraps.filter((w) => w.scope_id === wg && w.recipient_id === bobBot.id).map((w) => w.epoch);
    // installing the agent by its user: every epoch the user holds
    expect(epochsOf(await world.batch(bob, [await agentAdd(bob, bobBot)]), held)).toEqual([1, 2]);
    const without = await world.batch(alice, [{ type: 'grant', subject: bob.id, wg: plain, access: 'read' }]);
    expect(epochsOf(without, plain)).toEqual([2]);
    const withHistory = await world.batch(alice, [{ type: 'grant', subject: bob.id, wg: past, access: 'read' }], {
      history: true,
    });
    expect(epochsOf(withHistory, past)).toEqual([1, 2]);
  });
});

describe('catch-up after a rejoin', () => {
  it('confirms no slot of an epoch from before the user rejoined', async () => {
    const world = await org();
    await world.batch(bob, [await agentAdd(bob, bobBot)]);
    await world.batch(alice, [{ type: 'remove', subject: bob.id }]);
    await world.admitUser(alice, bob);
    world.log = await append(world.log!, bob, [await agentAdd(bob, bobBot)]);
    const log = world.log;
    const slot = (epoch: number): WrapSlot => ({
      scope: 'org',
      scope_id: world.org,
      epoch,
      recipient: bobBot.id,
    });
    expect(log.state.epoch('org', world.org)).toBe(2);
    expect(confirmedSlots(log, alice.id, [slot(1), slot(2)]).map(slotJson)).toEqual([slotJson(slot(2))]);
    const bobs = [1, 2].map((epoch) => ({ ...slot(epoch), recipient: bob.id }));
    expect(agentSlots(log, bobs).map((s) => s.epoch)).toEqual([2]);
  });
});

describe('catch-up for an agent', () => {
  it('gives the agent every slot its user has, confirmed by the log', async () => {
    const world = await org();
    await world.batch(alice, [{ type: 'rotate', scope: 'wg', scope_id: world.defaultWg, epoch: 2 }]);
    const wg = await idOf('later wg');
    await world.batch(alice, [{ type: 'wg_create', wg, epoch: 1 }]);
    await world.batch(alice, [{ type: 'rotate', scope: 'wg', scope_id: wg, epoch: 2 }]);
    await world.batch(alice, [{ type: 'grant', subject: bob.id, wg, access: 'read' }], { history: true });
    // the agent is added without wraps: catch-up delivers them
    world.log = await append(world.log!, bob, [await agentAdd(bob, bobBot)]);
    const existing: WrapSlot[] = world.wraps.map((wrap) => slotOf(wrap));
    const mine = (slots: WrapSlot[]) => slots.filter((slot) => slot.recipient === bobBot.id).map(slotJson);
    expect(agentSlots(world.log, existing).every((slot) => slot.recipient === bobBot.id)).toBe(true);
    const missing = missingWraps(world.log, alice.id, existing);
    const expected = [
      { scope: 'org', scope_id: world.org, epoch: 1, recipient_id: bobBot.id },
      ...[1, 2].map((epoch) => ({ scope: 'wg', scope_id: wg, epoch, recipient_id: bobBot.id })),
      ...[1, 2].map((epoch) => ({ scope: 'wg', scope_id: world.defaultWg, epoch, recipient_id: bobBot.id })),
    ].sort((a, b) => (a.scope_id + a.scope + a.epoch < b.scope_id + b.scope + b.epoch ? -1 : 1));
    expect(new Set(mine(missing).map((s) => JSON.stringify(s)))).toEqual(
      new Set(expected.map((s) => JSON.stringify(s))),
    );
    // the browser confirms the slots its user held by the log; the history bob got by the grant is not in the log
    const confirmed = mine(confirmedSlots(world.log, alice.id, missing));
    expect(confirmed).not.toContainEqual({ scope: 'wg', scope_id: wg, epoch: 1, recipient_id: bobBot.id });
    expect(confirmed).toContainEqual({ scope: 'wg', scope_id: world.defaultWg, epoch: 1, recipient_id: bobBot.id });
    expect(confirmed).toContainEqual({ scope: 'wg', scope_id: wg, epoch: 2, recipient_id: bobBot.id });
    // credential key: every CK epoch the owner has a wrap of, and the current one
    const cred = (epoch: number, recipient: string): WrapSlot => ({
      scope: 'cred',
      scope_id: bob.id,
      epoch,
      recipient,
    });
    const credMissing = missingCredWraps([world.log], bob.id, bob.id, 2, [
      cred(1, bob.id),
      cred(2, bob.id),
      cred(1, dev.id),
    ]);
    const keys = (slots: WrapSlot[]) => slots.map((slot) => JSON.stringify(slotJson(slot))).sort();
    expect(keys(credMissing)).toEqual(keys([cred(1, bobBot.id), cred(2, bobBot.id), cred(2, dev.id)]));
  });
});
