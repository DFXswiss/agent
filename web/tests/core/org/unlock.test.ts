// The "being unlocked" decision (ADR-059 section 2, `core/org/unlock.ts`): an admin that reads a workgroup by
// inheritance without its current key sees "being unlocked" while somebody else can still deliver the key, the
// dead-end variant once nobody but inheriting admins is left, and nothing while the keys are still being read.
import { beforeAll, describe, expect, it } from 'vitest';
import { owedPlanner } from '../../../src/core/jobs/rotation';
import { epochKey, WG } from '../../../src/core/log/state';
import { keyHolderLeft, selectUnlock, unlockStatus } from '../../../src/core/org/unlock';
import { createStore } from '../../../src/core/store';
import { setQueue } from '../../../src/core/store/slices/jobs';
import { setSession } from '../../../src/core/store/slices/me';
import { addKeys, setLog } from '../../../src/core/store/slices/orgs';
import { Secret } from '../../../src/core/crypto/secret';
import { actors, type Actor, idOf, T0, World } from '../log/world';

let ada: Actor, bob: Actor, cy: Actor, dev: Actor;
let world: World;
let wg: string;

beforeAll(async () => {
  [ada, bob, cy, dev] = await actors('ada', 'bob', 'cy', 'dev');
  world = await World.create(ada);
  wg = await idOf('unlock wg');
  await world.batch(ada, [{ type: 'wg_create', wg, epoch: 1 }]);
  await world.admitUser(ada, bob, 'member', { [wg]: 'read' });
  await world.admitUser(ada, cy, 'admin');
  await world.admitDevice(ada, dev, [wg]);
});

describe('unlockStatus', () => {
  it('is null for readers holding the key and for users who cannot read', () => {
    const state = world.state;
    expect(unlockStatus(state, cy.id, wg, true)).toBeNull();
    expect(unlockStatus(state, dev.id, 'nowhere', false)).toBeNull();
  });

  it('is "unlocking" while an explicit member, an assigned device or the epoch author is left', async () => {
    expect(unlockStatus(world.state, cy.id, wg, false)).toBe('unlocking');
    // without bob: dev (assigned) and ada (author and explicit writer) remain
    await world.batch(ada, [{ type: 'ungrant', subject: bob.id, wg }]);
    expect(keyHolderLeft(world.state, cy.id, wg)).toBe(true);
    await world.batch(ada, [{ type: 'device_unassign', device: dev.id, wg }]);
    // ada writes explicitly
    expect(keyHolderLeft(world.state, cy.id, wg)).toBe(true);
    expect(keyHolderLeft(world.state, ada.id, wg)).toBe(false);
  });

  it('is "lost" once only inheriting admins are left', async () => {
    const lost = await World.create(ada);
    const w = await idOf('lost wg');
    await lost.batch(ada, [{ type: 'wg_create', wg: w, epoch: 1 }]);
    await lost.admitUser(ada, bob, 'admin');
    await lost.admitUser(ada, cy, 'admin');
    await lost.batch(ada, [{ type: 'ungrant', subject: ada.id, wg: w }]);
    // ada (author of epoch 1) is still active
    expect(unlockStatus(lost.state, cy.id, w, false)).toBe('unlocking');
    await lost.batch(ada, [{ type: 'remove', subject: ada.id }]);
    expect(unlockStatus(lost.state, cy.id, w, false)).toBe('lost');
    expect(unlockStatus(lost.state, bob.id, w, false)).toBe('lost');
  });
});

describe('the way out of "lost"', () => {
  it('an admin rotates a workgroup nobody can deliver any more (a log from before ADR-059)', async () => {
    const old = await World.create(ada);
    const w = await idOf('orphan wg');
    await old.batch(ada, [{ type: 'wg_create', wg: w, epoch: 1 }]);
    await old.admitUser(ada, bob, 'admin');
    await old.admitUser(ada, cy, 'member');
    await old.batch(ada, [{ type: 'ungrant', subject: ada.id, wg: w }]);
    // the author leaves without the rotations the old rule did not owe (the test world does not enforce R)
    await old.batch(bob, [{ type: 'remove', subject: ada.id }]);
    expect(unlockStatus(old.state, bob.id, w, false)).toBe('lost');
    expect(owedPlanner(null)(old.log!, bob.id)).toEqual({
      entries: [{ type: 'rotate', scope: WG, scope_id: w, epoch: old.state.epoch(WG, w) + 1 }],
    });
    // not when the admin holds the current key, never for a member, never for an archived workgroup
    const held = (scope: string, id: string, epoch: number) =>
      scope === WG && id === w && epoch === old.state.epoch(WG, w) ? Secret.random(32) : null;
    expect(owedPlanner(null, held)(old.log!, bob.id)).toBeNull();
    expect(owedPlanner(null)(old.log!, cy.id)).toBeNull();
    await old.batch(bob, [{ type: 'wg_archive', wg: w }]);
    expect(owedPlanner(null)(old.log!, bob.id)).toBeNull();
  });
});

describe('selectUnlock', () => {
  it('decides only once the log is verified and the job queue settled', () => {
    const store = createStore();
    const org = world.org;
    const select = selectUnlock(org, wg);
    expect(select(store.getState())).toBeNull();
    setLog(store, org, world.log!);
    expect(select(store.getState())).toBeNull();
    setSession(store, cy.id, { keyId: 'session.x', expiresAt: T0 + 3600_000 });
    expect(select(store.getState())).toBeNull();
    setQueue(store, org, { running: 'catch-up' });
    expect(select(store.getState())).toBeNull();
    setQueue(store, org, { running: null, waiting: 1 });
    expect(select(store.getState())).toBeNull();
    setQueue(store, org, { waiting: 0 });
    expect(select(store.getState())).toBeNull();
    setQueue(store, org, { last: { name: 'catch-up', ok: true, code: null, at: T0 } });
    expect(select(store.getState())).toBe('unlocking');
    const epoch = world.state.epoch('wg', wg);
    addKeys(store, org, new Map([[epochKey('wg', wg, epoch), Secret.of(new Uint8Array(32))]]));
    expect(select(store.getState())).toBeNull();
  });
});
