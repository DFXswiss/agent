// Job "agents" (ADR-082 sections 4 and 9): the user's revoked agent – superseded or removed elsewhere in the verified
// logs, or revoked at the hub – is removed from a log where it is still active, with R in the same batch, and the
// credential key is rotated after it; the hub's active own-identity agent is added where it is missing, with the
// certificate of a verified log or – for a pairing this browser confirmed – of the hub's list; the cases in which
// nothing is sent.
import { describe, expect, it, vi } from 'vitest';
import { Prefs } from '../../../src/core/account/prefs';
import type { Row } from '../../../src/core/agents/list';
import { signCert } from '../../../src/core/crypto/agent';
import type { JobContext } from '../../../src/core/jobs';
import { agentAddOf, agentsPlanner, hubAgents, syncAgents } from '../../../src/core/agents/sync';
import { job } from '../../../src/core/jobs/agents';
import { rewrap } from '../../../src/core/credentials/rotation';
import { entryHash, type LogFields, logBody, signLogEntry } from '../../../src/core/log/entries';
import { checkBatch } from '../../../src/core/log/rotation';
import type { Principal } from '../../../src/core/log/state';
import { appendEntries, type VerifiedLog } from '../../../src/core/log/verify';
import { HubError } from '../../../src/core/net/signedFetch';
import { setIdentity, setProfile } from '../../../src/core/store/slices/me';
import { ensureOrg, orgEntry, setLog } from '../../../src/core/store/slices/orgs';
import { credWorld, type CredWorld } from '../credentials/world';
import { actor, type Actor, idOf, TS, World } from '../log/world';

const T = '2026-11-21T12:00:00.000Z';

/** The `agent_add` fields of `agent` for `user`, certified by the user. */
async function agentAdd(user: Actor, agent: Actor, gen = 1): Promise<LogFields> {
  const pub = agent.identity.public();
  const cert = await signCert(user.identity.sig.privateKey, { agent: agent.id, pub, user: user.id, gen });
  return { type: 'agent_add', subject: agent.id, user: user.id, pub, gen, cert };
}

/** A row of `GET /v1/me/agents`. */
function row(agent: string, state: Row['state'], extra: Partial<Row> = {}): Row {
  return {
    agent_id: agent,
    state,
    label_ct: 'x',
    created_at: T,
    confirmed_at: T,
    claimed_at: T,
    last_used_at: null,
    revoked_at: null,
    replaces: null,
    own_identity: true,
    held_identity: false,
    gen: 1,
    revoked_reason: null,
    ...extra,
  };
}

/** A second org of `founder` (`World.create` always takes the same IDs). */
async function secondOrg(founder: Actor): Promise<World> {
  const world = new World(await idOf('second org'), await idOf('second default wg'));
  world.add(founder);
  await world.batch(founder, [
    {
      type: 'genesis',
      subject: founder.id,
      pub: founder.identity.public(),
      default_wg: world.defaultWg,
      private: true,
    },
  ]);
  return world;
}

/** The credential world with a hub that appends batches to its orgs and lists `rows` as the user's agents. */
async function setup(options: { member?: boolean } = {}) {
  const t = await credWorld(options);
  const worlds = new Map<string, World>([[t.org, t.world]]);
  const rows: Row[] = [];
  const serve = (world: World) => {
    t.client.on(`POST /v1/orgs/${world.org}/log`, async (raw) => {
      const body = raw as { expected_seq: number; entries: never[]; wraps: never[] };
      if (body.expected_seq !== world.log!.head.seq + 1) throw new HubError('seq_conflict', 409);
      const result = await checkBatch(world.log, body.entries, body.wraps, t.me.id, { platform: world.platform });
      world.log = result.log;
      await world.store(body.wraps);
      return { head: result.log.head };
    });
    t.client.on(`GET /v1/orgs/${world.org}/log`, async (_body, target) => ({
      entries: world
        .log!.entries.map((entry, seq) => ({ seq, entry }))
        .filter((item) => item.seq > Number(new URL(target, 'https://hub.example').searchParams.get('after') ?? -1)),
      head: world.log!.head,
      next_after: null,
    }));
  };
  serve(t.world);
  t.client.on('GET /v1/me/agents', () => ({ agents: rows }));
  const addOrg = async () => {
    const world = await secondOrg(t.me);
    worlds.set(world.org, world);
    serve(world);
    ensureOrg(t.store, world.org);
    setLog(t.store, world.org, world.log!);
    setProfile(t.store, { orgs: [...worlds.keys()].map((org) => ({ org_id: org })) });
    t.client.on('GET /v1/me', () => ({ user_id: t.me.id, orgs: [...worlds.keys()].map((org) => ({ org_id: org })) }));
    return world;
  };
  const sync = (world: World = t.world) => setLog(t.store, world.org, world.log!);
  return { t, rows, addOrg, sync };
}

function jobContext(t: CredWorld, prefs: Prefs = new Prefs(null)): JobContext {
  return {
    prefs,
    store: t.store,
    client: t.client,
    rootKey: t.platform.root,
    now: () => t.clock.now,
    persist: {
      loadPinned: async () => null,
      savePinned: async () => undefined,
      loadPlatformHead: async () => null,
      savePlatformHead: async () => undefined,
    },
  };
}

const runOf = (t: CredWorld, org = t.org, generation = t.store.generation) => ({
  org,
  trigger: 'log' as const,
  generation,
});

const types = (t: CredWorld, world: World) =>
  t.client
    .of(`POST /v1/orgs/${world.org}/log`)
    .map((call) => (call.body as { entries: { body: { type: string } }[] }).entries.map((e) => e.body.type));

describe('hubAgents', () => {
  it('reads the revoked and expired agents and the newest active own-identity agent', async () => {
    const sig = new Uint8Array(32);
    const rows = [
      row('a', 'active', { own_identity: false }),
      row('b', 'active', { gen: null }),
      row('c', 'active', { gen: 4 }),
      row('d', 'active', { gen: 5 }),
      row('e', 'revoked'),
      row('f', 'expired'),
      row('g', 'confirmed'),
    ];
    expect(await hubAgents(rows, 'me', sig)).toEqual({
      revoked: new Set(['e', 'f']),
      active: { id: 'c', gen: 4, add: null },
    });
    expect(await hubAgents([], 'me', sig)).toEqual({ revoked: new Set(), active: null });
  });
});

describe('the agents job', () => {
  it('removes an agent the hub revoked with its rotations, then the credential key is rotated', async () => {
    const { t, rows, sync } = await setup();
    const bot = await actor('bot one');
    t.world.add(bot);
    await t.world.batch(t.me, [await agentAdd(t.me, bot)]);
    sync();
    const context = jobContext(t);
    // still active at the hub: nothing to do
    rows.push(row(bot.id, 'active'));
    expect(await syncAgents(context, runOf(t))).toBe(false);
    rows[0] = row(bot.id, 'revoked', { revoked_reason: 'revoked' });
    t.clock.now = Date.parse('2026-11-21T12:30:00.000Z');
    expect(await syncAgents(context, runOf(t))).toBe(true);
    expect(types(t, t.world)).toEqual([['remove', 'rotate', 'rotate']]);
    expect(orgEntry(t.store.getState().orgs, t.org).log!.state.member(bot.id)).toBeNull();
    // the job credential-rewrap runs after this one: the agent lost the credential key
    const result = await rewrap(context, runOf(t));
    expect(result).toMatchObject({ status: 'rotated', epoch: 2 });
    expect(t.hub.rotations[0]!.wraps.map((wrap) => wrap.recipient_id)).not.toContain(bot.id);
    // gone: nothing more to do
    expect(await syncAgents(context, runOf(t))).toBe(false);
  });

  it('lets a member without admin right remove its revoked agent with the rotations it owes', async () => {
    const { t, rows, sync } = await setup({ member: true });
    expect(t.world.state.isAdmin(t.me.id)).toBe(false);
    const bot = await actor('bot one');
    t.world.add(bot);
    await t.world.batch(t.me, [await agentAdd(t.me, bot)]);
    sync();
    rows.push(row(bot.id, 'revoked', { revoked_reason: 'revoked' }));
    expect(await syncAgents(jobContext(t), runOf(t))).toBe(true);
    expect(types(t, t.world)).toEqual([['remove', 'rotate', 'rotate']]);
    const state = orgEntry(t.store.getState().orgs, t.org).log!.state;
    expect(state.member(bot.id)).toBeNull();
    expect(state.agentOwed.size).toBe(0);
  });

  it('removes an agent superseded in another org, then adds the new one there with its certificate', async () => {
    const { t, rows, addOrg, sync } = await setup();
    const [first, second] = [await actor('bot one'), await actor('bot two')];
    t.world.add(first, second);
    const other = await addOrg();
    other.add(first, second);
    await t.world.batch(t.me, [await agentAdd(t.me, first, 1)]);
    sync();
    await other.batch(t.me, [await agentAdd(t.me, second, 2)]);
    sync(other);
    rows.push(row(second.id, 'active', { gen: 2 }), row(first.id, 'revoked', { revoked_reason: 'replaced' }));
    const context = jobContext(t);
    expect(await syncAgents(context, runOf(t))).toBe(true);
    expect(await syncAgents(context, runOf(t))).toBe(true);
    expect(types(t, t.world)).toEqual([['remove', 'rotate', 'rotate'], ['agent_add']]);
    const state = orgEntry(t.store.getState().orgs, t.org).log!.state;
    expect(state.activeAgent(t.me.id)!.id).toBe(second.id);
    expect(t.world.currentKey(second.id, 'org', t.org)).toBe(t.world.currentKey(t.me.id, 'org', t.org));
    expect(await syncAgents(context, runOf(t))).toBe(false);
    expect(await syncAgents(context, runOf(t, other.org))).toBe(false);
  });

  it("adds the hub's active agent no log names yet when its certificate verifies under the user's key", async () => {
    const { t, rows } = await setup();
    const [bot, mallory] = [await actor('bot one'), await actor('mallory')];
    t.world.add(bot);
    const pub = bot.identity.public();
    const certBy = (signer: Actor) =>
      signCert(signer.identity.sig.privateKey, { agent: bot.id, pub, user: t.me.id, gen: 1 });
    const prefs = new Prefs(null);
    const context = jobContext(t, prefs);
    const genuine = await certBy(t.me);
    // a pairing this browser did not confirm: nothing is added, even with a genuine certificate
    rows.push(row(bot.id, 'active', { pub, cert: genuine }));
    expect(await syncAgents(context, runOf(t))).toBe(false);
    const { prefs: _prefs, ...withoutPrefs } = context;
    expect(await syncAgents(withoutPrefs, runOf(t))).toBe(false);
    // confirmed here, but keys without a certificate, a certificate the user did not sign, another certificate
    const forged = await certBy(mallory);
    const remembered = { [bot.id]: genuine };
    vi.spyOn(prefs, 'confirmedAgents').mockReturnValue(remembered);
    rows[0] = row(bot.id, 'active', { pub });
    expect(await syncAgents(context, runOf(t))).toBe(false);
    rows[0] = row(bot.id, 'active', { cert: genuine });
    expect(await syncAgents(context, runOf(t))).toBe(false);
    rows[0] = row(bot.id, 'active', { pub, cert: forged });
    expect(await syncAgents(context, runOf(t))).toBe(false);
    remembered[bot.id] = forged;
    expect(await syncAgents(context, runOf(t))).toBe(false);
    remembered[bot.id] = genuine;
    rows[0] = row(bot.id, 'active', { pub, cert: genuine });
    // another client appended first: the log is read again and the batch rebuilt
    await t.world.batch(t.me, [{ type: 'rotate', scope: 'org', scope_id: t.org, epoch: 2 }]);
    t.client.on('GET /v1/platform/log', async (_body, target) => ({
      entries: t.platform.entries
        .map((entry, seq) => ({ seq, entry }))
        .filter((item) => item.seq > Number(new URL(target, 'https://hub.example').searchParams.get('after') ?? -1)),
      head: { seq: t.platform.entries.length - 1, hash: await entryHash(t.platform.entries.at(-1)!) },
      next_after: null,
    }));
    expect(await syncAgents(context, runOf(t))).toBe(true);
    expect(types(t, t.world)).toEqual([['agent_add'], ['agent_add']]);
    expect(orgEntry(t.store.getState().orgs, t.org).log!.state.activeAgent(t.me.id)!.id).toBe(bot.id);
    // every epoch the browser holds is wrapped to it (the epoch the other client created follows by catch-up)
    const sent = t.client.of(`POST /v1/orgs/${t.org}/log`).at(-1)!.body as { wraps: Record<string, unknown>[] };
    expect(sent.wraps.filter((w) => w.recipient_id === bot.id).map((w) => [w.scope, w.epoch])).toEqual([
      ['org', 1],
      ['wg', 1],
    ]);
  });

  it('removes an agent the logs revoke even while the hub still lists it as active', async () => {
    const { t, rows, sync } = await setup();
    const bot = await actor('bot one');
    t.world.add(bot);
    await t.world.batch(t.me, [await agentAdd(t.me, bot)]);
    await t.world.batch(t.me, [{ type: 'remove', subject: bot.id }]);
    sync();
    rows.push(row(bot.id, 'active'));
    // removed other than with its user: never added again
    expect(await syncAgents(jobContext(t), runOf(t))).toBe(false);
  });

  it('does nothing without an identity, a verified log or after a sign-out, and is registered', async () => {
    const { t, rows } = await setup();
    rows.push(row((await actor('bot one')).id, 'active'));
    const context = jobContext(t);
    const stale = runOf(t, t.org, t.store.generation - 1);
    expect(await syncAgents(context, stale)).toBe(false);
    expect(await syncAgents(context, runOf(t, 'unknown org'))).toBe(false);
    setIdentity(t.store, null);
    expect(await syncAgents(context, runOf(t))).toBe(false);
    expect(t.client.of('GET /v1/me/agents')).toHaveLength(1);
    expect(job).toMatchObject({ name: 'agents', order: 33, triggers: ['login', 'log', 'interval'], retry: true });
    t.client.on('GET /v1/me/agents', () => ({ agents: null }));
    setIdentity(t.store, t.me.identity);
    await expect(job.run(context, runOf(t))).rejects.toThrow('unavailable');
  });
});

describe('agentsPlanner and agentAddOf', () => {
  const principal = (id: string, extra: Partial<Principal> = {}): Principal =>
    ({ id, kind: 'agent', parent: 'me', gen: 1, removedSeq: null, removedWithUser: false, ...extra }) as Principal;

  /** A verified log with these principals, `active` as the user's agent and `entries`. */
  const fake = (org: string, principals: Principal[], active: Principal | null, entries: unknown[] = []) =>
    ({
      state: { org, principals: new Map(principals.map((p) => [p.id, p])), activeAgent: () => active },
      entries,
    }) as unknown as VerifiedLog;

  const added = (subject: string, gen: number, user = 'me') => ({
    body: { type: 'agent_add', subject, user, gen, pub: { kem: 'k', sig: 's' }, cert: 'c' },
  });
  const hub = (active: { id: string; gen: number } | null, revoked: string[] = []) => ({
    revoked: new Set(revoked),
    active,
  });

  it('adds an agent that came back with its user and leaves every other case alone', () => {
    const elsewhere = fake('o2', [], null, [
      { body: { type: 'rotate' } },
      added('x', 2),
      added('b', 2, 'other'),
      added('b', 1),
      added('b', 2),
    ]);
    const back = principal('b', { gen: 2, removedSeq: 3, removedWithUser: true });
    const log = fake('o1', [back], null);
    expect(agentsPlanner(hub({ id: 'b', gen: 2 }), [log, elsewhere])(log, 'me')).toEqual({
      entries: [{ type: 'agent_add', subject: 'b', user: 'me', pub: { kem: 'k', sig: 's' }, gen: 2, cert: 'c' }],
    });
    // no hub agent; one revoked by the hub; one the log never names
    expect(agentsPlanner(hub(null), [log])(log, 'me')).toBeNull();
    expect(agentsPlanner(hub({ id: 'b', gen: 2 }, ['b']), [log])(log, 'me')).toBeNull();
    expect(agentsPlanner(hub({ id: 'b', gen: 2 }), [log])(log, 'me')).toBeNull();
    // the hub names an agent of another user of this log
    const foreign = principal('b', { parent: 'other', gen: 2 });
    const withForeign = fake('o1', [foreign], null);
    expect(agentsPlanner(hub({ id: 'b', gen: 2 }), [withForeign, elsewhere])(withForeign, 'me')).toBeNull();
    // the active agent is it, or is newer
    const active = principal('b', { gen: 2 });
    const withB = fake('o1', [active], active);
    expect(agentsPlanner(hub({ id: 'b', gen: 2 }), [withB])(withB, 'me')).toBeNull();
    const newer = principal('n', { gen: 3 });
    const withNewer = fake('o1', [newer], newer);
    expect(agentsPlanner(hub({ id: 'b', gen: 2 }), [withNewer, elsewhere])(withNewer, 'me')).toBeNull();
    // an older active agent is replaced by the add
    const older = principal('n', { gen: 1 });
    const withOlder = fake('o1', [older], older);
    expect(agentsPlanner(hub({ id: 'b', gen: 2 }), [withOlder, elsewhere])(withOlder, 'me')!.entries).toHaveLength(1);
  });

  it('reads the agent member of genesis and accept', async () => {
    const [user, bot] = [await actor('member user'), await actor('member bot')];
    const pub = bot.identity.public();
    const cert = await signCert(user.identity.sig.privateKey, { agent: bot.id, pub, user: user.id, gen: 1 });
    const org = await idOf('member org');
    const fields: LogFields = {
      type: 'genesis',
      subject: user.id,
      pub: user.identity.public(),
      default_wg: await idOf('member org wg'),
      private: true,
      agent: { id: bot.id, pub, gen: 1, cert },
    };
    const genesis = await signLogEntry(logBody({ org, head: null }, user.id, fields, TS), bot.identity.sig.privateKey);
    const log = await appendEntries(null, [genesis], user.identity.public().sig);
    expect(agentAddOf([log], user.id, bot.id, 1)).toEqual({
      type: 'agent_add',
      subject: bot.id,
      user: user.id,
      pub,
      gen: 1,
      cert,
    });
    expect(agentAddOf([log], user.id, bot.id, 2)).toBeNull();
  });
});
