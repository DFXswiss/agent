// Property "incremental = rebuild" (WCR-02 acceptance; the TypeScript twin of the CRY-02 property test): random
// organizations are built only through `buildBatch` (seeded choices among the operations that are allowed in the
// current state), then verified three ways – from scratch, incrementally page by page (`extendLog`, the reader) and
// batch by batch with hub semantics (`appendEntries`) – and every state projection, head and catch-up plan must be
// identical. Each batch also passes `checkBatch` with the hub's receipt time. Afterwards catch-up rounds by every admin
// (W3-checked like at the hub) close every gap an admin can close: what admins read by inheritance (ADR-058) arrives
// by catch-up, not necessarily in the batch.
//
// The generator emits every entry type that changes rights: invitations and their acceptance, direct admissions
// (`admit`, ADR-060), org and certified platform devices assigned to workgroups and to the whole organization and
// unassigned again, grants, roles, archives, removals and leaving with its rotations, the batches a decertified
// platform device makes owed (`decertificationBatch`'s entries), and agents as sub-principals of their users (ADR-082):
// added and replaced by a higher generation (`agent_add`), removed by their user or by themselves, acting for an admin
// user, and their users' rights changing under them (grants, roles, removals). The shortcuts of KOR-35
// (`epochHolders`, `credentialLosses`, `mayWrap`, `activeAgent`/`agentsOf`) are compared with reference
// implementations written here from the current definitions without them (they share none of its indexes or
// `changedPrincipals`).
import { describe, expect, it } from 'vitest';
import { signCert } from '../../../src/core/crypto/agent';
import { CryptoError } from '../../../src/core/crypto/errors';
import { ed25519KeyPair } from '../../../src/core/crypto/keys';
import { buildPlatformEntries, entryHash, type Entry, type LogFields } from '../../../src/core/log/entries';
import { buildWraps, compatSkip, missingWraps } from '../../../src/core/log/catchup';
import { type PlatformState, type VerifiedPlatformLog, verifyPlatformLog } from '../../../src/core/log/platform';
import { checkBatch, unassignAll } from '../../../src/core/log/rotation';
import { changedPrincipals, type OrgState, type Principal } from '../../../src/core/log/state';
import { appendEntries, extendLog, verifyLog, type VerifiedLog } from '../../../src/core/log/verify';
import { checkWrap, mayWrap, slotOf, type WrapSlot } from '../../../src/core/log/wraps';
import { credentialLosses, type Loss } from '../../../src/core/credentials/rotation';
import { actor, type Actor, EXPIRES, idOf, inviteFor, platformWith, seed as seedOf, T0, TS, World } from './world';

/** mulberry32: a small seeded PRNG (test choices only). */
function prng(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

type Step = { author: Actor; entries: LogFields[] };

/** Batches with more than one requested entry over all random orgs. */
let combined = 0;
/** Credential losses replayed over all random orgs and owners. */
let losses = 0;
/** Batches owed after a decertification over all random orgs. */
let decertified = 0;
/** Entries about agents (added, replaced, removed, signed by one) over all random orgs. */
let agentEntries = 0;
/** Rights of an active agent that changed within one epoch of a scope because its user's rights changed. */
let agentShifts = 0;
/** Entries after which `changedPrincipals` named the principals (not null), and agents named only through their user. */
let named = 0;
let namedThroughUser = 0;
/** `mayWrap` questions asked over all random orgs, and those answered with yes. */
let asked = 0;
let allowed = 0;

/** The platform log of a random org: certified platform devices and user identities, extended as the run goes. */
interface Platform {
  readonly root: string;
  readonly rootKey: CryptoKey;
  readonly entries: Entry[];
  log: VerifiedPlatformLog;
  /** `user -> seq` of its `user_certify`. */
  readonly users: Map<string, number>;
  /** `device -> seq` of its `device_certify`. */
  readonly devices: Map<string, number>;
}

/** Appends platform entries, verifies the log again and hands its state to the world; returns their `seq`s. */
async function extendPlatform(platform: Platform, world: World, list: { type: string; [key: string]: unknown }[]) {
  const last = platform.entries.at(-1)!;
  const head = { seq: last.body.seq as number, hash: await entryHash(last) };
  const added = await buildPlatformEntries(head, platform.rootKey, list, TS);
  platform.entries.push(...added);
  platform.log = await verifyPlatformLog(platform.entries, platform.root);
  world.platform = platform.log.state;
  return added.map((entry) => entry.body.seq as number);
}

async function certifyUser(platform: Platform, world: World, user: Actor): Promise<void> {
  const [seq] = await extendPlatform(platform, world, [
    { type: 'user_certify', user: user.id, pub: user.identity.public() },
  ]);
  platform.users.set(user.id, seq!);
}

interface Run {
  readonly world: World;
  readonly platform: Platform;
  /** The platform state each batch of `world.batches` was built with. */
  readonly platforms: PlatformState[];
}

async function randomWorld(seed: number, steps: number): Promise<Run> {
  const random = prng(seed);
  const pick = <T>(items: readonly T[]): T => items[Math.floor(random() * items.length)]!;
  const founder = await actor(`founder ${seed}`);
  const machines = await Promise.all([1, 2, 3].map((n) => actor(`platform device ${seed} ${n}`)));
  const outsiders = await Promise.all([1, 2, 3].map((n) => actor(`admitted ${seed} ${n}`)));
  const made = await platformWith(machines);
  const world = await World.create(founder);
  world.add(...machines, ...outsiders);
  const platform: Platform = {
    root: made.root,
    rootKey: (await ed25519KeyPair(await seedOf('platform root'))).privateKey,
    entries: [...made.entries],
    log: await verifyPlatformLog(made.entries, made.root),
    users: new Map(),
    devices: made.seqs,
  };
  world.platform = platform.log.state;
  for (const user of [founder, ...outsiders]) await certifyUser(platform, world, user);
  const platforms: PlatformState[] = [];
  const settle = () => {
    while (platforms.length < world.batches.length) platforms.push(world.platform!);
  };
  settle();
  let counter = 0;
  for (let step = 0; step < steps; step += 1) {
    const state = world.state;
    const plat = world.platform!;
    const admins = state.admins().map((id) => world.actors.get(id)!);
    const author = pick(admins);
    const users = [...state.principals.values()].filter((p) => p.removedSeq === null && p.kind === 'user');
    const devices = [...state.principals.values()].filter((p) => p.removedSeq === null && p.kind === 'device');
    const wgs = [...state.workgroups.values()]
      .filter((wg) => wg.archivedSeq === null && !wg.isDefault)
      .map((wg) => wg.id);
    const writable = [state.defaultWg, ...wgs];
    /** Platform devices that may be assigned now: certified, and a member only with that certification. */
    const certified = machines.filter((m) => {
      const seq = platform.devices.get(m.id)!;
      const member = state.member(m.id);
      return plat.valid(seq) && (member === null || member.platformSeq === seq);
    });
    const platformSeq = (device: string) =>
      platform.devices.has(device) ? { platform_seq: platform.devices.get(device)! } : {};
    /** Active agents of active users, and the users that may get one. */
    const agents = [...state.principals.values()].filter((p) => p.kind === 'agent' && state.member(p.id) !== null);
    const nextGen = (user: string) =>
      1 + Math.max(0, ...[...state.principals.values()].filter((p) => p.parent === user).map((p) => p.gen!));
    const solo: (() => Promise<null>)[] = [
      async () => {
        // a user adds an agent (replacing its active one), or the agent adds itself with the user's certificate
        if (users.length === 0) return null;
        const user = world.actors.get(pick(users).id)!;
        counter += 1;
        const agent = await actor(`agent ${seed} ${counter}`);
        world.add(agent);
        const gen = nextGen(user.id);
        const pub = agent.identity.public();
        const cert = await signCert(user.identity.sig.privateKey, { agent: agent.id, pub, user: user.id, gen });
        const fields: LogFields = { type: 'agent_add', subject: agent.id, user: user.id, pub, gen, cert };
        await world.batch(user, [fields]);
        agentEntries += 1;
        return null;
      },
      async () => {
        // an agent is removed by its user or removes itself (a revocation: the user owes what it read)
        if (agents.length === 0) return null;
        const agent = pick(agents);
        const by = world.actors.get(random() < 0.5 ? agent.parent! : agent.id)!;
        await world.batch(by, [{ type: 'remove', subject: agent.id }]);
        agentEntries += 1;
        return null;
      },
      async () => {
        // the agent of an admin acts for it: a workgroup (the access goes to the user) or a grant
        const acting = agents.filter((a) => state.isAdmin(a.id));
        if (acting.length === 0) return null;
        const agent = world.actors.get(pick(acting).id)!;
        const pairs = users.flatMap((u) => wgs.map((wg) => [u.id, wg] as const));
        if (pairs.length > 0 && random() < 0.5) {
          const [subject, wg] = pick(pairs);
          const access = state.access.get(`${subject} ${wg}`) === 'write' ? 'read' : 'write';
          await world.batch(agent, [{ type: 'grant', subject, wg, access }]);
        } else {
          await world.batch(agent, [{ type: 'wg_create', wg: await idOf(`wg ${seed} ${(counter += 1)}`), epoch: 1 }]);
        }
        agentEntries += 1;
        return null;
      },
      async () => {
        counter += 1;
        const user = await actor(`user ${seed} ${counter}`);
        world.add(user);
        await certifyUser(platform, world, user);
        const chosen = wgs.filter(() => random() < 0.5);
        const secrets = await inviteFor(`p ${seed} ${counter}`);
        await world.batch(author, [
          {
            type: 'invite',
            invite_id: secrets.inviteId,
            pk_inv: secrets.pkInv,
            kind: 'user',
            role: random() < 0.3 ? 'admin' : 'member',
            wgs: Object.fromEntries(chosen.map((wg) => [wg, random() < 0.5 ? 'read' : 'write'])),
            expires: EXPIRES,
          },
        ]);
        await world.batch(
          user,
          [
            {
              type: 'accept',
              invite_id: secrets.inviteId,
              subject: user.id,
              kind: 'user',
              pub: user.identity.public(),
            },
          ],
          { invite: secrets.signing.privateKey, keys: world.ring(author.id) },
        );
        return null;
      },
      async () => {
        counter += 1;
        const device = await actor(`device ${seed} ${counter}`);
        await world.admitDevice(author, device, [world.defaultWg, ...wgs.filter(() => random() < 0.5)]);
        return null;
      },
      async () => {
        const leavers = users.filter((u) => u.role !== 'admin' || admins.length > 1);
        if (leavers.length === 0) return null;
        const leaver = world.actors.get(pick(leavers).id)!;
        await world.batch(leaver, [{ type: 'remove', subject: leaver.id }]);
        const remaining = world.state.admins().map((id) => world.actors.get(id)!);
        const rotates: LogFields[] = world.state.pendingScopes().map(([scope, scopeId]) => ({
          type: 'rotate',
          scope: scope as 'org' | 'wg',
          scope_id: scopeId,
          epoch: world.state.epoch(scope, scopeId) + 1,
        }));
        if (rotates.length > 0) await world.batch(pick(remaining), rotates);
        return null;
      },
      async () => {
        // the operator decertifies a platform device of the org; an admin sends what that owes
        const members = machines.filter((m) => {
          const member = state.member(m.id);
          return member !== null && plat.valid(member.platformSeq!);
        });
        if (members.length === 0) return null;
        const device = pick(members);
        await extendPlatform(platform, world, [{ type: 'device_decertify', device: device.id }]);
        const owed = world.platform!.excluded(state).flatMap((id) => unassignAll(state, id));
        if (owed.length > 0) {
          await world.batch(author, owed);
          decertified += 1;
        }
        return null;
      },
    ];
    const combinable: (() => Promise<Step | null>)[] = [
      async () => ({
        author,
        entries: [{ type: 'wg_create', wg: await idOf(`wg ${seed} ${(counter += 1)}`), epoch: 1 }],
      }),
      async () => {
        const pairs = users.flatMap((u) => wgs.map((wg) => [u.id, wg] as const));
        if (pairs.length === 0) return null;
        const [subject, wg] = pick(pairs);
        const access = state.access.get(`${subject} ${wg}`) === 'write' ? 'read' : 'write';
        return { author, entries: [{ type: 'grant', subject, wg, access }] };
      },
      async () => {
        const grants = [...state.access.keys()].map((key) => key.split(' ') as [string, string]);
        if (grants.length === 0) return null;
        const [subject, wg] = pick(grants);
        return { author, entries: [{ type: 'ungrant', subject, wg }] };
      },
      async () => {
        // org devices, and certified platform devices (members or not yet) with their certification
        const candidates = [
          ...devices.filter((d) => d.platformSeq === null).map((d) => d.id),
          ...certified.map((m) => m.id),
        ];
        const pairs = candidates.flatMap((d) =>
          state.orgAssignments.has(d)
            ? []
            : writable.filter((wg) => !state.assignments.has(`${d} ${wg}`)).map((wg) => [d, wg] as const),
        );
        if (pairs.length === 0) return null;
        const [device, wg] = pick(pairs);
        return { author, entries: [{ type: 'device_assign', device, wg, ...platformSeq(device) }] };
      },
      async () => {
        const pairs = [...state.assignments.keys()].map((key) => key.split(' ') as [string, string]);
        if (pairs.length === 0) return null;
        const [device, wg] = pick(pairs);
        return { author, entries: [{ type: 'device_unassign', device, wg }] };
      },
      async () => {
        const candidates = [
          ...devices.filter((d) => d.platformSeq === null).map((d) => d.id),
          ...certified.map((m) => m.id),
        ].filter((d) => !state.orgAssignments.has(d));
        if (candidates.length === 0) return null;
        const device = pick(candidates);
        return { author, entries: [{ type: 'device_assign_org', device, ...platformSeq(device) }] };
      },
      async () => {
        const assigned = [...state.orgAssignments.keys()];
        if (assigned.length === 0) return null;
        return { author, entries: [{ type: 'device_unassign_org', device: pick(assigned) }] };
      },
      async () => {
        const candidates = outsiders.filter((o) => state.member(o.id) === null);
        if (candidates.length === 0) return null;
        const subject = pick(candidates);
        return {
          author,
          entries: [
            {
              type: 'admit',
              subject: subject.id,
              pub: subject.identity.public(),
              platform_seq: platform.users.get(subject.id)!,
              id_sig: '',
              by_platform_seq: platform.users.get(author.id)!,
              by_id_sig: '',
              role: random() < 0.3 ? 'admin' : 'member',
              wgs: Object.fromEntries(wgs.filter(() => random() < 0.5).map((wg) => [wg, pick(['read', 'write'])])),
            },
          ],
        };
      },
      async () => (wgs.length === 0 ? null : { author, entries: [{ type: 'wg_archive', wg: pick(wgs) }] }),
      async () => {
        const candidates = users.filter((u) => u.role !== 'admin' || admins.length > 1);
        if (candidates.length === 0) return null;
        const subject = pick(candidates);
        return {
          author,
          entries: [{ type: 'role', subject: subject.id, role: subject.role === 'admin' ? 'member' : 'admin' }],
        };
      },
      async () => {
        const candidates = [...users, ...devices].filter(
          (p) => p.id !== author.id && (p.role !== 'admin' || admins.length > 1),
        );
        if (candidates.length === 0) return null;
        return { author, entries: [{ type: 'remove', subject: pick(candidates).id }] };
      },
    ];
    const index = Math.floor(random() * (solo.length + combinable.length));
    const chosen = index < solo.length ? await solo[index]!() : await combinable[index - solo.length]!();
    settle();
    if (chosen === null) continue;
    const history = random() < 0.3;
    // sometimes a second operation chosen on the same state joins the batch (several triggers, R as a union)
    const second = random() < 0.35 ? await pick(combinable)() : null;
    if (second !== null && second.author === chosen.author) {
      try {
        await world.batch(chosen.author, [...chosen.entries, ...second.entries], { history });
        combined += 1;
        settle();
        continue;
      } catch (error) {
        if (!(error instanceof CryptoError)) throw error; // the pair conflicts: send the first alone
      }
    }
    await world.batch(chosen.author, chosen.entries, { history });
    settle();
  }
  return { world, platform, platforms };
}

// --- reference implementations: the definitions without the shortcuts of KOR-35, inline ---

/** `OrgState.agentsOf` without the index: a scan of the principals. */
function agentsOfDef(state: OrgState, user: string): Principal[] {
  return [...state.principals.values()].filter((p) => p.parent === user);
}

/** `OrgState.activeAgent` without the index: the first active agent of the user in the principals. */
function activeAgentDef(state: OrgState, user: string): Principal | null {
  return [...state.principals.values()].find((p) => p.parent === user && state.member(p.id) !== null) ?? null;
}

/** `wraps.revokedAgents`: superseded by a higher generation of the same user, or removed by its owner. */
function revokedAgentsDef(states: Iterable<OrgState>): Set<string> {
  const agents = [...states].flatMap((state) => [...state.principals.values()].filter((p) => p.kind === 'agent'));
  return new Set(
    agents
      .filter((a) => a.removedByOwner || agents.some((other) => other.parent === a.parent && other.gen! > a.gen!))
      .map((a) => a.id),
  );
}

/** `OrgState.deviceWgs` without the index: a scan of the assignments. */
function deviceWgsDef(state: OrgState, device: string): string[] {
  if (state.member(device) === null) return [];
  if (state.orgAssignments.has(device)) return [...state.workgroups.keys()].sort();
  return [...state.assignments.keys()]
    .map((key) => key.split(' ') as [string, string])
    .filter(([dev]) => dev === device)
    .map(([, wg]) => wg)
    .sort();
}

/** `OrgState.canRead` without the index (an agent reads what its user reads, through `acting`). */
function canReadDef(state: OrgState, principal: string, scope: string, scopeId: string): boolean {
  const found = state.member(principal);
  if (found === null) return false;
  const device = found.kind === 'device';
  if (scope === 'org') return scopeId === state.org && (!device || deviceWgsDef(state, principal).length > 0);
  if (!device) return state.wgAccess(principal, scopeId) !== null;
  return state.assigned(principal, scopeId);
}

/** `OrgState.readers` before KOR-35. */
function readersDef(state: OrgState, scope: string, scopeId: string, excluded: readonly string[]): string[] {
  const skip = new Set(excluded);
  return [...state.principals.keys()].filter((p) => !skip.has(p) && canReadDef(state, p, scope, scopeId)).sort();
}

/** `VerifiedLog.epochHolders` without `changedPrincipals`: every principal of every state of the epoch, and the epoch's author. */
function holdersDef(log: VerifiedLog, scope: string, scopeId: string, epoch: number): Set<string> {
  const holders = new Set<string>();
  for (const state of log.states) {
    if (state.epoch(scope, scopeId) !== epoch) continue;
    for (const principal of state.principals.keys()) {
      if (canReadDef(state, principal, scope, scopeId)) holders.add(principal);
    }
  }
  const author = log.state.epochAuthors.get(`${scope} ${scopeId} ${epoch}`);
  if (author !== undefined) holders.add(author);
  return holders;
}

/** `mayWrap` without the shortcuts (`holders` memoizes `holdersDef` per epoch within one log). */
function mayWrapDef(
  log: VerifiedLog,
  slot: WrapSlot,
  wrappedBy: string,
  excluded: readonly string[],
  holders: (scope: string, scopeId: string, epoch: number) => Set<string>,
): boolean {
  const state = log.state;
  if (!(slot.scope === 'org' || slot.scope === 'wg')) return false;
  if (!(slot.epoch >= 1 && slot.epoch <= state.epoch(slot.scope, slot.scope_id))) return false;
  const readers = readersDef(state, slot.scope, slot.scope_id, excluded);
  if (!readers.includes(slot.recipient)) return false;
  return (
    wrappedBy === slot.recipient ||
    holders(slot.scope, slot.scope_id, slot.epoch).has(wrappedBy) ||
    readers.includes(wrappedBy)
  );
}

/** `credReaders` without the shortcuts: the owner, its active agent unless revoked, and its devices. */
function credReadersDef(states: Iterable<OrgState>, owner: string, excluded: Iterable<string> = []): Set<string> {
  const skip = new Set(excluded);
  const list = [...states];
  const org = list.find((state) => state.org === owner);
  if (org !== undefined) {
    return new Set(
      [...org.principals.values()]
        .filter((device) => device.kind === 'device' && !skip.has(device.id) && deviceWgsDef(org, device.id).length > 0)
        .map((device) => device.id),
    );
  }
  const found = new Set([owner]);
  const revoked = revokedAgentsDef(list);
  for (const state of list) {
    const user = state.member(owner);
    if (user === null) continue;
    const agent = activeAgentDef(state, owner);
    if (agent !== null && !revoked.has(agent.id)) found.add(agent.id);
    if (!user.consented || (user.joinAgent !== null && revoked.has(user.joinAgent))) continue;
    for (const device of state.principals.values()) {
      const eligible = deviceWgsDef(state, device.id).some((wg) => state.explicitAccess(owner, wg) !== null);
      if (device.kind === 'device' && !skip.has(device.id) && eligible) found.add(device.id);
    }
  }
  return found;
}

/** `credentialLosses` without the replay: the readers before and after every entry, and the decertified devices. */
function lossesDef(log: VerifiedLog, platform: VerifiedPlatformLog, owner: string): [string, Loss][] {
  const excluded = platform.state.excluded(log.state);
  const now = credReadersDef([log.state], owner, excluded);
  const losses = new Map<string, Loss>();
  const note = (device: string, at: number) => {
    if (!now.has(device) && at > (losses.get(device)?.at ?? -Infinity))
      losses.set(device, { at, key: `${device} ${at}` });
  };
  for (let seq = 1; seq < log.states.length; seq += 1) {
    const after = credReadersDef([log.states[seq]!], owner);
    for (const device of credReadersDef([log.states[seq - 1]!], owner)) {
      if (!after.has(device)) note(device, Date.parse(log.entries[seq]!.body.ts as string));
    }
  }
  for (const device of credReadersDef([log.state], owner)) {
    if (now.has(device)) continue;
    const newest = platform.entries
      .filter((entry) => entry.body.type === 'device_decertify' && entry.body.device === device)
      .at(-1);
    if (newest !== undefined) note(device, Date.parse(newest.body.ts as string));
  }
  return [...losses];
}

const view = (log: VerifiedLog) => log.states.map((state) => state.projection());

describe('property: incremental verification equals a rebuild', () => {
  const seen = new Set<string>();
  let total = 0;

  for (const seed of [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]) {
    it(`random org ${seed}`, async () => {
      const { world, platform, platforms } = await randomWorld(seed, 24);
      const plat = platform.log.state;
      const entries = world.log!.entries;
      const founder = world.log!.state.founder;
      const full = await verifyLog(entries, founder, plat);
      for (const entry of entries) seen.add(entry.body.type as string);
      total += entries.length;
      // reader: pages of random size after the first entry
      const random = prng(seed * 7919);
      let reader = await verifyLog(entries.slice(0, 1), founder, plat);
      while (reader.entries.length < entries.length) {
        const size = 1 + Math.floor(random() * 5);
        reader = await extendLog(reader, entries.slice(reader.entries.length, reader.entries.length + size), plat);
      }
      // hub: the batches as they were sent, each checked with the receipt time and the platform state of its time
      let hub: VerifiedLog | null = null;
      for (const [index, built] of world.batches.entries()) {
        const then = platforms[index]!;
        await checkBatch(hub, built.body.entries, built.body.wraps, built.body.entries[0]!.body.by as string, {
          founder,
          now: T0,
          platform: then,
          platformAccess: true,
          excluded: then.excluded(built.log.state),
        });
        hub = await appendEntries(hub, built.body.entries, founder, { platform: plat });
      }
      expect(view(reader)).toEqual(view(full));
      // the shortcuts of KOR-35 answer like the code before it, on every scope, epoch, owner and slot
      const memo = new Map<string, Set<string>>();
      const holders = (scope: string, scopeId: string, epoch: number) => {
        const key = `${scope} ${scopeId} ${epoch}`;
        if (!memo.has(key)) memo.set(key, holdersDef(full, scope, scopeId, epoch));
        return memo.get(key)!;
      };
      for (const [scope, scopeId] of full.state.scopes()) {
        for (let epoch = 1; epoch <= full.state.epoch(scope, scopeId); epoch += 1) {
          expect([...full.epochHolders(scope, scopeId, epoch)].sort()).toEqual(
            [...holders(scope, scopeId, epoch)].sort(),
          );
        }
      }
      // the agent lookups answer like a scan, in every state, for every principal (an index of a later state too)
      for (const state of full.states) {
        for (const user of full.state.principals.keys()) {
          expect(state.agentsOf(user)).toEqual(agentsOfDef(state, user));
          expect(state.activeAgent(user)).toEqual(activeAgentDef(state, user));
        }
      }
      // an agent whose own entry stayed while what it reads changed (its user's rights changed under it); and the
      // contract of `changedPrincipals`: a principal it does not name has the same rights in both states
      const rights = (state: OrgState, p: string) => ({
        member: state.member(p),
        acting: state.acting(p),
        admin: state.isAdmin(p),
        wgs: [...state.workgroups.keys()].map((wg) => [state.wgAccess(p, wg), state.canWriteWg(p, wg)]),
        devices: deviceWgsDef(state, p),
        read: state.scopes().map(([scope, id]) => canReadDef(state, p, scope, id)),
        owed: state.scopes().filter(([scope, id]) => state.agentOwed.get(`${scope} ${id}`) === state.acting(p)),
      });
      for (let seq = 1; seq < full.states.length; seq += 1) {
        const [before, after] = [full.states[seq - 1]!, full.states[seq]!];
        const changed = changedPrincipals(before, after);
        if (changed !== null) {
          named += 1;
          for (const p of after.principals.keys()) {
            if (!changed.has(p)) expect(rights(after, p)).toEqual(rights(before, p));
            else if (after.principals.get(p)?.kind === 'agent' && before.principals.get(p) === after.principals.get(p))
              namedThroughUser += 1;
          }
        }
        for (const agent of after.principals.values()) {
          if (agent.kind !== 'agent' || before.principals.get(agent.id) !== agent) continue;
          const shifted = after
            .scopes()
            .some(([scope, id]) => canReadDef(before, agent.id, scope, id) !== canReadDef(after, agent.id, scope, id));
          if (shifted) agentShifts += 1;
        }
      }
      for (const owner of [...full.state.principals.keys(), full.state.org]) {
        const expected = lossesDef(full, platform.log, owner);
        expect([...credentialLosses([full], platform.log, owner)]).toEqual(expected);
        losses += expected.length;
      }
      const principals = [...full.state.principals.keys(), 'unknown'];
      const exclusions = [[], plat.excluded(full.state), principals.filter(() => random() < 0.3)];
      const scopes = [...full.state.scopes(), ['wg', 'unknown'] as const, ['cred', founder] as const];
      for (const [scope, scopeId] of scopes) {
        for (let epoch = 0; epoch <= full.state.epoch(scope, scopeId) + 1; epoch += 1) {
          for (const recipient of principals) {
            const slot = { scope, scope_id: scopeId, epoch, recipient };
            for (const excluded of exclusions) {
              for (const signer of principals) {
                const expected = mayWrapDef(full, slot, signer, excluded, holders);
                if (mayWrap(full, slot, signer, excluded) !== expected) {
                  expect({ slot, signer, excluded, allowed: !expected }).toEqual({
                    slot,
                    signer,
                    excluded,
                    allowed: expected,
                  });
                }
                asked += 1;
                if (expected) allowed += 1;
              }
            }
          }
        }
      }
      expect(view(hub!)).toEqual(view(full));
      expect(reader.head).toEqual(full.head);
      expect(world.log!.head).toEqual(full.head);
      // catch-up by every admin until nothing changes: what is still missing, no admin may wrap or holds the key of
      const excluded = plat.excluded(full.state);
      const open = (admin: string) => {
        const ring = world.ring(admin);
        const existing = world.wraps.map((w) => slotOf(w));
        return missingWraps(full, admin, existing, excluded).filter(
          (s) => ring(s.scope, s.scope_id, s.epoch) !== null && !compatSkip(full.state, admin, s),
        );
      };
      for (let round = 0; round < 4; round += 1) {
        for (const admin of full.state.admins()) {
          const wraps = await buildWraps(world.actors.get(admin)!.identity, open(admin), world.ring(admin), [
            full.state,
          ]);
          for (const wrap of wraps) await checkWrap(wrap, admin, full, excluded, true);
          await world.store(wraps);
        }
      }
      for (const admin of full.state.admins()) expect(open(admin)).toEqual([]);
    }, 120_000);
  }

  it('covered every entry type that changes rights over substantial logs', () => {
    expect(losses).toBeGreaterThan(0);
    expect(decertified).toBeGreaterThan(0);
    expect(agentEntries).toBeGreaterThan(12);
    expect(agentShifts).toBeGreaterThan(0);
    expect(named).toBeGreaterThan(100);
    expect(namedThroughUser).toBeGreaterThan(0);
    expect(allowed).toBeGreaterThan(0);
    expect(asked).toBeGreaterThan(allowed);
    expect([...seen].sort()).toEqual([
      'accept',
      'admit',
      'agent_add',
      'device_assign',
      'device_assign_org',
      'device_unassign',
      'device_unassign_org',
      'genesis',
      'grant',
      'invite',
      'remove',
      'role',
      'rotate',
      'ungrant',
      'wg_archive',
      'wg_create',
    ]);
    expect(total).toBeGreaterThan(12 * 24);
    expect(combined).toBeGreaterThan(5);
  });
});
