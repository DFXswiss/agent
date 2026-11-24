// The batch builder (06-web.md section 7.1, section 11 job 4; 04-crypto.md sections 4.7 and 5.10): genesis,
// workgroup creation with grants and assignments, removal/ungrant/unassign with exactly R as `rotate` entries and
// wraps of every new epoch for all remaining recipients, leave with a pending rotation, grants with history,
// platform devices, `seq_conflict` recomputation and the refusals of `checkBatch`.
import { beforeAll, describe, expect, it, vi } from 'vitest';
import { b64uEncode } from '../../../src/core/encoding/b64u';
import { CryptoError } from '../../../src/core/crypto/errors';
import { buildEntries } from '../../../src/core/log/entries';
import { appendEntries } from '../../../src/core/log/verify';
import {
  asJson,
  buildBatch,
  checkBatch,
  entrySubject,
  createOrgBody,
  decertificationBatch,
  unassignAll,
  pendingRotation,
} from '../../../src/core/log/rotation';
import { slotJson } from '../../../src/core/log/wraps';
import { actor, actors, type Actor, idOf, platformWith, TS, World } from './world';

const code = async (promise: Promise<unknown>) => {
  try {
    await promise;
  } catch (error) {
    if (error instanceof CryptoError) return error.code;
    throw error;
  }
  return null;
};

let alice: Actor, bob: Actor, carol: Actor, dave: Actor, device: Actor, dev2: Actor;

beforeAll(async () => {
  [alice, bob, carol, dave, device, dev2] = await actors('alice', 'bob', 'carol', 'dave', 'device', 'dev2');
});

describe('genesis batch (POST /v1/orgs)', () => {
  it('creates the org and default workgroup keys and wraps both for the founder', async () => {
    const world = await World.create(alice);
    const built = world.batches[0]!;
    expect(built.body.expected_seq).toBe(0);
    expect(built.body.entries).toHaveLength(1);
    expect(built.newKeys.map((k) => [k.scope, k.scope_id, k.epoch])).toEqual([
      ['org', world.org, 1],
      ['wg', world.defaultWg, 1],
    ]);
    expect(built.body.wraps.map((w) => [w.scope, w.recipient_id])).toEqual([
      ['org', alice.id],
      ['wg', alice.id],
    ]);
    expect(built.required).toEqual([]);
    expect(world.currentKey(alice.id, 'org', world.org)).toBe(built.newKeys[0]!.key.reveal(b64uEncode));
  });

  it('creates a new org with genesis alone and maps it to the POST /v1/orgs body', async () => {
    const world = await World.create(alice);
    const built = world.batches[0]!;
    expect(createOrgBody(built)).toEqual({ genesis: built.body.entries[0], wraps: built.body.wraps, events: [] });
    expect(createOrgBody(built, 'inv').platform_invite_id).toBe('inv');
    const pub = alice.identity.public();
    const genesis = { type: 'genesis' as const, subject: alice.id, pub, default_wg: await idOf('w'), private: true };
    const more = [genesis, { type: 'wg_create' as const, wg: await idOf('x'), epoch: 1 as const }];
    await expect(code(buildBatch({ log: null, org: world.org, author: alice, entries: more }))).resolves.toBe(
      'log_genesis',
    );
  });

  it('needs the org ID for a new org', async () => {
    const pub = alice.identity.public();
    const genesis = { type: 'genesis' as const, subject: alice.id, pub, default_wg: await idOf('w'), private: true };
    await expect(code(buildBatch({ log: null, author: alice, entries: [genesis] }))).resolves.toBe('log_genesis');
  });

  it('defaults the time and the key source', async () => {
    const pub = alice.identity.public();
    const genesis = { type: 'genesis' as const, subject: alice.id, pub, default_wg: await idOf('w'), private: true };
    const built = await buildBatch({ log: null, org: await idOf('o'), author: alice, entries: [genesis] });
    expect(built.newKeys.every((k) => k.key.length === 32)).toBe(true);
    expect(typeof built.body.entries[0]!.body.ts).toBe('string');
  });
});

describe('membership batches', () => {
  let world: World;
  let wg: string;

  beforeAll(async () => {
    world = await World.create(alice);
    await world.admitUser(alice, bob);
    await world.admitUser(alice, carol, 'admin');
    await world.admitUser(alice, dave);
    await world.admitDevice(alice, device, [world.defaultWg]);
    world.add(dev2);
    wg = await idOf('wg one');
  });

  it('gives the invitee the current keys by its own self-wraps at accept', () => {
    expect(world.currentKey(bob.id, 'org', world.org)).toBe(world.currentKey(alice.id, 'org', world.org));
    expect(world.currentKey(device.id, 'wg', world.defaultWg)).toBe(world.currentKey(alice.id, 'wg', world.defaultWg));
    const accept = world.batches.find((b) => b.body.entries[0]!.body.type === 'accept')!;
    expect(accept.body.wraps.every((w) => w.recipient_id === accept.body.entries[0]!.body.by)).toBe(true);
  });

  it('creates a workgroup with grants and an assignment in one batch (wraps of WK_1 for all recipients)', async () => {
    const built = await world.batch(
      alice,
      [
        { type: 'wg_create', wg, epoch: 1 },
        { type: 'grant', subject: bob.id, wg, access: 'write' },
        { type: 'device_assign', device: device.id, wg },
      ],
      { events: [{ marker: 1 }] },
    );
    expect(built.newKeys.map((k) => k.scope_id)).toEqual([wg]);
    // the other admin (carol) reads every workgroup by inheritance (ADR-058) and gets WK_1 as well
    expect(new Set(built.body.wraps.map((w) => w.recipient_id))).toEqual(
      new Set([alice.id, bob.id, carol.id, device.id]),
    );
    expect(built.body.events).toEqual([{ marker: 1 }]);
    for (const who of [alice, bob, carol, device]) expect(world.currentKey(who.id, 'wg', wg)).not.toBeNull();
    expect(world.currentKey(dave.id, 'wg', wg)).toBeNull();
  });

  it('wraps the org key for a device at its first assignment (ADR-007)', async () => {
    await world.admitDevice(alice, dev2, []);
    expect(world.currentKey(dev2.id, 'org', world.org)).toBeNull();
    const built = await world.batch(alice, [{ type: 'device_assign', device: dev2.id, wg }]);
    expect(built.newKeys).toEqual([]);
    expect(built.body.wraps.map((w) => [w.scope, w.recipient_id])).toEqual([
      ['org', dev2.id],
      ['wg', dev2.id],
    ]);
    expect(world.currentKey(dev2.id, 'org', world.org)).toBe(world.currentKey(alice.id, 'org', world.org));
  });

  it('rotates exactly R after a device loses its last workgroup and gives the new keys to the rest', async () => {
    const oldOrg = world.currentKey(alice.id, 'org', world.org);
    const built = await world.batch(alice, [{ type: 'device_unassign', device: dev2.id, wg }]);
    expect(built.required).toEqual(asJson([`org ${world.org}`, `wg ${wg}`]));
    const types = built.body.entries.map((e) => e.body.type);
    expect(types).toEqual(['device_unassign', 'rotate', 'rotate']);
    expect(world.currentKey(alice.id, 'org', world.org)).not.toBe(oldOrg);
    expect(world.currentKey(bob.id, 'org', world.org)).toBe(world.currentKey(alice.id, 'org', world.org));
    expect(world.currentKey(device.id, 'wg', wg)).toBe(world.currentKey(alice.id, 'wg', wg));
    expect(world.currentKey(dev2.id, 'org', world.org)).toBeNull();
  });

  it('grants with history: wraps of every older epoch of that workgroup for that subject only', async () => {
    const built = await world.batch(
      alice,
      [
        { type: 'grant', subject: dave.id, wg, access: 'read' },
        { type: 'device_assign', device: dev2.id, wg },
      ],
      { history: true },
    );
    expect(built.body.wraps.map((w) => [w.scope, w.epoch, w.recipient_id])).toEqual([
      ['org', 2, dev2.id],
      ['wg', 1, dave.id],
      ...[dave.id, dev2.id].sort().map((id) => ['wg', 2, id]),
    ]);
    await world.batch(alice, [{ type: 'device_unassign', device: dev2.id, wg }]);
  });

  it('makes the read of an admin explicit with a grant (no wraps: it reads already)', async () => {
    const built = await world.batch(alice, [{ type: 'grant', subject: carol.id, wg, access: 'read' }]);
    expect(built.body.wraps).toEqual([]);
    expect(world.state.inherited(carol.id, wg)).toBe(false);
    expect(world.state.explicitAccess(carol.id, wg)).toBe('read');
  });

  it('ungrants and removes with one rotate per element of R, in order after the triggers', async () => {
    // an admin's explicit access goes, the inherited read stays: nothing to rotate
    const admin = await world.batch(alice, [{ type: 'ungrant', subject: carol.id, wg }]);
    expect(admin.required).toEqual([]);
    expect(world.state.inherited(carol.id, wg)).toBe(true);
    const ungrant = await world.batch(alice, [{ type: 'ungrant', subject: dave.id, wg }]);
    expect(ungrant.required).toEqual([{ scope: 'wg', scope_id: wg }]);
    const removal = await world.batch(alice, [{ type: 'remove', subject: bob.id }]);
    expect(removal.required).toEqual(asJson([`org ${world.org}`, `wg ${world.defaultWg}`, `wg ${wg}`]));
    expect(removal.body.entries.map((e) => e.body.type)).toEqual(['remove', 'rotate', 'rotate', 'rotate']);
    expect(removal.body.wraps.some((w) => w.recipient_id === bob.id)).toBe(false);
    expect(world.currentKey(bob.id, 'wg', wg)).toBeNull();
  });

  it('leaves without rotation (pending) and lets an admin rotate the pending scopes', async () => {
    const leave = await world.batch(carol, [{ type: 'remove', subject: carol.id }]);
    expect(leave.required).toEqual([]);
    expect(leave.body.wraps).toEqual([]);
    // the admin read every workgroup: all of them are owed
    expect(world.state.pendingScopes()).toEqual([
      ['org', world.org],
      ...[world.defaultWg, wg].sort().map((id) => ['wg', id]),
    ]);
    const memberJob = pendingRotation(world.log!, { author: device });
    expect(memberJob).toBeNull();
    const job = (await pendingRotation(world.log!, { author: alice, ts: TS }))!;
    expect(job.body.entries.map((e) => [e.body.type, e.body.scope])).toEqual([
      ['rotate', 'org'],
      ['rotate', 'wg'],
      ['rotate', 'wg'],
    ]);
    expect(job.log.state.pendingScopes()).toEqual([]);
    expect(pendingRotation(job.log, { author: alice })).toBeNull();
  });

  it('lets a workgroup writer rotate a pending workgroup scope but not the org scope', async () => {
    const state = world.state;
    const writer = await World.create(alice);
    await writer.admitUser(alice, bob);
    const scopes = [['org', writer.org] as const, ['wg', writer.defaultWg] as const, ['wg', state.org] as const];
    const job = (await pendingRotation(writer.log!, { author: bob, ts: TS }, scopes))!;
    expect(job.body.entries.map((e) => e.body.scope_id)).toEqual([writer.defaultWg]);
  });

  it('recomputes from the new state after seq_conflict', async () => {
    const stale = world.log!;
    const first = await buildBatch({
      log: stale,
      author: alice,
      entries: [{ type: 'wg_create', wg: await idOf('a'), epoch: 1 }],
      ts: TS,
    });
    await world.batch(alice, [{ type: 'wg_create', wg: await idOf('b'), epoch: 1 }]);
    await expect(code(appendEntries(world.log, first.body.entries, ''))).resolves.toBe('seq_conflict');
    const again = await buildBatch({
      log: world.log,
      author: alice,
      entries: [{ type: 'wg_create', wg: await idOf('a'), epoch: 1 }],
      ts: TS,
    });
    expect(again.body.expected_seq).toBe(world.log!.head.seq + 1);
    await checkBatch(world.log, again.body.entries, again.body.wraps, alice.id, { now: Date.now() });
  });
});

describe('checkBatch refusals', () => {
  let world: World;

  beforeAll(async () => {
    world = await World.create(alice);
    await world.admitUser(alice, bob);
  });

  const removal = async () =>
    buildBatch({
      log: world.log,
      author: alice,
      entries: [{ type: 'remove', subject: bob.id }],
      keys: world.ring(alice.id),
      ts: TS,
    });

  it('accepts the built removal', async () => {
    const built = await removal();
    const result = await checkBatch(world.log, built.body.entries, built.body.wraps, alice.id);
    expect(result.wraps.map(slotJson)).toHaveLength(built.body.wraps.length);
  });

  it('refuses a batch without the rotations, with a missing wrap, an extra rotate or a rotate before the trigger', async () => {
    const built = await removal();
    const [remove, ...rotates] = built.body.entries;
    expect(await code(checkBatch(world.log, [remove], [], alice.id))).toBe('rotation_required');
    expect(await code(checkBatch(world.log, built.body.entries, built.body.wraps.slice(1), alice.id))).toBe(
      'rotation_required',
    );
    const signer = { id: alice.id, key: alice.identity.sig.privateKey };
    const extra = await buildEntries({ org: world.org, head: world.log!.head }, signer, [
      { type: 'remove', subject: bob.id },
      { type: 'rotate', scope: 'org', scope_id: world.org, epoch: 2 },
      { type: 'rotate', scope: 'wg', scope_id: world.defaultWg, epoch: 2 },
      { type: 'rotate', scope: 'wg', scope_id: world.defaultWg, epoch: 3 },
    ]);
    expect(await code(checkBatch(world.log, extra, [], alice.id))).toBe('rotation_required');
    const early = await buildEntries({ org: world.org, head: world.log!.head }, signer, [
      { type: 'rotate', scope: 'org', scope_id: world.org, epoch: 2 },
      { type: 'remove', subject: bob.id },
      { type: 'rotate', scope: 'wg', scope_id: world.defaultWg, epoch: 2 },
    ]);
    expect(await code(checkBatch(world.log, early, [], alice.id))).toBe('rotation_required');
    const other = await buildEntries({ org: world.org, head: world.log!.head }, signer, [
      { type: 'remove', subject: bob.id },
      { type: 'rotate', scope: 'org', scope_id: world.org, epoch: 2 },
      { type: 'rotate', scope: 'wg', scope_id: await idOf('nowhere'), epoch: 1 },
    ]);
    expect(await code(checkBatch(world.log, other, [], alice.id))).toBe('log_rights');
    expect(rotates).toHaveLength(2);
  });

  it('names the subject of every entry type', () => {
    expect(entrySubject({ subject: 's', device: 'd', by: 'b' })).toBe('s');
    expect(entrySubject({ device: 'd', by: 'b' })).toBe('d');
    expect(entrySubject({ by: 'b' })).toBe('b');
  });
});

describe('platform devices in batches', () => {
  it('assigns a certified platform device with its keys from the platform log and excludes it after decertification', async () => {
    const pdev = await actor('pdev');
    const plat = await platformWith([pdev]);
    const world = await World.create(alice, { private: true });
    world.platform = plat.state;
    world.add(pdev);
    const built = await world.batch(alice, [
      { type: 'device_assign', device: pdev.id, wg: world.defaultWg, platform_seq: plat.seqs.get(pdev.id)! },
    ]);
    expect(built.body.wraps.map((w) => w.recipient_id)).toEqual([pdev.id, pdev.id]);
    expect(world.currentKey(pdev.id, 'wg', world.defaultWg)).toBe(world.currentKey(alice.id, 'wg', world.defaultWg));
    // decertified: no recipient of new epochs any more
    const ended = plat.state.with({
      certifications: new Map([...plat.state.certifications].map(([seq, c]) => [seq, { ...c, decertifiedSeq: 99 }])),
    });
    world.platform = ended;
    expect(decertificationBatch(world.log!, plat.state, { author: alice })).toBeNull();
    const owed = (await decertificationBatch(world.log!, ended, { author: alice, ts: TS }))!;
    expect(owed.body.entries.map((e) => e.body.type)).toEqual(['device_unassign', 'rotate', 'rotate']);
    expect(owed.body.wraps.map((w) => w.recipient_id)).toEqual([alice.id, alice.id]);
    const rotate = await world.batch(alice, [{ type: 'rotate', scope: 'wg', scope_id: world.defaultWg, epoch: 2 }]);
    expect(rotate.body.wraps.map((w) => w.recipient_id)).toEqual([alice.id]);
  });

  it('owes device_unassign_org and every explicit device_unassign of a decertified device (ADR-060)', async () => {
    const pdev = await actor('pdev');
    const plat = await platformWith([pdev]);
    const world = await World.create(alice, { private: true });
    world.platform = plat.state;
    world.add(pdev);
    const seq = plat.seqs.get(pdev.id)!;
    const wg = await idOf('wg org-wide');
    await world.batch(alice, [{ type: 'device_assign', device: pdev.id, wg: world.defaultWg, platform_seq: seq }]);
    await world.batch(alice, [{ type: 'device_assign_org', device: pdev.id, platform_seq: seq }]);
    const created = await world.batch(alice, [{ type: 'wg_create', wg, epoch: 1 }]);
    // the organization's device is a recipient of the new workgroup's first key
    expect(created.body.wraps.map((w) => w.recipient_id).sort()).toEqual([alice.id, pdev.id].sort());
    expect(unassignAll(world.log!.state, pdev.id)).toEqual([
      { type: 'device_unassign_org', device: pdev.id },
      { type: 'device_unassign', device: pdev.id, wg: world.defaultWg },
    ]);
    expect(unassignAll(world.log!.state, 'unknown')).toEqual([]);
    const ended = plat.state.with({
      certifications: new Map([...plat.state.certifications].map(([s, c]) => [s, { ...c, decertifiedSeq: 99 }])),
    });
    world.platform = ended;
    const owed = (await decertificationBatch(world.log!, ended, { author: alice, ts: TS }))!;
    expect(owed.body.entries.map((e) => e.body.type)).toEqual([
      'device_unassign_org',
      'device_unassign',
      'rotate',
      'rotate',
      'rotate',
    ]);
  });
});

describe('builder time source', () => {
  it('uses the clock when no time is given', async () => {
    vi.useFakeTimers({ now: Date.UTC(2027, 0, 1) });
    try {
      const world = await World.create(alice);
      const built = await buildBatch({
        log: world.log,
        author: alice,
        entries: [{ type: 'wg_create', wg: await idOf('t'), epoch: 1 }],
      });
      expect(built.body.entries[0]!.body.ts).toBe('2027-01-01T00:00:00.000Z');
    } finally {
      vi.useRealTimers();
    }
  });
});
