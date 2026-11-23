// ADR-059: every admin reads every workgroup by inheritance. The effective access and its sources (`explicitAccess`,
// `wgAccess`, `inherited`, projection), the credential scope that does not inherit, catch-up of every epoch for an
// inheriting admin, the transition rule `compatSkip`, and the batches of promotion, demotion and self-demotion.
import { beforeAll, describe, expect, it } from 'vitest';
import { CryptoError } from '../../../src/core/crypto/errors';
import { catchUpEpochs, compatSkip, confirmedSlots, missingWraps } from '../../../src/core/log/catchup';
import { buildBatch, checkBatch, SELF_OWED } from '../../../src/core/log/rotation';
import { compareSlots, credReaders, slotJson } from '../../../src/core/log/wraps';
import { actors, type Actor, idOf, TS, World } from './world';

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
let world: World;
let wg: string;

beforeAll(async () => {
  [alice, bob, carol, dave, device, dev2] = await actors('alice', 'bob', 'carol', 'dave', 'device', 'dev2');
  world = await World.create(alice);
  wg = await idOf('inherited wg');
  await world.batch(alice, [{ type: 'wg_create', wg, epoch: 1 }]);
  await world.batch(alice, [{ type: 'rotate', scope: 'wg', scope_id: wg, epoch: 2 }]);
  await world.admitUser(alice, bob, 'member', { [wg]: 'read' });
  await world.admitUser(alice, carol);
  await world.admitDevice(alice, device, [world.defaultWg]);
  world.add(dave);
});

describe('effective access (OrgState)', () => {
  it('derives the inherited read of an admin and keeps explicit access apart', () => {
    const state = world.state;
    expect(state.explicitAccess(alice.id, wg)).toBe('write');
    expect(state.inherited(alice.id, wg)).toBe(false);
    expect(state.wgAccess(bob.id, wg)).toBe('read');
    expect(state.wgAccess(carol.id, wg)).toBeNull();
    expect(state.inherited(carol.id, wg)).toBe(false);
    expect(state.wgAccess(device.id, wg)).toBeNull();
    expect(state.wgAccess(alice.id, 'nowhere')).toBeNull();
  });

  it('a promoted admin reads every workgroup; the batch gives it every epoch the author holds', async () => {
    const built = await world.batch(alice, [{ type: 'role', subject: carol.id, role: 'admin' }]);
    expect(built.required).toEqual([]);
    expect(built.body.wraps.map((w) => [w.scope_id, w.epoch, w.recipient_id])).toEqual([
      [wg, 1, carol.id],
      [wg, 2, carol.id],
    ]);
    const state = world.state;
    expect(state.wgAccess(carol.id, wg)).toBe('read');
    expect(state.explicitAccess(carol.id, wg)).toBeNull();
    expect(state.inherited(carol.id, wg)).toBe(true);
    expect(state.inherited(carol.id, world.defaultWg)).toBe(false);
    expect(state.canWriteWg(carol.id, wg)).toBe(false);
    const row = (state.projection().workgroups as { wg_id: string; inherited: string[] }[]).find((w) => w.wg_id === wg);
    expect(row!.inherited).toEqual([carol.id]);
  });

  it('does not spread credentials through the inherited read (credReaders use the explicit access)', async () => {
    await world.batch(alice, [{ type: 'device_assign', device: device.id, wg }]);
    expect(credReaders([world.state], bob.id).has(device.id)).toBe(true);
    // carol reads the device's workgroup only as admin, but the default workgroup explicitly
    expect(credReaders([world.state], carol.id).has(device.id)).toBe(true);
    await world.batch(alice, [{ type: 'device_unassign', device: device.id, wg: world.defaultWg }]);
    expect(credReaders([world.state], carol.id).has(device.id)).toBe(false);
    expect(credReaders([world.state], alice.id).has(device.id)).toBe(true);
  });
});

describe('catch-up of an inheriting admin', () => {
  it('gives every epoch to an admin reading by inheritance, the current one to everybody else', () => {
    const state = world.state;
    const current = state.epoch('wg', wg);
    expect(catchUpEpochs(state, 'wg', wg, carol.id)).toEqual(Array.from({ length: current }, (_, i) => i + 1));
    expect(catchUpEpochs(state, 'wg', wg, bob.id)).toEqual([current]);
    expect(catchUpEpochs(state, 'org', state.org, carol.id)).toEqual([state.epoch('org', state.org)]);
  });

  it('confirms exactly the epochs catch-up gives, and withholds device wraps by the transition rule', () => {
    const state = world.state;
    const current = state.epoch('wg', wg);
    const slot = (epoch: number, recipient: string) => ({ scope: 'wg', scope_id: wg, epoch, recipient });
    const offered = [slot(1, carol.id), slot(current, carol.id), slot(1, bob.id), slot(current, device.id)];
    // alice created epoch 1 and 2 explicitly: everything entitled is confirmed (older epochs only for carol)
    expect(confirmedSlots(world.log!, alice.id, offered)).toEqual(
      [slot(1, carol.id), slot(current, carol.id), slot(current, device.id)].sort(compareSlots),
    );
    // carol reads only as admin and did not create the epoch: no wrap for the device
    expect(compatSkip(state, carol.id, slot(current, device.id))).toBe(true);
    expect(compatSkip(state, carol.id, slot(current, bob.id))).toBe(false);
    expect(compatSkip(state, alice.id, slot(current, device.id))).toBe(false);
    expect(compatSkip(state, carol.id, { scope: 'org', scope_id: state.org, epoch: 1, recipient: device.id })).toBe(
      false,
    );
    expect(confirmedSlots(world.log!, carol.id, offered).map((s) => s.recipient)).not.toContain(device.id);
    expect(missingWraps(world.log!, carol.id, []).map(slotJson)).toContainEqual(slotJson(slot(current, device.id)));
  });

  it("lets an inheriting admin's own epoch reach devices (created epochs are unaffected)", async () => {
    const built = await world.batch(carol, [{ type: 'rotate', scope: 'wg', scope_id: wg, epoch: 3 }]);
    expect(built.body.wraps.map((w) => w.recipient_id)).toContain(device.id);
    expect(compatSkip(world.state, carol.id, { scope: 'wg', scope_id: wg, epoch: 3, recipient: device.id })).toBe(
      false,
    );
  });

  it('skips the device wrap of an epoch the inheriting admin did not create in its own batch', async () => {
    await world.batch(alice, [{ type: 'rotate', scope: 'wg', scope_id: wg, epoch: 4 }]);
    await world.admitDevice(alice, dev2, []);
    const built = await world.batch(carol, [{ type: 'device_assign', device: dev2.id, wg }]);
    expect(built.body.wraps.map((w) => [w.scope, w.recipient_id])).toEqual([['org', dev2.id]]);
    expect(world.currentKey(dev2.id, 'wg', wg)).toBeNull();
  });
});

describe('revocation of the inherited read', () => {
  it('rotates every workgroup a demoted admin read only as admin, without a wrap for it', async () => {
    const before = world.log!;
    const built = await world.batch(alice, [{ type: 'role', subject: carol.id, role: 'member' }]);
    expect(built.required).toEqual([{ scope: 'wg', scope_id: wg }]);
    expect(built.body.entries.map((e) => e.body.type)).toEqual(['role', 'rotate']);
    expect(built.body.wraps.some((w) => w.recipient_id === carol.id)).toBe(false);
    expect(world.state.wgAccess(carol.id, wg)).toBeNull();
    expect(await code(checkBatch(before, built.body.entries.slice(0, 1), [], alice.id))).toBe('rotation_required');
  });

  it('turns a self-demotion into a pending rotation (like leaving)', async () => {
    expect([...SELF_OWED].sort()).toEqual(['remove', 'role']);
    await world.batch(alice, [{ type: 'role', subject: carol.id, role: 'admin' }]);
    const built = await world.batch(carol, [{ type: 'role', subject: carol.id, role: 'member' }]);
    expect(built.required).toEqual([]);
    expect(built.body.entries).toHaveLength(1);
    expect(world.state.pendingScopes()).toEqual([['wg', wg]]);
    const rotate = await buildBatch({
      log: world.log,
      author: alice,
      entries: [{ type: 'rotate', scope: 'wg', scope_id: wg, epoch: world.state.epoch('wg', wg) + 1 }],
      keys: world.ring(alice.id),
      ts: TS,
    });
    expect(rotate.log.state.pendingScopes()).toEqual([]);
  });

  it('keeps the explicit access of a demoted admin and leaves the default workgroup alone', async () => {
    await world.admitUser(alice, dave, 'admin');
    await world.batch(alice, [{ type: 'grant', subject: dave.id, wg, access: 'write' }]);
    const built = await world.batch(alice, [{ type: 'role', subject: dave.id, role: 'member' }]);
    expect(built.required).toEqual([]);
    expect(world.state.explicitAccess(dave.id, wg)).toBe('write');
  });
});
