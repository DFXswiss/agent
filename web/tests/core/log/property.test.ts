// Property "incremental = rebuild" (WCR-02 acceptance; the TypeScript twin of the CRY-02 property test): random
// organizations are built only through `buildBatch` (seeded choices among the operations that are allowed in the
// current state), then verified three ways – from scratch, incrementally page by page (`extendLog`, the reader) and
// batch by batch with hub semantics (`appendEntries`) – and every state projection, head and catch-up plan must be
// identical. Each batch also passes `checkBatch` with the hub's receipt time. Afterwards catch-up rounds by every admin
// (W3-checked like at the hub) close every gap an admin can close: what admins read by inheritance (ADR-058) arrives
// by catch-up, not necessarily in the batch.
import { describe, expect, it } from 'vitest';
import { CryptoError } from '../../../src/core/crypto/errors';
import type { LogFields } from '../../../src/core/log/entries';
import { buildWraps, compatSkip, missingWraps } from '../../../src/core/log/catchup';
import { checkBatch } from '../../../src/core/log/rotation';
import { appendEntries, extendLog, verifyLog, type VerifiedLog } from '../../../src/core/log/verify';
import { checkWrap, slotOf } from '../../../src/core/log/wraps';
import { actor, type Actor, EXPIRES, idOf, inviteFor, T0, World } from './world';

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

async function randomWorld(seed: number, steps: number): Promise<World> {
  const random = prng(seed);
  const pick = <T>(items: readonly T[]): T => items[Math.floor(random() * items.length)]!;
  const founder = await actor(`founder ${seed}`);
  const world = await World.create(founder);
  let counter = 0;
  for (let step = 0; step < steps; step += 1) {
    const state = world.state;
    const admins = state.admins().map((id) => world.actors.get(id)!);
    const author = pick(admins);
    const users = [...state.principals.values()].filter((p) => p.removedSeq === null && p.kind === 'user');
    const devices = [...state.principals.values()].filter((p) => p.removedSeq === null && p.kind === 'device');
    const wgs = [...state.workgroups.values()]
      .filter((wg) => wg.archivedSeq === null && !wg.isDefault)
      .map((wg) => wg.id);
    const options: (() => Promise<Step | null>)[] = [
      async () => {
        counter += 1;
        const user = await actor(`user ${seed} ${counter}`);
        world.add(user);
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
        const pairs = devices.flatMap((d) =>
          wgs.filter((wg) => !state.assigned(d.id, wg)).map((wg) => [d.id, wg] as const),
        );
        if (pairs.length === 0) return null;
        const [device, wg] = pick(pairs);
        return { author, entries: [{ type: 'device_assign', device, wg }] };
      },
      async () => {
        const pairs = devices.flatMap((d) => state.deviceWgs(d.id).map((wg) => [d.id, wg] as const));
        if (pairs.length === 0) return null;
        const [device, wg] = pick(pairs);
        return { author, entries: [{ type: 'device_unassign', device, wg }] };
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
      async () => {
        const leavers = users.filter((u) => u.role !== 'admin' || admins.length > 1);
        if (leavers.length === 0) return null;
        const leaver = world.actors.get(pick(leavers).id)!;
        await world.batch(leaver, [{ type: 'remove', subject: leaver.id }]);
        const remaining = world.state.admins().map((id) => world.actors.get(id)!);
        const pending = world.state.pendingScopes();
        const rotates: LogFields[] = pending.map(([scope, scopeId]) => ({
          type: 'rotate',
          scope: scope as 'org' | 'wg',
          scope_id: scopeId,
          epoch: world.state.epoch(scope, scopeId) + 1,
        }));
        return rotates.length === 0 ? null : { author: pick(remaining), entries: rotates };
      },
    ];
    const chosen = await pick(options)();
    if (chosen === null) continue;
    const history = random() < 0.3;
    // sometimes a second operation chosen on the same state joins the batch (several triggers, R as a union)
    const second = random() < 0.35 ? await pick(options.slice(2, 10))() : null;
    if (second !== null && second.author === chosen.author) {
      try {
        await world.batch(chosen.author, [...chosen.entries, ...second.entries], { history });
        combined += 1;
        continue;
      } catch (error) {
        if (!(error instanceof CryptoError)) throw error; // the pair conflicts: send the first alone
      }
    }
    await world.batch(chosen.author, chosen.entries, { history });
  }
  return world;
}

const view = (log: VerifiedLog) => log.states.map((state) => state.projection());

describe('property: incremental verification equals a rebuild', () => {
  const seen = new Set<string>();
  let total = 0;

  for (const seed of [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]) {
    it(`random org ${seed}`, async () => {
      const world = await randomWorld(seed, 18);
      const entries = world.log!.entries;
      const founder = world.log!.state.founder;
      const full = await verifyLog(entries, founder);
      for (const entry of entries) seen.add(entry.body.type as string);
      total += entries.length;
      // reader: pages of random size after the first entry
      const random = prng(seed * 7919);
      let reader = await verifyLog(entries.slice(0, 1), founder);
      while (reader.entries.length < entries.length) {
        const size = 1 + Math.floor(random() * 5);
        reader = await extendLog(reader, entries.slice(reader.entries.length, reader.entries.length + size));
      }
      // hub: the batches as they were sent, each checked with the receipt time
      let hub: VerifiedLog | null = null;
      for (const built of world.batches) {
        await checkBatch(hub, built.body.entries, built.body.wraps, built.body.entries[0]!.body.by as string, {
          founder,
          now: T0,
        });
        hub = await appendEntries(hub, built.body.entries, founder);
      }
      expect(view(reader)).toEqual(view(full));
      expect(view(hub!)).toEqual(view(full));
      expect(reader.head).toEqual(full.head);
      expect(world.log!.head).toEqual(full.head);
      // catch-up by every admin until nothing changes: what is still missing, no admin may wrap or holds the key of
      const open = (admin: string) => {
        const ring = world.ring(admin);
        const existing = world.wraps.map((w) => slotOf(w));
        return missingWraps(full, admin, existing).filter(
          (s) => ring(s.scope, s.scope_id, s.epoch) !== null && !compatSkip(full.state, admin, s),
        );
      };
      for (let round = 0; round < 4; round += 1) {
        for (const admin of full.state.admins()) {
          const wraps = await buildWraps(world.actors.get(admin)!.identity, open(admin), world.ring(admin), [
            full.state,
          ]);
          for (const wrap of wraps) await checkWrap(wrap, admin, full, [], true);
          await world.store(wraps);
        }
      }
      for (const admin of full.state.admins()) expect(open(admin)).toEqual([]);
    }, 60_000);
  }

  it('covered the generated entry types over substantial logs', () => {
    expect([...seen].sort()).toEqual([
      'accept',
      'device_assign',
      'device_unassign',
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
    expect(total).toBeGreaterThan(12 * 18);
    expect(combined).toBeGreaterThan(5);
  });
});
